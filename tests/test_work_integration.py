"""Offline chapter scheduling, submission, and guard regressions.

Load production definitions without package startup, credentials or network.
Event handshakes prove overlap without sleep or timing-based assertions.
"""
import enum
import json
import random
import re
import threading
import unittest
from collections import deque
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field
from math import isfinite
from queue import PriorityQueue
from threading import Condition, Event, Thread, current_thread
from time import monotonic
from difflib import SequenceMatcher
from types import SimpleNamespace
from typing import Any, Callable, Hashable, Iterable, Optional
from unittest.mock import Mock

from tests.source_units import load_units


class ProgressError(RuntimeError):
    pass


class FakeResponse:
    def __init__(self, payload=None, *, status=200, text=None,
                 url="https://platform.invalid/work", content_type="application/json"):
        self.status_code = status
        self.url = url
        self.headers = {"Content-Type": content_type}
        self.history = []
        self.encoding = None
        self.content = b"offline"
        self.payload = payload if payload is not None else {"status": True, "msg": "ok"}
        self.text = json.dumps(self.payload) if text is None else text

    def json(self):
        return deepcopy(self.payload)


class FakeSession:
    def __init__(self):
        self.responses = deque()
        self.calls = []
        self.cookies = Mock()
        self.request = Mock(side_effect=self._request)

    def _request(self, method, url, **kwargs):
        self.calls.append((method, url, deepcopy(kwargs)))
        if method == "GET":
            if not self.responses:
                raise AssertionError("Unexpected offline GET")
            return self.responses.popleft()
        if method == "POST":
            return FakeResponse()
        raise AssertionError(f"Unexpected offline method: {method}")

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def count(self, method):
        return sum(call[0] == method for call in self.calls)


class WorkIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        self.session = FakeSession()
        self.logger = Mock()
        self.course = {"courseId": "course-1", "clazzId": "class-1", "cpi": "cpi-1", "title": "Offline course"}
        self.job = {"type": "workid", "workid": "work-1", "jobid": "job-1",
                    "title": "Offline work", "enc": "fake-job-enc"}
        self.info = {"knowledgeid": "chapter-1", "ktoken": "fake-ktoken", "cpi": "cpi-1"}
        self.key = load_units(
            "chaoxing/api/answer.py", ["_normalize_question_title", "question_cache_key"],
            {"json": json, "sub": re.sub},
        )["question_cache_key"]
        checks = load_units("chaoxing/api/answer_check.py", [
            "check_answer", "check_single", "check_multiple", "check_judgement",
            "check_completion", "cut",
        ])
        prefetch = load_units("chaoxing/api/work_prefetch.py", [
            "PreparedWork", "_Batch", "WorkPrefetcher",
        ], {"dataclass": dataclass, "field": field, "deepcopy": deepcopy,
            "threading": threading, "CancelledError": CancelledError, "Future": Future,
            "ThreadPoolExecutor": ThreadPoolExecutor, "Any": Any, "Callable": Callable,
            "Hashable": Hashable, "Iterable": Iterable, "Optional": Optional,
            "deque": deque, "isfinite": isfinite, "Condition": Condition, "Event": Event,
            "Thread": Thread, "current_thread": current_thread, "monotonic": monotonic,
            "_CANCEL_POLL_SECONDS": 0.05, "question_cache_key": self.key, "__name__": __name__})
        self.PreparedWork = prefetch["PreparedWork"]
        self.WorkPrefetcher = prefetch["WorkPrefetcher"]
        methods = ["start_course", "finish_course", "_check_platform_response", "_wait_study",
                   "_work_key", "prepare_work", "release_work", "_prepared_answers",
                   "_query_work_answers", "_fetch_work", "study_work"]
        namespace = load_units("chaoxing/api/base.py", ["StudyResult"] + [
            f"Chaoxing.{name}" for name in methods
        ], {"Enum": enum.Enum, "CancelledError": CancelledError, "ProgressError": ProgressError,
            "PreparedWork": self.PreparedWork,
            "WorkPrefetcher": self.WorkPrefetcher, "question_cache_key": self.key,
            "check_answer": checks["check_answer"], "cut": checks["cut"], "re": re, "logger": self.logger,
            "requests": SimpleNamespace(exceptions=SimpleNamespace(RequestException=ConnectionError)),
            "SessionManager": SimpleNamespace(get_session=lambda: self.session),
            "decode_questions_info": json.loads, "deepcopy": deepcopy, "SequenceMatcher": SequenceMatcher,
            "time": SimpleNamespace(sleep=Mock(side_effect=AssertionError("Unexpected sleep"))),
            "choice": random.choice, "sample": random.sample, "randint": random.randint,
            "_Origin": "https://platform.invalid"})
        self.namespace = namespace
        self.Result = namespace["StudyResult"]
        client_type = namespace["Chaoxing"]
        self.client = client_type()
        self.client.release_work = Mock(wraps=self.client.release_work)
        self.client.tiku = SimpleNamespace(
            DISABLE=False, COVER_RATE=1.0, query=Mock(return_value="A"),
            get_submit_params=lambda: "", config={"true_list": ["正确"], "false_list": ["错误"]})
        self.client.kwargs = {"query_delay": 0}
        self.client.rollback_times = 0
        self.client.rate_limiter = SimpleNamespace(stop_event=None, limit_rate=Mock(return_value=True))
        self.client.video_log_limiter = SimpleNamespace(stop_event=None)
        self.client._work_prefetch = None
        self.client._course_session = None
        self.client._study_stop_event = self.stop
        self.client._ensure_study_context = Mock()
        self.client._touch_study_ajax = Mock()
        self.client.cipher = SimpleNamespace(get_enc=lambda: "fake-cipher")
        self.client.get_job_list = Mock(return_value=([self.job], self.info))
        self.client.is_point_complete = Mock(return_value=True)
        self.client.study_emptypage = Mock()
        self.client.skip_invalid = True
        self.addCleanup(self.client.finish_course)
        self.addCleanup(self.stop.set)

    @staticmethod
    def question(qid="q1", title="Same question", options=None):
        return {"id": qid, "title": title, "type": "single",
                "options": "A. first\nB. second" if options is None else options,
                "answerField": {f"answertype{qid}": "0"}}

    def queue_page(self, token, questions=None):
        self.session.responses.append(FakeResponse({
            "token": token, "questions": [self.question()] if questions is None else questions,
        }))

    def start(self, *, enabled=True, capacity=2):
        self.client.start_course(capacity, self.stop, prefetch_work=enabled)
        return self.client._work_prefetch

    def prepare(self, token="old-token", questions=None):
        self.queue_page(token, questions)
        return self.client.prepare_work(self.course, self.job, self.info)

    def submit_payload(self):
        posts = [call[2]["data"] for call in self.session.calls if call[0] == "POST"]
        self.assertEqual(1, len(posts))
        return posts[0]

    def chapter_runner(self):
        namespace = load_units("chaoxing/main.py", ["ChapterResult", "process_job", "process_chapter"], {
            "enum": enum, "Any": Any, "logger": self.logger, "StudyResult": self.Result,
            "ProgressError": ProgressError, "CancelledError": CancelledError,
        })
        return namespace["process_chapter"], namespace["ChapterResult"]

    def test_prepare_fetches_once_without_submission(self):
        manager = self.start()
        prepared = self.prepare()
        answers = manager.answers(prepared)
        self.assertEqual("A", answers[self.key(self.question())])
        self.assertEqual(1, self.session.count("GET"))
        self.assertEqual(0, self.session.count("POST"))
        self.client.tiku.query.assert_called_once()
        self.client.release_work(prepared)
        with self.assertRaises(CancelledError):
            manager.answers(prepared)

    def test_real_prefetch_overlaps_media_without_early_submission(self):
        query_started, media_started, query_finished = (threading.Event() for _ in range(3))
        def query(question):
            query_started.set()
            if not media_started.wait(2):
                raise AssertionError("Media did not overlap the provider")
            self.assertEqual(0, self.session.count("POST"))
            query_finished.set()
            return "A"
        def media(*args, **kwargs):
            media_started.set()
            self.assertTrue(query_started.wait(2), "Provider did not run during media")
            self.assertEqual(0, self.session.count("POST"))
            self.assertTrue(query_finished.wait(2), "Provider remained blocked")
            return self.Result.SUCCESS
        self.client.tiku.query.side_effect = query
        self.client.study_video = Mock(side_effect=media)
        self.client.get_job_list.return_value = ([{"type": "video", "jobid": "video-1"}, self.job], self.info)
        manager = self.start()
        self.queue_page("old-token")
        self.queue_page("fresh-token")
        run, result = self.chapter_runner()
        self.assertEqual(result.SUCCESS, run(self.client, self.course, {"id": "chapter-1", "title": "Offline chapter", "has_finished": False}, 1.0))
        self.client.study_video.assert_called_once()
        self.client.tiku.query.assert_called_once()
        self.assertEqual("fresh-token", self.submit_payload()["token"])
        released = [call.args[0] for call in self.client.release_work.call_args_list if call.args[0] is not None]
        self.assertTrue(released)
        self.assertTrue(all(handle is released[0] for handle in released))
        with self.assertRaises(CancelledError):
            manager.answers(released[0])

    def test_media_failure_releases_running_prefetch_without_submission(self):
        query_started, release_query = threading.Event(), threading.Event()
        def query(question):
            query_started.set()
            if not release_query.wait(2):
                raise AssertionError("Test did not release blocked provider")
            return "A"
        def media(*args, **kwargs):
            self.assertTrue(query_started.wait(2))
            return self.Result.ERROR
        self.client.tiku.query.side_effect = query
        self.client.study_video = Mock(side_effect=media)
        self.client.get_job_list.return_value = ([{"type": "video", "jobid": "video-1"}, self.job], self.info)
        manager = self.start()
        self.queue_page("old-token")
        run, result = self.chapter_runner()
        try:
            self.assertEqual(result.ERROR, run(self.client, self.course, {"id": "chapter-1", "title": "Offline chapter", "has_finished": False}, 1.0))
            self.assertEqual(0, self.session.count("POST"))
            self.client.release_work.assert_called_once()
            with self.assertRaises(CancelledError):
                manager.answers(self.client.release_work.call_args.args[0])
        finally:
            release_query.set()
        self.client.finish_course()

    def test_study_waits_for_prefetch_before_refreshing_page(self):
        entered, allow_query, wait_entered = (threading.Event() for _ in range(3))
        def query(question):
            entered.set()
            if not allow_query.wait(2):
                raise AssertionError("Test did not release query")
            return "A"
        self.client.tiku.query.side_effect = query
        manager = self.start()
        prepared = self.prepare()
        self.assertTrue(entered.wait(2))
        self.queue_page("fresh-token")
        real_wait = manager.answers
        def observed_wait(batch_id):
            wait_entered.set()
            return real_wait(batch_id)
        manager.answers = observed_wait
        with ThreadPoolExecutor(max_workers=1) as worker:
            study = worker.submit(self.client.study_work, self.course, self.job, self.info, prepared=prepared)
            try:
                self.assertTrue(wait_entered.wait(2))
                self.assertEqual(1, self.session.count("GET"), "Page fetched before batch completed")
                self.assertEqual(0, self.session.count("POST"))
            finally:
                allow_query.set()
            self.assertEqual(self.Result.SUCCESS, study.result(timeout=2))
        self.assertEqual("fresh-token", self.submit_payload()["token"])

    def test_fresh_question_changes_only_query_missing_keys(self):
        for changed in (self.question("q2", title="Changed title"),
                        self.question("q2", options="B. second\nA. first")):
            with self.subTest(changed=changed):
                self.session.calls.clear()
                self.client.tiku.query.reset_mock()
                self.start()
                old = [self.question(), self.question("q2", title="Second")]
                prepared = self.prepare(questions=old)
                self.client._work_prefetch.answers(prepared)
                self.queue_page("fresh-token", [self.question(), changed])
                self.assertEqual(self.Result.SUCCESS, self.client.study_work(
                    self.course, self.job, self.info, prepared=prepared))
                calls = [call.args[0] for call in self.client.tiku.query.call_args_list]
                self.assertEqual([self.key(q) for q in old + [changed]], [self.key(q) for q in calls])
                self.assertEqual("fresh-token", self.submit_payload()["token"])
                self.client.finish_course()

    def test_foreign_work_context_never_reuses_prepared_answer(self):
        for field_name, container in (("courseId", "course"), ("clazzId", "course"),
                                      ("knowledgeid", "info"), ("jobid", "job"),
                                      ("cpi", "info")):
            with self.subTest(field=field_name):
                self.session.calls.clear()
                self.client.tiku.query.reset_mock()
                self.client.tiku.query.side_effect = ["A", "B"]
                self.start()
                prepared = self.prepare()
                self.client._work_prefetch.answers(prepared)
                course, job, info = deepcopy(self.course), deepcopy(self.job), deepcopy(self.info)
                {"course": course, "job": job, "info": info}[container][field_name] = "other"
                self.queue_page("other-token")
                self.assertEqual(self.Result.SUCCESS, self.client.study_work(course, job, info, prepared=prepared))
                self.assertEqual(2, self.client.tiku.query.call_count)
                self.assertEqual("B", self.submit_payload()["answerq1"])
                self.client.finish_course()

    def test_stop_before_study_avoids_page_fetch_and_submission(self):
        self.start()
        self.stop.set()
        self.assertEqual(self.Result.DEFERRED, self.client.study_work(self.course, self.job, self.info))
        self.assertEqual([], self.session.calls)
        self.client.tiku.query.assert_not_called()

    def test_stop_during_synchronous_query_never_posts(self):
        def query(question):
            self.stop.set()
            return "A"
        self.client.tiku.query.side_effect = query
        self.start(enabled=False)
        self.queue_page("fresh-token", [self.question(), self.question("q2", title="Second")])
        self.assertEqual(self.Result.DEFERRED, self.client.study_work(self.course, self.job, self.info))
        self.client.tiku.query.assert_called_once()
        self.assertEqual(0, self.session.count("POST"))

    def test_stop_during_submit_rate_limit_never_posts(self):
        self.start(enabled=False)
        self.queue_page("fresh-token")
        def limit_rate():
            if self.client.tiku.query.called:
                self.stop.set()
            return True
        self.client.rate_limiter.limit_rate.side_effect = limit_rate
        self.assertEqual(self.Result.DEFERRED, self.client.study_work(self.course, self.job, self.info))
        self.assertEqual(0, self.session.count("POST"))

    def test_stopped_prefetch_wait_returns_deferred_without_refresh(self):
        entered, release = threading.Event(), threading.Event()
        def query(question):
            entered.set()
            if not release.wait(2):
                raise AssertionError("Test did not release query")
            return "A"
        self.client.tiku.query.side_effect = query
        self.start()
        prepared = self.prepare()
        try:
            self.assertTrue(entered.wait(2))
            self.stop.set()
            self.assertEqual(self.Result.DEFERRED, self.client.study_work(
                self.course, self.job, self.info, prepared=prepared))
            self.assertEqual(1, self.session.count("GET"))
            self.assertEqual(0, self.session.count("POST"))
        finally:
            release.set()

    def test_disabled_or_absent_provider_never_starts_prefetch(self):
        for provider in (None, SimpleNamespace(DISABLE=True, query=Mock())):
            with self.subTest(provider=provider):
                self.client.tiku = provider
                self.assertIsNone(self.start())
                self.assertIsNone(self.client.prepare_work(self.course, self.job, self.info))
                self.assertEqual([], self.session.calls)
                self.client.finish_course()

    def test_explicit_prefetch_disable_never_fetches_early(self):
        self.assertIsNone(self.start(enabled=False))
        self.assertIsNone(self.client.prepare_work(self.course, self.job, self.info))
        self.assertEqual([], self.session.calls)
        self.client.tiku.query.assert_not_called()

    def test_course_finish_restores_original_request_and_clears_limiter_stops(self):
        original = self.session.request
        manager = self.start()
        self.assertIsNot(original, self.session.request)
        self.assertIs(self.stop, self.client.rate_limiter.stop_event)
        self.client.finish_course()
        self.assertIs(original, self.session.request)
        self.assertIsNone(self.client.rate_limiter.stop_event)
        self.assertIsNone(self.client.video_log_limiter.stop_event)
        with self.assertRaises(CancelledError):
            manager.prepare("after-close", [])
        self.client.finish_course()
        self.assertIs(original, self.session.request)

    def test_course_finish_restores_request_even_if_prefetch_close_raises(self):
        original = self.session.request
        self.start(enabled=False)
        self.client._work_prefetch = SimpleNamespace(close=Mock(side_effect=RuntimeError("close failed")))
        with self.assertRaisesRegex(RuntimeError, "close failed"):
            self.client.finish_course()
        self.assertIs(original, self.session.request)
        self.assertIsNone(self.client.rate_limiter.stop_event)

    def test_course_finish_preserves_newer_request_wrapper(self):
        self.start(enabled=False)
        newer = Mock()
        self.session.request = newer
        self.client.finish_course()
        self.assertIs(newer, self.session.request)

    def test_explicit_rejections_stop_without_retry_or_cookie_clear(self):
        responses = [FakeResponse(status=403), FakeResponse(status=429),
                     FakeResponse(url="https://passport2.chaoxing.com/login"),
                     FakeResponse(text="<html>请输入验证码</html>", content_type="text/html"),
                     FakeResponse(text="<html>安全验证</html>", content_type="text/html")]
        for response in responses:
            with self.subTest(status=response.status_code, body=response.text):
                self.stop.clear()
                self.session.calls.clear()
                original = self.session.request
                self.start(enabled=False)
                self.session.responses.append(response)
                with self.assertRaises(ProgressError):
                    self.session.get("https://platform.invalid/work")
                self.assertTrue(self.stop.is_set())
                with self.assertRaises(ProgressError):
                    self.session.post("https://platform.invalid/submit")
                self.assertEqual(1, len(self.session.calls))
                self.session.cookies.clear.assert_not_called()
                self.client.finish_course()
                self.assertIs(original, self.session.request)

    def test_normal_question_text_about_verification_does_not_stop(self):
        self.start(enabled=False)
        responses = [
            FakeResponse({"question": "请输入验证码与安全验证有什么区别？"}),
            FakeResponse(text="<html><body><p>题目：为什么要显示请输入验证码？</p></body></html>",
                         content_type="text/html"),
            FakeResponse(text="<html><body><p>题目：安全验证的用途是什么？</p></body></html>",
                         content_type="text/html"),
        ]
        for response in responses:
            with self.subTest(body=response.text):
                self.session.responses.append(response)
                self.assertIs(response, self.session.get("https://platform.invalid/work"))
                self.assertFalse(self.stop.is_set())
        self.assertEqual(len(responses), len(self.session.calls))
        self.session.cookies.clear.assert_not_called()


