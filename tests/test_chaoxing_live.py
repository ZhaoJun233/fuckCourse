"""Offline live-status/progress regressions, based on the official live card schema."""
import contextlib
import enum
import importlib.util
import io
from concurrent.futures import ThreadPoolExecutor
import sys
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from tests.source_units import ROOT, load_units


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


StudyResult = load_units('chaoxing/api/base.py', ['StudyResult'], {'Enum': enum.Enum})['StudyResult']
session_manager = Mock()
logger = Mock()
with patch.dict(sys.modules, {
    'api.base': SimpleNamespace(SessionManager=session_manager, StudyResult=StudyResult,
                               _draw_progress_bar=Mock(), _wipe_bar=Mock()),
    'api.logger': SimpleNamespace(logger=logger),
    'api.config': SimpleNamespace(GlobalConst=SimpleNamespace(HEADERS={})),
}):
    live_api = load_module('offline_live_api', 'chaoxing/api/live.py')
    with patch.dict(sys.modules, {'api.live': live_api}):
        live_process = load_module('offline_live_process', 'chaoxing/api/live_process.py')


def payload(state=4, review=0, *, duration=120, watched=0, percent=0):
    detail = {'liveStatus': state, 'ifReview': review, 'timeLong': watched * 1000,
              'timeLongValue': watched / 60, 'percentValue': percent,
              'ygdate': '2099-01-01 12:00'}
    if duration is not None:
        detail['duration'] = duration
    return {'status': True, 'temp': {'data': detail}}


class LiveStatusTests(unittest.TestCase):
    def test_confirmed_official_status_mapping_including_reversed_review_flag(self):
        for state, review, expected in ((0, 1, 'NOT_STARTED'), (1, 0, 'LIVING'),
                                       (4, 0, 'REPLAY'), (4, 1, 'ENDED'),
                                       (4, None, 'UNKNOWN'), (8, 0, 'UNKNOWN')):
            with self.subTest(state=state, review=review):
                parsed = live_api.parse_live_status(payload(state, review))
                self.assertEqual(parsed.state.name, expected)

    def test_status_strings_and_server_progress_units(self):
        value = payload('4', '0', duration='3490', watched=3469.313, percent='99.41')
        parsed = live_api.parse_live_status(value)
        self.assertEqual(parsed.duration, 3490)
        self.assertAlmostEqual(parsed.watched_seconds, 3469.313)
        self.assertAlmostEqual(parsed.percent, 99.41)

    def test_rounded_minutes_supported_when_millisecond_field_missing(self):
        value = payload(watched=60)
        del value['temp']['data']['timeLong']
        self.assertEqual(live_api.parse_live_status(value).watched_seconds, 60)

    def test_unstarted_live_has_no_fabricated_duration(self):
        parsed = live_api.parse_live_status(payload(0, 1, duration=None))
        self.assertIsNone(parsed.duration)
        self.assertEqual(parsed.state.name, 'NOT_STARTED')

    def test_invalid_success_envelope_and_numbers_are_not_trusted(self):
        for value in (None, [], {}, {'status': False, 'temp': {'data': {}}},
                      {'status': True, 'temp': []}):
            with self.subTest(value=value), self.assertRaises(live_api.LiveError):
                live_api.parse_live_status(value)
        for field, bad in (('duration', 'not-a-number'), ('duration', -1),
                           ('duration', 'NaN'), ('percentValue', 101),
                           ('percentValue', -1), ('percentValue', True)):
            value = payload()
            value['temp']['data'][field] = bad
            parsed = live_api.parse_live_status(value)
            self.assertIsNone(parsed.duration if field == 'duration' else parsed.percent)


