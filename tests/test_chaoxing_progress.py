"""Offline regressions for media timing, independent confirmation and cross-day state."""
import contextlib
import functools
import enum
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from tests.source_units import ROOT, load_units


Result = load_units('chaoxing/api/base.py', ['StudyResult'], {'Enum': enum.Enum})['StudyResult']


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    time = monotonic

    def sleep(self, seconds):
        self.now += seconds
        if self.now > 500:
            raise AssertionError('Unbounded media loop')


class MediaTimingTests(unittest.TestCase):
    def run_media(self, *, speed=1, bookmark=0, confirmed=True, replies=None,
                  duration=3, interval=1, monitor_factory=None):
        clock = Clock()
        session = Mock()
        session.get.return_value.json.return_value = {
            'status': 'success', 'dtoken': 'synthetic', 'duration': duration, 'crc': '', 'key': ''}
        env = {'SessionManager': SimpleNamespace(get_session=lambda: session),
               'gc': SimpleNamespace(VIDEO_HEADERS={}, AUDIO_HEADERS={}, POLL_INTERVAL=1),
               'time': clock, 'random': SimpleNamespace(uniform=lambda a, b: a),
               'logger': Mock(), 'StudyResult': Result, 'math': math,
               'requests': requests, 'RequestException': requests.RequestException,
               '_draw_progress_bar': Mock(), '_wipe_bar': Mock(),
               'OnlineDetectionError': RuntimeError, 'ProgressError': ValueError}
        cx = load_units('chaoxing/api/base.py', ['Chaoxing.study_video'], env)['Chaoxing']()
        cx.get_fid = lambda: 1
        cx.confirm_video_completion = Mock(return_value=confirmed)
        cx._recover_after_forbidden = Mock(return_value=None)
        cx.create_online_monitor = Mock(return_value=Mock(enabled=True))
        if monitor_factory is not None:
            cx.create_online_monitor.side_effect = lambda *args: monitor_factory(clock, cx)
        positions = []

        def report(*args, **kwargs):
            positions.append((args[6], clock.now, kwargs.get('_isdrag', 3)))
            if replies is not None:
                return replies.pop(0) if replies else (False, 200)
            return args[6] >= duration, 200

        cx.video_progress_log = Mock(side_effect=report)
        result = cx.study_video({}, {'objectid': 'synthetic', 'name': 'media',
                                     'jobid': 'job', 'playTime': bookmark},
                                {'reportTimeInterval': interval}, _speed=speed)
        return result, positions, cx, env

    def test_new_video_never_reports_full_duration_at_start(self):
        result, positions, _, _ = self.run_media()
        self.assertEqual(positions[0][0], 0)
        self.assertTrue(all(position <= elapsed for position, elapsed, _ in positions))
        self.assertEqual(result, Result.SUCCESS)

    def test_configured_speed_does_not_fabricate_elapsed_time(self):
        _, positions, _, _ = self.run_media(speed=2)
        self.assertTrue(all(position <= elapsed for position, elapsed, _ in positions))

    def test_http_pass_requires_independent_task_card_confirmation(self):
        result, _, cx, _ = self.run_media(confirmed=False)
        self.assertNotEqual(result, Result.SUCCESS)
        cx.confirm_video_completion.assert_called()

    def test_unknown_confirmation_is_not_success(self):
        result, _, _, _ = self.run_media(confirmed=None)
        self.assertNotEqual(result, Result.SUCCESS)

    def test_end_bookmark_without_pass_does_not_instantly_replay_or_finish(self):
        result, positions, _, _ = self.run_media(bookmark=3000, confirmed=False)
        self.assertEqual(result, Result.DEFERRED)
        self.assertEqual(positions, [])

    def test_unconfirmed_end_reports_are_bounded_and_bar_is_cleaned(self):
        result, _, _, env = self.run_media(replies=[(False, 200)] * 20)
        self.assertEqual(result, Result.TIMEOUT)
        env['_wipe_bar'].assert_called_with('media')

    def test_initial_forbidden_stops_without_replaying_as_audio(self):
        result, positions, _, _ = self.run_media(replies=[(False, 403)])
        self.assertEqual(result, Result.FORBIDDEN)
        self.assertEqual(len(positions), 1)

    def test_invalid_speed_stops_without_any_progress_write(self):
        for speed in (float('nan'), float('inf'), 0, -1, 'invalid'):
            with self.subTest(speed=speed):
                result, positions, _, _ = self.run_media(speed=speed)
                self.assertEqual(result, Result.ERROR)
                self.assertEqual(positions, [])

    def test_initial_online_failure_prevents_first_video_write(self):
        captured = []

        def factory(clock, cx):
            captured.append(cx)
            raise RuntimeError('synthetic online rejection')

        with self.assertRaises(ValueError):
            self.run_media(monitor_factory=factory)
        captured[0].video_progress_log.assert_not_called()
        self.assertTrue(captured[0]._online_detection_failed)

    def test_online_failure_at_thirty_seconds_stops_before_next_progress_report(self):
        captured = []

        def factory(clock, cx):
            captured.append(cx)
            monitor = Mock(enabled=True)

            def check():
                if clock.now >= 30:
                    raise RuntimeError('synthetic online rejection')

            monitor.check.side_effect = check
            return monitor

        with self.assertRaises(ValueError):
            self.run_media(duration=90, interval=60, monitor_factory=factory)
        self.assertEqual(captured[0].video_progress_log.call_count, 1)
        captured[0].confirm_video_completion.assert_not_called()
        self.assertTrue(captured[0]._online_detection_failed)