class ProcessorLifecycleTests(unittest.TestCase):
    def processor(self, thread_factory):
        namespace = load_units("chaoxing/main.py", ["ChapterResult", "ChapterTask",
            "JobProcessor.run", "JobProcessor.worker_thread", "JobProcessor.retry_thread"], {
            "enum": enum, "dataclass": dataclass, "Any": Any, "__name__": __name__,
            "log_error": lambda function: function, "logger": Mock(),
            "threading": SimpleNamespace(Thread=thread_factory),
            "ProgressError": ProgressError, "ShutDown": type("ShutDown", (Exception,), {}),
        })
        processor = namespace["JobProcessor"]()
        processor.tasks = [namespace["ChapterTask"](0, {"title": "Offline chapter"})]
        processor.worker_num = 2
        processor.stop_event = threading.Event()
        processor.config = {"prefetch_work": True}
        processor.threads = []
        processor.task_queue = PriorityQueue()
        processor.retry_queue = PriorityQueue()
        processor._stop_task = namespace["ChapterTask"](-1, {})
        processor.chaoxing = SimpleNamespace(start_course=Mock(), finish_course=Mock())
        return processor

    def test_course_start_failure_still_finishes_without_starting_threads(self):
        factory = Mock(side_effect=AssertionError("Thread created before course startup"))
        processor = self.processor(factory)
        processor.chaoxing.start_course.side_effect = RuntimeError("start failed")
        with self.assertRaisesRegex(RuntimeError, "start failed"):
            processor.run()
        processor.chaoxing.finish_course.assert_called_once()
        factory.assert_not_called()
        self.assertTrue(processor.stop_event.is_set())
        self.assertEqual([], processor.threads)

    def test_partial_thread_start_failure_joins_started_worker_before_finish(self):
        created = []
        def factory(*, target):
            if created:
                return SimpleNamespace(start=Mock(side_effect=RuntimeError("thread start failed")))
            thread = threading.Thread(target=target, name="offline-partial-start")
            created.append(thread)
            return thread
        processor = self.processor(factory)
        alive_at_finish = []
        processor.chaoxing.finish_course.side_effect = lambda: alive_at_finish.append(
            any(thread.is_alive() for thread in created))
        try:
            with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                processor.run()
            processor.chaoxing.finish_course.assert_called_once()
            self.assertEqual([False], alive_at_finish)
            self.assertEqual(1, len(created))
            self.assertFalse(created[0].is_alive())
            self.assertTrue(processor.stop_event.is_set())
        finally:
            # Make the regression safe even if production forgets to drain its worker.
            processor.stop_event.set()
            for thread in created:
                if thread.is_alive():
                    processor.task_queue.put(processor._stop_task)
                thread.join(2)
                self.assertFalse(thread.is_alive(), "Leaked partial-start worker")