class LiveRequestTests(unittest.TestCase):
    def setUp(self):
        self.session = Mock()
        session_manager.get_session.return_value = self.session
        logger.reset_mock()
        self.attachment = {'jobid': 'JOB_SYNTHETIC', 'property': {
            'title': '测试直播', 'liveId': 'LIVE_SYNTHETIC', 'streamName': 'STREAM_SYNTHETIC',
            'vdoid': 'VIDEO_SYNTHETIC', '_jobid': 'OLD_JOB_SYNTHETIC', 'rt': '0.9'}}
        self.defaults = {'userid': 'USER_SYNTHETIC', 'clazzId': 'CLASS_SYNTHETIC',
                         'knowledgeid': 'POINT_SYNTHETIC'}
        self.live = live_api.Live(self.attachment, self.defaults, 'COURSE_SYNTHETIC')

    def test_status_request_keeps_official_job_id_and_query_encoding(self):
        self.session.get.return_value.json.return_value = payload()
        self.assertEqual(self.live.get_status(), payload())
        args, kwargs = self.session.get.call_args
        self.assertEqual(args[0], 'https://mooc1.chaoxing.com/ananas/live/liveinfo')
        self.assertEqual(kwargs['params']['jobid'], 'JOB_SYNTHETIC')
        self.assertEqual(kwargs['params']['liveid'], 'LIVE_SYNTHETIC')
        self.assertEqual(self.live.required_percent, 90)

    def test_official_live_id_fallback_from_live_job(self):
        del self.attachment['property']['liveId']
        self.attachment['property']['_jobid'] = 'live-SYNTHETIC_LIVE'
        self.session.get.return_value.json.return_value = payload()
        self.live.get_status()
        self.assertEqual(self.session.get.call_args.kwargs['params']['liveid'], 'SYNTHETIC_LIVE')

    def test_missing_required_parameters_never_send_requests(self):
        del self.defaults['userid']
        self.assertIsNone(self.live.get_status())
        self.assertFalse(self.live.do_finish())
        self.session.get.assert_not_called()

    def test_write_failure_and_status_failure_do_not_leak_urls_or_credentials(self):
        self.session.get.side_effect = requests.Timeout('https://private.invalid/?token=SYNTHETIC_SECRET')
        self.assertIsNone(self.live.get_status())
        self.assertFalse(self.live.do_finish())
        self.assertNotIn('SYNTHETIC_SECRET', str(logger.mock_calls))

    def test_required_ratio_uses_official_default_and_rejects_bad_values(self):
        del self.attachment['property']['rt']
        self.assertEqual(self.live.required_percent, 90)
        self.attachment['property']['rt'] = 0.95
        self.assertEqual(self.live.required_percent, 95)
        for value in (0, 1.1, 'NaN', True):
            self.attachment['property']['rt'] = value
            with self.assertRaises(live_api.LiveError):
                _ = self.live.required_percent


class LiveProcessorTests(unittest.TestCase):
    def run_live(self, statuses, *, speed=1, save=True):
        self.live = SimpleNamespace(name='测试直播', required_percent=90,
                                    get_status=Mock(side_effect=statuses), do_finish=Mock(return_value=save))
        self.emit, self.wait, self.draw, self.wipe = Mock(), Mock(), Mock(), Mock()
        self.events = []
        self.emit.side_effect = lambda message: self.events.append(('emit', message))
        self.wipe.side_effect = lambda prefix: self.events.append(('wipe', prefix))
        with patch.object(live_process.time, 'sleep', self.wait), \
             patch.object(live_process, '_draw_progress_bar', self.draw, create=True), \
             patch.object(live_process, '_wipe_bar', self.wipe, create=True):
            return live_process.LiveProcessor.run_live(self.live, speed, emit=self.emit)

    def test_unstarted_living_and_no_replay_stay_pending_without_wait_or_write(self):
        for state, review, label in ((0, 1, '未开始'), (1, 0, '正在直播'), (4, 1, '不允许回放')):
            with self.subTest(state=state):
                self.assertEqual(self.run_live([payload(state, review, duration=None)]), StudyResult.DEFERRED)
                self.live.do_finish.assert_not_called()
                self.wait.assert_not_called()
                self.assertIn(label, str(self.emit.call_args_list))
                self.assertIn('待办', str(self.emit.call_args_list))

    def test_replay_already_at_target_only_displays_server_progress(self):
        self.assertEqual(self.run_live([payload(percent=99.41, watched=119)]), StudyResult.SUCCESS)
        self.live.do_finish.assert_not_called()
        self.wait.assert_not_called()
        self.draw.assert_called()
        self.wipe.assert_called_once()
        self.assertIn('99.41%', str(self.emit.call_args_list))

    def test_replay_completes_only_after_refreshed_server_confirmation(self):
        self.assertEqual(self.run_live([payload(percent=20, watched=24),
                                       payload(percent=90, watched=108)]), StudyResult.SUCCESS)
        self.live.do_finish.assert_called_once()
        self.wait.assert_called_once()
        self.assertEqual(self.live.get_status.call_count, 2)
        self.wipe.assert_called_once()

    def test_progress_cleanup_cannot_erase_the_final_result_message(self):
        self.run_live([payload(percent=99.41)])
        self.assertEqual(self.events[-2][0], 'wipe')
        self.assertEqual(self.events[-1][0], 'emit')
        self.assertIn('平台确认已达到', self.events[-1][1])

    def test_write_success_without_progress_is_not_reported_as_completion(self):
        self.assertEqual(self.run_live([payload()] * 4), StudyResult.ERROR)
        self.assertEqual(self.live.do_finish.call_count, 3)
        self.assertNotIn('平台确认已达到', str(self.emit.call_args_list))

    def test_failed_write_or_refresh_does_not_report_success(self):
        self.assertEqual(self.run_live([payload()], save=False), StudyResult.ERROR)
        self.assertEqual(self.run_live([payload(), None]), StudyResult.ERROR)
        self.wipe.assert_called_once()

    def test_invalid_or_unknown_status_stops_without_writes(self):
        for value in (None, payload(8), payload(review=None), payload(duration=0)):
            with self.subTest(value=value):
                self.assertNotEqual(self.run_live([value]), StudyResult.SUCCESS)
                self.live.do_finish.assert_not_called()

    def test_replay_closed_midway_becomes_pending(self):
        self.assertEqual(self.run_live([payload(), payload(4, 1)]), StudyResult.DEFERRED)
        self.live.do_finish.assert_called_once()

    def test_speed_does_not_fake_live_credit_or_double_divide_duration(self):
        intervals = []
        for speed in (1, 2):
            self.assertEqual(self.run_live([payload(), payload(percent=90)], speed=speed), StudyResult.SUCCESS)
            intervals.append(self.wait.call_args.args[0])
        self.assertEqual(intervals, [59, 59])


