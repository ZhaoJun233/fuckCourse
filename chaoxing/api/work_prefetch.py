"""Bounded, cancellable answer lookup; this module never accesses platform sessions."""

from collections import deque
from concurrent.futures import CancelledError, Future
from copy import deepcopy
from dataclasses import dataclass, field
from math import isfinite
from threading import Condition, Event, Thread, current_thread
from time import monotonic
from typing import Callable, Hashable, Iterable, Optional

from api.answer import question_cache_key

__all__ = ["PreparedWork", "WorkPrefetcher"]

_CANCEL_POLL_SECONDS = 0.05


@dataclass(frozen=True, eq=False)
class PreparedWork:
    """Opaque retained batch. ``future`` signals completion, not its answer values.

    Obtain handles from ``prepare`` and use ``answers`` to copy their results.
    The future is an observer only: callers must not complete or cancel it.
    After ``release``, ``answers`` raises ``CancelledError`` even if completed.
    """

    key: Hashable
    future: Future[None] = field(default_factory=Future, init=False, repr=False)
    _owner: object = field(repr=False)
    _cancelled: Event = field(default_factory=Event, init=False, repr=False)


@dataclass(eq=False)
class _Batch:
    handle: PreparedWork
    questions: list[dict]
    answers: dict[str, Optional[str]] = field(default_factory=dict)
    running: bool = False