class RateLimiterIntegrationTests(unittest.TestCase):
    def make_limiter(self, ticks):
        clock = SimpleNamespace(monotonic=Mock(side_effect=ticks), sleep=Mock(),
                                time=Mock(side_effect=AssertionError("Wall clock must not be used")))
        limiter_type = load_units("chaoxing/api/base.py", ["RateLimiter"], {
            "time": clock, "threading": threading, "random": SimpleNamespace(uniform=Mock(return_value=0.25)),
        })["RateLimiter"]
        return limiter_type(2), clock

    def test_monotonic_wait_and_reservations_preserve_spacing(self):
        limiter, clock = self.make_limiter([100, 101, 101])
        self.assertTrue(limiter.limit_rate())
        self.assertTrue(limiter.limit_rate())
        self.assertEqual([1, 3], [call.args[0] for call in clock.sleep.call_args_list])
        self.assertEqual(104, limiter.last_call)
        clock.time.assert_not_called()

    def test_elapsed_interval_never_sleeps_negative_duration(self):
        limiter, clock = self.make_limiter([100, 110])
        self.assertTrue(limiter.limit_rate())
        clock.sleep.assert_called_once_with(0)

    def test_random_delay_is_added_to_monotonic_spacing(self):
        limiter, clock = self.make_limiter([100, 101])
        self.assertTrue(limiter.limit_rate(random_time=True))
        clock.sleep.assert_called_once_with(1.25)

    def test_prestopped_limiter_does_not_reserve_or_sleep(self):
        limiter, clock = self.make_limiter([100])
        stop = threading.Event()
        stop.set()
        self.assertFalse(limiter.limit_rate(stop_event=stop))
        self.assertEqual(100, limiter.last_call)
        clock.sleep.assert_not_called()
        self.assertEqual(1, clock.monotonic.call_count)

    def test_wait_cancellation_uses_event_instead_of_sleep(self):
        limiter, clock = self.make_limiter([100, 101])
        stop = Mock(is_set=Mock(return_value=False), wait=Mock(return_value=True))
        limiter.stop_event = stop
        self.assertFalse(limiter.limit_rate())
        stop.wait.assert_called_once_with(1)
        clock.sleep.assert_not_called()

    def test_explicit_event_overrides_course_event(self):
        limiter, clock = self.make_limiter([100, 101])
        limiter.stop_event = threading.Event()
        limiter.stop_event.set()
        explicit = Mock(is_set=Mock(return_value=False), wait=Mock(return_value=False))
        self.assertTrue(limiter.limit_rate(stop_event=explicit))
        explicit.wait.assert_called_once_with(1)
        clock.sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