class LiveSchedulerTests(unittest.TestCase):
    def test_process_job_propagates_live_result_not_unconditional_success(self):
        processor = Mock()
        env = {'logger': Mock(), 'Live': Mock(), 'LiveProcessor': processor, 'StudyResult': StudyResult}
        process = load_units('chaoxing/main.py', ['process_job'], env)['process_job']
        for result in (StudyResult.SUCCESS, StudyResult.ERROR, StudyResult.DEFERRED):
            processor.run_live.return_value = result
            self.assertEqual(process(Mock(), {'title': '测试课程'}, {'type': 'live', 'jobid': 'TEST'}, {}, 1), result)

    def test_chapter_defers_only_if_other_jobs_succeeded_and_keeps_error_precedence(self):
        chapter_result = load_units('chaoxing/main.py', ['ChapterResult'], {'enum': enum})['ChapterResult']
        for results, expected in (([StudyResult.DEFERRED], chapter_result.DEFERRED),
                                  ([StudyResult.SUCCESS, StudyResult.DEFERRED], chapter_result.DEFERRED),
                                  ([StudyResult.ERROR, StudyResult.DEFERRED], chapter_result.ERROR),
                                  ([StudyResult.SUCCESS], chapter_result.SUCCESS)):
            with self.subTest(results=results):
                cx = Mock()
                jobs = [{'type': 'live'} for _ in results]
                cx.get_job_list.return_value = (jobs, {})
                env = {'logger': Mock(), 'ChapterResult': chapter_result, 'StudyResult': StudyResult,
                       'ThreadPoolExecutor': ThreadPoolExecutor, 'process_job': Mock(side_effect=results)}
                run = load_units('chaoxing/main.py', ['process_chapter'], env)['process_chapter']
                self.assertEqual(run(cx, {}, {'title': '测试章节', 'has_finished': False}, 1), expected)

    def test_pending_summary_never_sends_all_completed_notification(self):
        for deferred, failed in ((1, 0), (0, 1), (1, 1)):
            with self.subTest(deferred=deferred, failed=failed):
                notification = Mock()
                notification.get_notification_from_config.return_value = notification
                cx = Mock()
                cx.get_course_list.return_value = [{'title': '测试课程'}]
                env = {'init_config': lambda: ({}, {}, {}), 'init_chaoxing': lambda *args: cx,
                       'Notification': lambda: notification, 'login_with_retry': Mock(),
                       'filter_courses': lambda *args: cx.get_course_list.return_value,
                       'process_course': Mock(return_value=SimpleNamespace(
                           deferred_tasks=[object()] * deferred, failed_tasks=[object()] * failed)),
                       'logger': Mock(), 'sys': sys, 'traceback': traceback}
                main = load_units('chaoxing/main.py', ['main'], env)['main']
                with contextlib.redirect_stdout(io.StringIO()):
                    main()
                message = notification.send.call_args.args[0]
                self.assertIn(f'{deferred} 个章节有待办', message)
                self.assertNotIn('所有课程学习任务已完成', message)