class WorkPrefetcher:
    """Retain at most ``capacity`` batches and query them on one non-daemon thread.

    Capacity includes queued, running and completed-but-unreleased handles.
    A released running batch still occupies its slot until its query returns.
    Each unique question is queried once per batch, including missing answers.
    ``query_delay`` precedes each lookup (including the first), in seconds.

    The injected query must have its own bounded I/O timeout. Cancellation can
    interrupt waits and suppress subsequent queries, but cannot interrupt an
    already executing query. ``close`` waits for that query and joins the thread.
    The caller owns ``stop_event``; this class never sets or clears it.
    """

    def __init__(
        self,
        query: Callable[[dict], Optional[str]],
        capacity: int,
        stop_event: Event,
        query_delay: float = 0,
    ) -> None:
        if not callable(query):
            raise TypeError("query must be callable")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        if not isinstance(stop_event, Event):
            raise TypeError("stop_event must be a threading.Event")
        if (
            isinstance(query_delay, bool)
            or not isinstance(query_delay, (int, float))
            or not isfinite(query_delay)
            or query_delay < 0
        ):
            raise ValueError("query_delay must be a finite non-negative number")

        self._query = query
        self._capacity = capacity
        self._stop_event = stop_event
        self._query_delay = query_delay
        self._owner = object()
        self._condition = Condition()
        self._batches: dict[Hashable, _Batch] = {}
        self._queue: deque[_Batch] = deque()
        self._closed = False
        self._worker = Thread(target=self._run, name="work-prefetch", daemon=False)
        self._worker.start()

    def prepare(
        self, key: Hashable, questions: Iterable[dict], *, wait: bool = False
    ) -> Optional[PreparedWork]:
        """Snapshot questions, or reuse the unreleased handle for this exact key.

        Return ``None`` if no slot is available and ``wait`` is false. Waiting
        for a slot is cancellable by close/stop, which raise ``CancelledError``.
        A released in-flight handle cannot be reused; its key remains occupied
        until the query finishes. Keys must be hashable and stable while retained.
        Invalid/deepcopy-failing inputs raise without reserving a slot.
        """
        hash(key)
        with self._condition:
            while True:
                self._raise_if_cancelled()
                existing = self._batches.get(key)
                if existing is not None and not existing.handle._cancelled.is_set():
                    return existing.handle
                if existing is None and len(self._batches) < self._capacity:
                    snapshot = deepcopy(list(questions))
                    self._raise_if_cancelled()
                    handle = PreparedWork(key=key, _owner=self._owner)
                    batch = _Batch(handle=handle, questions=snapshot)
                    self._batches[key] = batch
                    self._queue.append(batch)
                    self._condition.notify_all()
                    return handle
                if not wait:
                    return None
                self._condition.wait(_CANCEL_POLL_SECONDS)

    def answers(self, handle: PreparedWork) -> dict[str, Optional[str]]:
        """Wait cancellably and return an independent map, preserving ``None``.

        Propagate fingerprint/provider exceptions unchanged. Closed, stopped or
        released work raises ``CancelledError``. Foreign handles raise ``ValueError``.
        The successful future contains ``None``; results live only in this owner.
        """
        self._validate_handle(handle)
        with self._condition:
            while True:
                self._raise_if_cancelled(handle)
                if handle.future.done():
                    handle.future.result()
                    return dict(self._batches[handle.key].answers)
                self._condition.wait(_CANCEL_POLL_SECONDS)

    def release(self, handle: PreparedWork) -> None:
        """Cancel one handle and discard its retained data; safe to repeat.

        Queued/completed work releases its slot immediately. Running work keeps
        its slot until the in-flight query returns, but answer waiters cancel now.
        No returned answer-map copies are changed. Foreign handles are rejected.
        """
        self._validate_handle(handle)
        with self._condition:
            handle._cancelled.set()
            batch = self._batches.get(handle.key)
            if batch is not None and batch.handle is handle:
                batch.answers.clear()
                if not batch.running:
                    self._remove_batch(batch)
            self._condition.notify_all()
        handle.future.cancel()

    def close(self) -> None:
        """Cancel all work and join the worker; idempotent, without setting stop.

        Calling this from the query worker itself is invalid (it cannot join
        itself) and raises ``RuntimeError``. In-flight I/O must finish normally.
        """
        if current_thread() is self._worker:
            raise RuntimeError("close cannot run on the prefetch query worker")
        with self._condition:
            self._closed = True
            futures = self._cancel_all()
        for future in futures:
            future.cancel()
        self._worker.join()

    def _validate_handle(self, handle: PreparedWork) -> None:
        if not isinstance(handle, PreparedWork) or handle._owner is not self._owner:
            raise ValueError("handle does not belong to this WorkPrefetcher")

    def _raise_if_cancelled(self, handle: Optional[PreparedWork] = None) -> None:
        if (
            self._closed
            or self._stop_event.is_set()
            or (handle is not None and handle._cancelled.is_set())
        ):
            raise CancelledError()

    def _remove_batch(self, batch: _Batch) -> None:
        # Called with the condition held, and only after no query uses its data.
        if self._batches.get(batch.handle.key) is batch:
            del self._batches[batch.handle.key]
        if batch in self._queue:
            self._queue.remove(batch)
        batch.questions.clear()
        batch.answers.clear()
        self._condition.notify_all()

    def _cancel_all(self) -> list[Future[None]]:
        # Keep running work counted until the query really returns.
        futures = []
        for batch in list(self._batches.values()):
            batch.handle._cancelled.set()
            batch.answers.clear()
            futures.append(batch.handle.future)
            if not batch.running:
                self._remove_batch(batch)
        self._condition.notify_all()
        return futures

    def _wait_query_delay(self, handle: PreparedWork) -> None:
        deadline = monotonic() + self._query_delay
        with self._condition:
            while True:
                self._raise_if_cancelled(handle)
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return
                self._condition.wait(min(remaining, _CANCEL_POLL_SECONDS))

    def _query_batch(self, batch: _Batch) -> dict[str, Optional[str]]:
        answers = {}
        for question in batch.questions:
            with self._condition:
                self._raise_if_cancelled(batch.handle)
            fingerprint = question_cache_key(question)
            if fingerprint in answers:
                continue
            # Preserve snapshots even when repeated inputs alias the same dict
            # and a provider normalizes/mutates its argument.
            query_question = deepcopy(question)
            self._wait_query_delay(batch.handle)
            answer = self._query(query_question)
            with self._condition:
                self._raise_if_cancelled(batch.handle)
            answers[fingerprint] = answer
        return answers

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    while not self._queue and not self._closed and not self._stop_event.is_set():
                        self._condition.wait(_CANCEL_POLL_SECONDS)
                    if self._closed or self._stop_event.is_set():
                        return
                    batch = self._queue.popleft()
                    if not batch.handle.future.set_running_or_notify_cancel():
                        batch.handle._cancelled.set()
                        self._remove_batch(batch)
                        continue
                    batch.running = True

                results = {}
                error = None
                try:
                    results = self._query_batch(batch)
                except BaseException as exc:
                    # Like an executor, preserve the original failure on its future.
                    error = exc

                with self._condition:
                    batch.running = False
                    cancelled = (
                        self._closed or self._stop_event.is_set()
                        or batch.handle._cancelled.is_set()
                    )
                    if cancelled:
                        batch.handle._cancelled.set()
                        self._remove_batch(batch)
                    else:
                        batch.questions.clear()
                        batch.answers = results

                # Future callbacks run outside our condition to avoid lock inversions.
                if cancelled:
                    batch.handle.future.set_exception(CancelledError())
                elif error is not None:
                    batch.handle.future.set_exception(error)
                else:
                    batch.handle.future.set_result(None)
                with self._condition:
                    self._condition.notify_all()
        finally:
            with self._condition:
                self._closed = True
                futures = self._cancel_all()
            for future in futures:
                future.cancel()
