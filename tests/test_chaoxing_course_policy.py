"""Offline regression for independently guarded course-directory reads."""
import enum
import importlib.util
import re
import threading
import unittest
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import Mock

from requests.cookies import RequestsCookieJar

from tests.source_units import ROOT, load_units


_SPEC = importlib.util.spec_from_file_location(
    "course_request_policy_fixture", ROOT / "chaoxing/api/request_policy.py")
_policy = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_policy)


def response(status=200, points=None, *, challenge=False, set_marker=False):
    return SimpleNamespace(
        status_code=status,
        url="https://example.invalid/course",
        headers={"Content-Type": "text/html"},
        text="<title>安全验证</title>" if challenge else "<div>course</div>",
        point_list={"points": [] if points is None else points},
        set_marker=set_marker,
    )


class FakeSession:
    def __init__(self):
        self.cookies = RequestsCookieJar()
        # Synthetic values, with a domain in the real cleanup allowlist.
        self.cookies.set("JSESSIONID", "synthetic-session", domain="mooc1.chaoxing.com", path="/")
        self.cookies.set("metadata_mark", "synthetic-marker", domain="mooc1.chaoxing.com", path="/")
        self.responses = deque()
        self.request = Mock(side_effect=self._send)

    def _send(self, method, url, **kwargs):
        if not self.responses:
            raise AssertionError("Unexpected extra request; no retry is permitted")
        result = self.responses.popleft()
        if result.set_marker:
            self.cookies.set("metadata_mark", "synthetic-new-marker", domain="mooc1.chaoxing.com", path="/")
        return result

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


class FakeProcessor:
    """Complete fake chapter work, optionally verifying inside its own guard."""

    def __init__(self, client, course, tasks, config, results, verify_during_run):
        self.client = client
        self.tasks = tasks
        self.config = dict(config)
        self.speed = config["speed"]
        self.worker_num = min(config["jobs"], len(tasks))
        self.results = results
        self.verify_during_run = verify_during_run
        self.failed_tasks = []
        self.deferred_tasks = []
        self.progress_error = None
        self.verify_completion = None

    def run(self):
        if not self.tasks:
            return
        self.client.start_course(self.worker_num, threading.Event(), prefetch_work=False)
        try:
            for task in self.tasks:
                if self.verify_during_run and not self.verify_completion(task.point):
                    task.result = self.results.DEFERRED
                    self.deferred_tasks.append(task)
                else:
                    task.result = self.results.SUCCESS
        finally:
            self.client.finish_course()


class CourseRequestPolicyTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.original_request = self.session.request
        self.storage = {"chaoxing": self.session.cookies.get_dict(), "other": {"keep": "synthetic"}}
        self.persist = Mock(side_effect=self._persist_session)
        self.provider_worker = Mock()
        self.pacers = []
        self.cancel_pacing = False
        self.ProgressError = load_units("chaoxing/api/progress.py", ["ProgressError"])["ProgressError"]
        self.pacer_factory = Mock(side_effect=self._pacer)
        env = load_units(
            "chaoxing/api/base.py",
            ["Chaoxing.start_course", "Chaoxing.finish_course",
             "Chaoxing._check_platform_response", "Chaoxing._cleanup_cookie_markers"],
            {"logger": Mock(), "re": re, "ProgressError": self.ProgressError,
             "RequestPacer": self.pacer_factory, "RequestActivity": _policy.RequestActivity,
             "validate_request_settings": _policy.validate_request_settings,
             "clear_cookie_markers": _policy.clear_cookie_markers,
             "SessionManager": SimpleNamespace(get_session=lambda: self.session),
             "WorkPrefetcher": self.provider_worker, "save_cookies": self.persist},
        )
        self.client = env["Chaoxing"]()
        self.client.kwargs = {
            "request_interval": 0.5, "request_jitter": 0.2,
            "cookie_cleanup_names": ["metadata_mark"], "speed": 1.75, "jobs": 7,
        }
        self.client.tiku = SimpleNamespace(DISABLE=False, query=Mock(), query_delay=0)
        self.client._study_stop_event = None
        self.client._work_prefetch = None
        self.client._course_session = None
        self.client._request_pacer = None
        self.client._course_requests = None
        self.client._cookie_cleanup_names = ()
        self.client._cookie_cleanup_done = False
        self.client._cookie_cleanup_pending = False
        self.client.rate_limiter = SimpleNamespace(stop_event=None)
        self.client.video_log_limiter = SimpleNamespace(stop_event=None)
        self.client.start_course = Mock(wraps=self.client.start_course)
        self.client.finish_course = Mock(wraps=self.client.finish_course)
        self.decode = Mock(side_effect=lambda result: result.point_list)
        self.client.get_course_point = Mock(side_effect=self._read_points)
        self.client.get_uid = Mock(return_value="synthetic-account")
        self.progress = Mock()
        self.progress_factory = Mock(return_value=self.progress)
        self.verify_during_run = False
        self.processor = None
        self.processor_factory = Mock(side_effect=self._processor)
        main = load_units(
            "chaoxing/main.py",
            ["ChapterResult", "ChapterTask", "_get_course_point_with_policy", "process_course"],
            {"enum": enum, "dataclass": dataclass, "threading": threading,
             "logger": Mock(), "ProgressStore": self.progress_factory,
             "JobProcessor": self.processor_factory, "print_course_tree": Mock()},
        )
        self.results = main["ChapterResult"]
        self.helper = main["_get_course_point_with_policy"]
        self.process_course = main["process_course"]
        self.course = {"courseId": "c", "clazzId": "b", "cpi": "p", "title": "synthetic"}
        self.config = {"speed": 1.75, "jobs": 7, "prefetch_work": True, "tree_view": False}

    def _persist_session(self, session):
        if self.client._course_requests is not None:
            self.assertEqual(self.client._course_requests._active, 0)
        self.storage["chaoxing"] = session.cookies.get_dict()

    def _pacer(self, interval, jitter):
        def wait(stop):
            if self.cancel_pacing:
                stop.set()
                return False
            return True
        pacer = SimpleNamespace(wait=Mock(side_effect=wait))
        self.pacers.append(pacer)
        return pacer

    def _read_points(self, course_id, clazz_id, cpi):
        result = self.session.get("https://example.invalid/course",
                                  params={"courseId": course_id, "clazzId": clazz_id, "cpi": cpi})
        return self.decode(result)

    def _processor(self, client, course, tasks, config):
        self.processor = FakeProcessor(client, course, tasks, config, self.results, self.verify_during_run)
        return self.processor

    def assert_released(self):
        self.assertIs(self.session.request, self.original_request)
        self.assertIsNone(self.client._course_session)
        self.assertIsNone(self.client._request_pacer)
        self.assertIsNone(self.client._course_requests)
        self.assertIsNone(self.client.rate_limiter.stop_event)
        self.assertIsNone(self.client.video_log_limiter.stop_event)
        self.assertTrue(self.client._study_stop_event.is_set())
        self.assertEqual(self.session.cookies.get("JSESSIONID"), "synthetic-session")
        self.assertEqual(self.storage["other"], {"keep": "synthetic"})

    def test_independent_reads_use_fresh_events_without_changing_custom_settings(self):
        first, second = response(), response()
        self.session.responses.extend([first, second])
        original_settings = dict(self.client.kwargs)
        self.assertIs(self.helper(self.client, self.course), first.point_list)
        self.assertIs(self.helper(self.client, self.course), second.point_list)
        calls = self.client.start_course.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertIsNot(calls[0].args[1], calls[1].args[1])
        for call in calls:
            self.assertEqual(call.args[0], 1)
            self.assertEqual(call.kwargs, {"prefetch_work": False})
            self.assertTrue(call.args[1].is_set())
        self.assertEqual(self.client.kwargs, original_settings)
        self.assertEqual(self.client.get_course_point.call_args.args, ("c", "b", "p"))
        self.assertEqual(self.client.finish_course.call_count, 2)
        self.assertEqual(self.pacer_factory.call_args_list[0].args, (0.5, 0.2))
        self.assertEqual([p.wait.call_count for p in self.pacers], [1, 1])
        self.provider_worker.assert_not_called()
        self.assert_released()

    def test_initial_rejection_stops_before_progress_or_workers_and_persists_cleanup(self):
        self.session.responses.extend([response(403, set_marker=True), response()])
        with self.assertRaises(self.ProgressError):
            self.process_course(self.client, self.course, self.config)
        self.assertEqual(self.original_request.call_count, 1)
        self.assertEqual(len(self.session.responses), 1)
        self.progress_factory.assert_not_called()
        self.processor_factory.assert_not_called()
        self.decode.assert_not_called()
        self.assertEqual(self.persist.call_count, 2)
        self.assertNotIn("metadata_mark", self.storage["chaoxing"])
        self.assert_released()

    def test_initial_200_challenge_does_not_decode_as_an_empty_course(self):
        self.session.responses.append(response(challenge=True))
        with self.assertRaises(self.ProgressError):
            self.process_course(self.client, self.course, self.config)
        self.decode.assert_not_called()
        self.progress_factory.assert_not_called()
        self.processor_factory.assert_not_called()
        self.assertEqual(self.original_request.call_count, 1)
        self.assert_released()

    def test_final_rejection_preserves_task_flags_and_does_not_retry(self):
        pending = [{"id": "1", "title": "one", "has_finished": False}]
        self.session.responses.extend([response(points=pending), response(403, set_marker=True), response()])
        with self.assertRaises(self.ProgressError):
            self.process_course(self.client, self.course, self.config)
        self.assertEqual(self.original_request.call_count, 2)
        self.assertEqual(len(self.session.responses), 1)
        self.assertEqual(self.processor.tasks[0].result, self.results.SUCCESS)
        self.assertEqual(self.processor.failed_tasks, [])
        self.assertEqual(self.progress.check_and_record.call_count, 1)
        self.assertEqual(self.persist.call_count, 2)
        self.assert_released()

    def test_verification_reuses_active_guard_and_keeps_custom_speed_and_jobs(self):
        pending = [{"id": "1", "title": "one", "has_finished": False}]
        complete = [dict(pending[0], has_finished=True)]
        self.verify_during_run = True
        self.session.responses.extend([response(points=pending), response(points=complete), response(points=complete)])
        original_config = dict(self.config)
        processor = self.process_course(self.client, self.course, self.config)
        self.assertEqual(processor.tasks[0].result, self.results.SUCCESS)
        self.assertEqual(processor.speed, 1.75)
        self.assertEqual(processor.config["jobs"], 7)
        self.assertEqual(self.config, original_config)
        self.assertEqual(self.client.start_course.call_count, 3)
        self.assertEqual(self.client.finish_course.call_count, 3)
        self.assertEqual(self.original_request.call_count, 3)
        self.assertEqual([p.wait.call_count for p in self.pacers], [1, 1, 1])
        self.assertEqual(self.progress.check_and_record.call_count, 3)
        self.assert_released()

    def test_empty_course_has_guarded_reads_but_no_answer_worker(self):
        self.session.responses.extend([response(), response()])
        processor = self.process_course(self.client, self.course, self.config)
        self.assertEqual(processor.tasks, [])
        self.assertEqual(self.client.start_course.call_count, 2)
        self.assertEqual(self.original_request.call_count, 2)
        self.provider_worker.assert_not_called()
        self.assert_released()

    def test_empty_names_preserve_all_cookies_on_explicit_rejections(self):
        self.client.kwargs["cookie_cleanup_names"] = []
        for status in (401, 403, 429):
            with self.subTest(status=status):
                self.session.responses.append(response(status))
                with self.assertRaises(self.ProgressError):
                    self.helper(self.client, self.course)
                self.assertEqual(self.session.cookies.get("metadata_mark"), "synthetic-marker")
                self.assert_released()
        self.persist.assert_not_called()
        self.assertEqual(self.original_request.call_count, 3)

    def test_failed_start_does_not_finish_another_owner(self):
        owner = threading.Event()
        self.client.start_course(1, owner, prefetch_work=False)
        guard = self.session.request
        try:
            with self.assertRaisesRegex(RuntimeError, "上一轮课程尚未收尾"):
                self.helper(self.client, self.course)
            self.client.finish_course.assert_not_called()
            self.assertIs(self.session.request, guard)
            self.assertFalse(owner.is_set())
            self.original_request.assert_not_called()
        finally:
            self.client.finish_course()
        self.assert_released()

    def test_cleanup_persistence_failure_still_restores_guard(self):
        self.session.cookies.clear(domain="mooc1.chaoxing.com", path="/", name="metadata_mark")
        self.persist.side_effect = OSError("Synthetic persistence failure")
        self.session.responses.append(response(403, set_marker=True))
        with self.assertRaisesRegex(OSError, "Synthetic persistence failure"):
            self.helper(self.client, self.course)
        self.assertEqual(self.original_request.call_count, 1)
        self.assertEqual(self.persist.call_count, 1)
        self.assert_released()

    def test_cancelled_pacing_never_dispatches_and_still_restores_guard(self):
        self.cancel_pacing = True
        self.session.responses.append(response())
        with self.assertRaises(self.ProgressError):
            self.helper(self.client, self.course)
        self.original_request.assert_not_called()
        self.decode.assert_not_called()
        self.assertEqual(self.client.finish_course.call_count, 1)
        self.assert_released()


if __name__ == "__main__":
    unittest.main()