class SessionReuseTests(unittest.TestCase):
    def test_get_session_preserves_runtime_cookies_and_connection_pool(self):
        session = Mock(headers={}, cookies={})
        factory = Mock(return_value=session)
        env = {'threading': threading, 'requests': SimpleNamespace(Session=factory),
               'functools': functools, 'HTTPAdapter': Mock(),
               'gc': SimpleNamespace(HEADERS={}), 'use_cookies': lambda: {'saved': 'synthetic'}}
        manager = load_units('chaoxing/api/base.py', ['SessionManager'], env)['SessionManager']
        first = manager.get_session()
        first.cookies['runtime'] = 'new-cookie'
        second = manager.get_session()
        self.assertIs(first, second)
        factory.assert_called_once()
        self.assertEqual(second.cookies['runtime'], 'new-cookie')


class ConfirmationRequestTests(unittest.TestCase):
    def test_confirmation_uses_only_exact_card_get_without_empty_page_write(self):
        response = Mock(status_code=200, text='synthetic')
        session = Mock()
        session.get.return_value = response
        decode = Mock(return_value=True)
        env = {'SessionManager': SimpleNamespace(get_session=lambda: session),
               'decode_media_completion': decode, 'RequestException': requests.RequestException}
        cx = load_units('chaoxing/api/base.py', ['Chaoxing.confirm_video_completion'], env)['Chaoxing']()
        cx.rate_limiter = Mock()
        course = {'courseId': 'course', 'clazzId': 'class', 'cpi': 'cpi'}
        job = {'jobid': 'job', 'objectid': 'obj', 'cardnum': '1'}
        self.assertIs(cx.confirm_video_completion(course, job, {'knowledgeid': 'point'}), True)
        session.post.assert_not_called()
        params = session.get.call_args.kwargs['params']
        self.assertEqual(params['num'], '1')
        self.assertEqual(params['knowledgeid'], 'point')
        decode.assert_called_once_with('synthetic', job)

    def test_progress_response_string_false_is_never_truthy_success(self):
        session = Mock()
        session.get.return_value.status_code = 200
        session.get.return_value.json.return_value = {'isPassed': 'false'}
        env = {'gc': SimpleNamespace(VIDEO_HEADERS={}), 'get_timestamp': lambda: '0',
               'logger': Mock(), 're': __import__('re')}
        cx = load_units('chaoxing/api/base.py', ['Chaoxing.video_progress_log'], env)['Chaoxing']()
        cx.video_log_limiter = Mock()
        cx.get_enc, cx.get_uid = Mock(return_value='synthetic'), Mock(return_value='uid')
        course = {'clazzId': 'class', 'courseId': 'course', 'cpi': 'cpi'}
        job = {'jobid': 'job', 'objectid': 'obj', 'otherinfo': '', 'rt': 1,
               'videoFaceCaptureEnc': '', 'attDuration': '', 'attDurationEnc': ''}
        result, status = cx.video_progress_log(session, course, job, {}, 'token', 30, 0, headers={})
        self.assertIs(result, False)
        self.assertEqual(status, 422)


class CompletionDecoderTests(unittest.TestCase):
    def decode(self, html):
        env = {'json': json, 're': __import__('re')}
        decode = load_units('chaoxing/api/decode.py', ['decode_media_completion'], env)['decode_media_completion']
        return decode(html, {'jobid': 'job', 'objectid': 'obj'})

    def card(self, passed):
        return 'var mArg = ' + json.dumps({'attachments': [
            {'type': 'video', 'jobid': 'job', 'objectId': 'obj', 'isPassed': passed}]}) + ';'

    def test_matching_explicit_boolean_is_required(self):
        self.assertIs(self.decode(self.card(True)), True)
        self.assertIs(self.decode(self.card(False)), False)
        for invalid in ('false', 'true', 1, None):
            self.assertIsNone(self.decode(self.card(invalid)))

    def test_login_empty_malformed_and_different_task_are_unknown(self):
        for html in ('login', 'mArg={bad};', 'mArg={"attachments": []};',
                     self.card(True).replace('"job"', '"other-job"')):
            self.assertIsNone(self.decode(html))


