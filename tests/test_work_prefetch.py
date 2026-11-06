"""Offline lifecycle tests using real threads/events and the real pure key helper."""

import importlib.util
import json
import re
import sys
import threading
import types
import unittest
from concurrent.futures import CancelledError, Future
from copy import deepcopy
from pathlib import Path
from unittest import mock

from tests.source_units import load_units


# Load the actual key implementation without importing provider SDKs or caches.
_key_namespace = load_units(
    "chaoxing/api/answer.py",
    {"_normalize_question_title", "question_cache_key"},
    {"json": json, "sub": re.sub},
)
question_cache_key = _key_namespace["question_cache_key"]
_fake_answer = types.ModuleType("api.answer")
_fake_answer.question_cache_key = question_cache_key
_spec = importlib.util.spec_from_file_location(
    "_work_prefetch_under_test",
    Path(__file__).resolve().parents[1] / "chaoxing/api/work_prefetch.py",
)
_module = importlib.util.module_from_spec(_spec)
with mock.patch.dict(sys.modules, {_spec.name: _module, "api.answer": _fake_answer}):
    _spec.loader.exec_module(_module)
WorkPrefetcher = _module.WorkPrefetcher
PreparedWork = _module.PreparedWork


def question(title="Question", *, kind="single", options=None):
    return {"title": title, "type": kind, "options": ["A", "B"] if options is None else options}


class WorkPrefetcherTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.prefetchers = []
        self.gates = []
        self.helpers = []

    def tearDown(self):
        self.stop.set()
        for gate in self.gates:
            gate.set()
        for prefetcher in self.prefetchers:
            prefetcher.close()
            self.assertFalse(prefetcher._worker.is_alive())
        for thread in self.helpers:
            thread.join(3)
            self.assertFalse(thread.is_alive(), thread.name)

    def prefetcher(self, query, capacity=2, query_delay=0):
        prefetcher = WorkPrefetcher(query, capacity, self.stop, query_delay)
        self.prefetchers.append(prefetcher)
        return prefetcher

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def launch(self, operation, *, name=None):
        future = Future()

        def run():
            try:
                future.set_result(operation())
            except BaseException as exc:
                future.set_exception(exc)

        thread = threading.Thread(
            target=run, name=name or f"prefetch-test-{len(self.helpers)}", daemon=False
        )
        self.helpers.append(thread)
        thread.start()
        return future

    def launch_waiter(self, prefetcher, operation):
        """Know the helper really reached Condition.wait, without using sleeps."""
        waiting = threading.Event()
        name = f"prefetch-test-{len(self.helpers)}"
        original_wait = prefetcher._condition.wait

        def observed_wait(timeout=None):
            if threading.current_thread().name == name:
                waiting.set()
            return original_wait(timeout)

        patcher = mock.patch.object(prefetcher._condition, "wait", observed_wait)
        patcher.start()
        self.addCleanup(patcher.stop)
        future = self.launch(operation, name=name)
        self.assertTrue(waiting.wait(3), "helper did not reach its cancellation point")
        return future

    def blocking_query(self):
        entered = threading.Event()
        gate = self.gate()
        calls = []

        def query(info):
            calls.append(info["title"])
            entered.set()
            if not gate.wait(5):
                raise AssertionError("test did not unblock provider query")
            return info["title"]

        return query, entered, gate, calls

    def test_constructor_rejects_invalid_configuration_before_starting_worker(self):
        for capacity in (0, -1, 1.5, True):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                WorkPrefetcher(lambda info: "A", capacity, self.stop)
        for delay in (-1, float("nan"), float("inf"), True, "1"):
            with self.subTest(delay=delay), self.assertRaises(ValueError):
                WorkPrefetcher(lambda info: "A", 1, self.stop, delay)
        with self.assertRaises(TypeError):
            WorkPrefetcher(None, 1, self.stop)
        with self.assertRaises(TypeError):
            WorkPrefetcher(lambda info: "A", 1, None)

    def test_completed_results_retain_capacity_and_return_independent_maps(self):
        calls = []
        prefetcher = self.prefetcher(lambda info: calls.append(info) or "A", capacity=1)
        info = question()
        handle = prefetcher.prepare("work", [info])
        self.assertIsInstance(handle, PreparedWork)
        self.assertEqual(handle.key, "work")
        self.assertIsNone(handle.future.result(timeout=3))
        self.assertFalse(prefetcher._worker.daemon)
        result = prefetcher.answers(handle)
        self.assertEqual(result, {question_cache_key(info): "A"})
        result.clear()
        self.assertEqual(prefetcher.answers(handle), {question_cache_key(info): "A"})
        self.assertIs(prefetcher.prepare("work", [question("Ignored")]), handle)
        self.assertIsNone(prefetcher.prepare("full", []))
        self.assertEqual(len(calls), 1)
        retained = prefetcher._batches[handle.key].answers
        prefetcher.release(handle)
        self.assertEqual(retained, {})
        self.assertEqual(prefetcher._batches, {})
        with self.assertRaises(CancelledError):
            prefetcher.answers(handle)
        prefetcher.release(handle)
        replacement = prefetcher.prepare("new", [])
        self.assertEqual(prefetcher.answers(replacement), {})

    def test_simultaneous_producers_reuse_one_handle(self):
        query, entered, gate, calls = self.blocking_query()
        prefetcher = self.prefetcher(query, capacity=1)
        barrier = threading.Barrier(7, timeout=3)

        def prepare():
            barrier.wait()
            return prefetcher.prepare("shared-context", [question()])

        results = [self.launch(prepare) for _ in range(6)]
        barrier.wait()
        handles = [result.result(timeout=3) for result in results]
        self.assertTrue(all(handle is handles[0] for handle in handles))
        self.assertTrue(entered.wait(3))
        gate.set()
        self.assertEqual(prefetcher.answers(handles[0]), {question_cache_key(question()): "Question"})
        self.assertEqual(calls, ["Question"])

    def test_single_query_worker_and_capacity_includes_running_and_queued(self):
        barrier = threading.Barrier(2, timeout=3)
        gate = self.gate()
        lock = threading.Lock()
        active = 0
        peak = 0
        identities = set()
        calls = []

        def query(info):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                identities.add(threading.get_ident())
                calls.append(info["title"])
            try:
                if info["title"] == "First":
                    barrier.wait()
                    if not gate.wait(5):
                        raise AssertionError("provider gate timed out")
                return "A"
            finally:
                with lock:
                    active -= 1

        prefetcher = self.prefetcher(query, capacity=3)
        first = prefetcher.prepare("first", [question("First")])
        barrier.wait()
        second = prefetcher.prepare("second", [question("Second")])
        third = prefetcher.prepare("third", [question("Third")])
        self.assertIsNone(prefetcher.prepare("overflow", [question("Overflow")]))
        self.assertEqual(calls, ["First"])
        gate.set()
        for handle in (first, second, third):
            self.assertEqual(len(prefetcher.answers(handle)), 1)
        self.assertEqual(calls, ["First", "Second", "Third"])
        self.assertEqual(peak, 1)
        self.assertEqual(identities, {prefetcher._worker.ident})
        self.assertIsNone(prefetcher.prepare("still-full", []))

    def test_releasing_completed_work_wakes_capacity_waiter(self):
        prefetcher = self.prefetcher(lambda info: "A", capacity=1)
        first = prefetcher.prepare("first", [])
        first.future.result(timeout=3)
        waiting = self.launch_waiter(prefetcher, lambda: prefetcher.prepare("next", [], wait=True))
        self.assertFalse(waiting.done())
        prefetcher.release(first)
        second = waiting.result(timeout=3)
        self.assertEqual(second.key, "next")
        self.assertEqual(prefetcher.answers(second), {})

    def test_releasing_running_work_keeps_slot_until_query_finishes(self):
        query, entered, gate, calls = self.blocking_query()
        prefetcher = self.prefetcher(query, capacity=1)
        first = prefetcher.prepare("same", [question("First"), question("Must not run")])
        self.assertTrue(entered.wait(3))
        prefetcher.release(first)
        with self.assertRaises(CancelledError):
            prefetcher.answers(first)
        self.assertFalse(first.future.done())
        self.assertIsNone(prefetcher.prepare("other", []))
        self.assertIsNone(prefetcher.prepare("same", []))
        waiting = self.launch_waiter(prefetcher, lambda: prefetcher.prepare("same", [], wait=True))
        self.assertFalse(waiting.done())
        gate.set()
        second = waiting.result(timeout=3)
        self.assertIsNot(second, first)
        self.assertEqual(prefetcher.answers(second), {})
        with self.assertRaises(CancelledError):
            first.future.result(timeout=3)
        self.assertEqual(calls, ["First"])
        prefetcher.release(first)
        self.assertEqual(prefetcher.answers(second), {})

    def test_releasing_queued_work_cancels_future_without_querying(self):
        query, entered, gate, calls = self.blocking_query()
        prefetcher = self.prefetcher(query, capacity=2)
        first = prefetcher.prepare("first", [question("First")])
        self.assertTrue(entered.wait(3))
        queued = prefetcher.prepare("queued", [question("Must not run")])
        prefetcher.release(queued)
        self.assertTrue(queued.future.cancelled())
        self.assertEqual(len(prefetcher._queue), 0)
        replacement = prefetcher.prepare("replacement", [])
        gate.set()
        prefetcher.answers(first)
        self.assertEqual(prefetcher.answers(replacement), {})
        self.assertEqual(calls, ["First"])

    def test_questions_are_deep_copied_and_provider_mutations_are_isolated(self):
        entered = threading.Event()
        gate = self.gate()
        seen = []

        def query(info):
            if info["title"] == "Blocker":
                entered.set()
                if not gate.wait(5):
                    raise AssertionError("provider gate timed out")
                return "A"
            seen.append(deepcopy(info))
            info["title"] = "Provider changed"
            info["options"][0]["text"] = "Provider option"
            return "B"

        prefetcher = self.prefetcher(query)
        blocker = prefetcher.prepare("blocker", [question("Blocker")])
        self.assertTrue(entered.wait(3))
        source = question("Original", options=[{"text": "original option"}])
        original = deepcopy(source)
        batch = [source]
        handle = prefetcher.prepare("snapshot", batch)
        source["title"] = "Caller changed"
        source["options"][0]["text"] = "Caller option"
        batch.append(question("Caller appended"))
        gate.set()
        prefetcher.answers(blocker)
        self.assertEqual(prefetcher.answers(handle), {question_cache_key(original): "B"})
        self.assertEqual(seen, [original])
        self.assertEqual(source["title"], "Caller changed")
        self.assertEqual(source["options"][0]["text"], "Caller option")

    def test_batch_deduplicates_real_fingerprints_and_retains_missing_answers(self):
        calls = []
        prefetcher = self.prefetcher(lambda info: calls.append(deepcopy(info)))
        first = question("1Question（1.0分）")
        duplicate = question("Question")
        duplicate["id"] = "different-platform-id"
        other_type = question("Question", kind="multiple")
        other_options = question("Question", options=["B", "A"])
        handle = prefetcher.prepare("batch", [first, duplicate, other_type, other_options])
        expected = {question_cache_key(info): None for info in (first, other_type, other_options)}
        self.assertEqual(prefetcher.answers(handle), expected)
        self.assertEqual(prefetcher.answers(handle), expected)
        self.assertEqual(len(calls), 3)
        self.assertEqual(question_cache_key(first), question_cache_key(duplicate))

    def test_provider_mutation_cannot_change_fingerprint_of_aliased_duplicates(self):
        calls = []

        def query(info):
            calls.append(deepcopy(info))
            info["title"] = "Changed by provider"
            info["options"].append("C")
            return "A"

        prefetcher = self.prefetcher(query)
        info = question()
        handle = prefetcher.prepare("aliased", [info, info])
        self.assertEqual(prefetcher.answers(handle), {question_cache_key(info): "A"})
        self.assertEqual(calls, [info])

    def test_same_question_in_different_contexts_has_independent_results(self):
        calls = []

        def query(info):
            calls.append(info)
            return str(len(calls))

        prefetcher = self.prefetcher(query)
        info = question()
        first = prefetcher.prepare(("user-a", "course", "class", "work", "enc"), [info])
        second = prefetcher.prepare(("user-b", "course", "class", "work", "enc"), [info])
        self.assertIsNot(first, second)
        self.assertEqual(prefetcher.answers(first), {question_cache_key(info): "1"})
        self.assertEqual(prefetcher.answers(second), {question_cache_key(info): "2"})

    def test_provider_exception_propagates_unchanged_and_worker_continues(self):
        failure = RuntimeError("provider unavailable")
        calls = []

        def query(info):
            calls.append(info["title"])
            if info["title"] == "Fail":
                raise failure
            return "A"

        prefetcher = self.prefetcher(query)
        failed = prefetcher.prepare("failed", [question("Fail"), question("Skipped")])
        following = prefetcher.prepare("following", [question("Next")])
        with self.assertRaises(RuntimeError) as raised:
            prefetcher.answers(failed)
        self.assertIs(raised.exception, failure)
        self.assertIs(failed.future.exception(timeout=3), failure)
        self.assertEqual(prefetcher.answers(following), {question_cache_key(question("Next")): "A"})
        self.assertEqual(calls, ["Fail", "Next"])

    def test_fingerprint_failure_is_not_reported_as_a_missing_answer(self):
        query = mock.Mock(return_value="A")
        prefetcher = self.prefetcher(query)
        handle = prefetcher.prepare("malformed", [{"title": "No type"}])
        with self.assertRaises(KeyError):
            prefetcher.answers(handle)
        query.assert_not_called()

    def test_deepcopy_failure_reserves_no_capacity(self):
        failure = RuntimeError("cannot copy")

        class CannotCopy:
            def __deepcopy__(self, memo):
                raise failure

        prefetcher = self.prefetcher(lambda info: "A", capacity=1)
        with self.assertRaises(RuntimeError) as raised:
            prefetcher.prepare("invalid", [{"nested": CannotCopy()}])
        self.assertIs(raised.exception, failure)
        self.assertEqual(prefetcher._batches, {})
        self.assertEqual(prefetcher.answers(prefetcher.prepare("valid", [])), {})

    def test_foreign_handles_are_rejected(self):
        first = self.prefetcher(lambda info: "A")
        second = self.prefetcher(lambda info: "B")
        handle = first.prepare("work", [])
        with self.assertRaises(ValueError):
            second.answers(handle)
        with self.assertRaises(ValueError):
            second.release(handle)
        with self.assertRaises(ValueError):
            first.answers(object())
        self.assertEqual(first.answers(handle), {})

    def test_close_cancels_waiters_and_pending_queries_but_joins_inflight_query(self):
        query, entered, gate, calls = self.blocking_query()
        prefetcher = self.prefetcher(query)
        running = prefetcher.prepare("running", [question("First"), question("Skipped batch remainder")])
        self.assertTrue(entered.wait(3))
        queued = prefetcher.prepare("queued", [question("Skipped queued")])
        slot_waiter = self.launch_waiter(prefetcher, lambda: prefetcher.prepare("waiting", [], wait=True))
        answer_waiter = self.launch_waiter(prefetcher, lambda: prefetcher.answers(running))
        closing = self.launch(prefetcher.close)
        with self.assertRaises(CancelledError):
            queued.future.result(timeout=3)
        with self.assertRaises(CancelledError):
            slot_waiter.result(timeout=3)
        with self.assertRaises(CancelledError):
            answer_waiter.result(timeout=3)
        self.assertFalse(closing.done())
        self.assertTrue(prefetcher._worker.is_alive())
        self.assertFalse(self.stop.is_set())
        gate.set()
        self.assertIsNone(closing.result(timeout=3))
        self.assertFalse(prefetcher._worker.is_alive())
        self.assertEqual(prefetcher._batches, {})
        self.assertEqual(calls, ["First"])
        with self.assertRaises(CancelledError):
            prefetcher.prepare("after-close", [])
        prefetcher.close()
        self.assertFalse(self.stop.is_set())

    def test_external_stop_cancels_answer_and_capacity_waits_and_exits_worker(self):
        query, entered, gate, calls = self.blocking_query()
        prefetcher = self.prefetcher(query, capacity=1)
        running = prefetcher.prepare("running", [question("First"), question("Skipped")])
        self.assertTrue(entered.wait(3))
        slot_waiter = self.launch_waiter(prefetcher, lambda: prefetcher.prepare("waiting", [], wait=True))
        answer_waiter = self.launch_waiter(prefetcher, lambda: prefetcher.answers(running))
        self.stop.set()
        with self.assertRaises(CancelledError):
            slot_waiter.result(timeout=3)
        with self.assertRaises(CancelledError):
            answer_waiter.result(timeout=3)
        gate.set()
        prefetcher._worker.join(3)
        self.assertFalse(prefetcher._worker.is_alive())
        self.assertTrue(self.stop.is_set())
        self.assertEqual(calls, ["First"])
        with self.assertRaises(CancelledError):
            running.future.result(timeout=3)
        with self.assertRaises(CancelledError):
            prefetcher.prepare("after-stop", [])

    def test_query_delay_is_cancellable_by_release_stop_and_close(self):
        for action in ("release", "stop", "close"):
            with self.subTest(action=action):
                self.stop.clear()
                query = mock.Mock(return_value="A")
                prefetcher = self.prefetcher(query, query_delay=10)
                entered_delay = threading.Event()
                original_delay = prefetcher._wait_query_delay

                def observed_delay(handle):
                    entered_delay.set()
                    original_delay(handle)

                with mock.patch.object(prefetcher, "_wait_query_delay", observed_delay):
                    handle = prefetcher.prepare(action, [question()])
                    self.assertTrue(entered_delay.wait(3))
                    query.assert_not_called()
                    if action == "release":
                        prefetcher.release(handle)
                    elif action == "stop":
                        self.stop.set()
                    else:
                        self.launch(prefetcher.close).result(timeout=3)
                    with self.assertRaises(CancelledError):
                        handle.future.result(timeout=3)
                    with self.assertRaises(CancelledError):
                        prefetcher.answers(handle)
                    query.assert_not_called()
                    prefetcher.close()
                    self.assertFalse(prefetcher._worker.is_alive())

    def test_already_stopped_constructor_never_queries_and_close_preserves_event(self):
        self.stop.set()
        query = mock.Mock(return_value="A")
        prefetcher = self.prefetcher(query)
        with self.assertRaises(CancelledError):
            prefetcher.prepare("stopped", [question()])
        prefetcher.close()
        self.assertTrue(self.stop.is_set())
        query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