def load_progress():
    spec = importlib.util.spec_from_file_location('offline_progress', ROOT / 'chaoxing/api/progress.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CrossDayTests(unittest.TestCase):
    def test_cross_day_regression_stops_and_preserves_last_confirmed_snapshot(self):
        module = load_progress()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            store = module.ProgressStore(path)
            course = {'courseId': 'course', 'clazzId': 'class', 'cpi': 'cpi'}
            store.check_and_record('account', course, [{'id': '1', 'has_finished': True}])
            previous = path.read_bytes()
            with self.assertRaises(module.ProgressError):
                module.ProgressStore(path).check_and_record('account', course,
                                                           [{'id': '1', 'has_finished': False}])
            self.assertEqual(path.read_bytes(), previous)

    def test_accounts_and_classes_are_isolated_and_no_raw_identifiers_saved(self):
        module = load_progress()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            store = module.ProgressStore(path)
            course = {'courseId': 'course-private', 'clazzId': 'class-private', 'cpi': 'cpi-private'}
            store.check_and_record('account-private', course, [{'id': 'chapter-private', 'has_finished': True}])
            store.check_and_record('other-account', course, [{'id': 'chapter-private', 'has_finished': False}])
            store.check_and_record('account-private', dict(course, clazzId='other-class'),
                                   [{'id': 'chapter-private', 'has_finished': False}])
            self.assertNotIn('private', path.read_text())

    def test_invalid_response_and_corrupt_state_never_overwrite_evidence(self):
        module = load_progress()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            path.write_text('{broken')
            with self.assertRaises(module.ProgressError):
                module.ProgressStore(path).check_and_record('a', {'courseId': 'c', 'clazzId': 'b', 'cpi': 'p'},
                                                           [{'id': '1', 'has_finished': True}])
            self.assertEqual(path.read_text(), '{broken')

    def test_empty_response_and_removed_chapter_preserve_valid_snapshot(self):
        module = load_progress()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            store = module.ProgressStore(path)
            course = {'courseId': 'c', 'clazzId': 'b', 'cpi': 'p'}
            store.check_and_record('a', course, [{'id': '1', 'has_finished': True},
                                               {'id': '2', 'has_finished': False}])
            previous = path.read_bytes()
            for points in ([], [{'id': '1', 'has_finished': True}]):
                with self.assertRaises(module.ProgressError):
                    store.check_and_record('a', course, points)
                self.assertEqual(path.read_bytes(), previous)

    def test_atomic_replace_failure_preserves_original_and_cleans_temporary(self):
        module = load_progress()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.json'
            store = module.ProgressStore(path)
            course = {'courseId': 'c', 'clazzId': 'b', 'cpi': 'p'}
            store.check_and_record('a', course, [{'id': '1', 'has_finished': False}])
            previous = path.read_bytes()
            with patch.object(module.os, 'replace', side_effect=PermissionError()):
                with self.assertRaises(module.ProgressError):
                    store.check_and_record('a', course, [{'id': '1', 'has_finished': True}])
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(Path(tmp).iterdir()), [path])


class CourseRecheckTests(unittest.TestCase):
    def env(self, *, before=True, after=True):
        result = load_units('chaoxing/main.py', ['ChapterResult'], {'enum': enum})['ChapterResult']
        cx = Mock()
        cx.get_uid.return_value = 'account'
        point = {'id': '1', 'title': 'chapter', 'has_finished': before}
        cx.get_course_point.side_effect = [{'points': [point]},
                                           {'points': [dict(point, has_finished=after)]}]
        store = Mock()

        class Processor:
            def __init__(self, cx, course, tasks, config):
                self.tasks, self.failed_tasks = tasks, []
                self.deferred_tasks, self.progress_error = [], None

            def run(self):
                for task in self.tasks:
                    task.result = result.SUCCESS

        env = {'logger': Mock(), 'ProgressStore': lambda: store, 'JobProcessor': Processor,
               'ChapterResult': result,
               'ChapterTask': lambda point, index: SimpleNamespace(point=point, index=index),
               'print_course_tree': Mock()}
        run = load_units('chaoxing/main.py', ['process_course'], env)['process_course']
        return run, cx, store, result

    def test_final_unconfirmed_chapter_is_not_announced_as_complete(self):
        run, cx, store, result = self.env(before=False, after=False)
        p = run(cx, {'courseId': 'c', 'clazzId': 'b', 'cpi': 'p', 'title': 'course'}, {})
        self.assertEqual(len(p.failed_tasks), 1)
        self.assertEqual(p.tasks[0].result, result.ERROR)
        self.assertEqual(store.check_and_record.call_count, 2)

    def test_rollback_aborts_before_scheduler_or_any_study_writes(self):
        run, cx, store, _ = self.env()
        store.check_and_record.side_effect = ValueError('regression')
        with self.assertRaisesRegex(ValueError, 'regression'):
            run(cx, {'courseId': 'c', 'clazzId': 'b', 'cpi': 'p', 'title': 'course'}, {})
        cx.get_job_list.assert_not_called()
        cx.study_video.assert_not_called()

    def test_confirmed_course_is_checked_before_and_after(self):
        run, cx, store, _ = self.env()
        p = run(cx, {'courseId': 'c', 'clazzId': 'b', 'cpi': 'p', 'title': 'course'}, {})
        self.assertEqual(p.failed_tasks, [])
        self.assertEqual(store.check_and_record.call_count, 2)
