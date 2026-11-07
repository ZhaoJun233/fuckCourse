"""Offline tests for chapter concurrency, settings precedence and directory order."""
import argparse
import ast
import contextlib
from dataclasses import dataclass
import enum
import io
import importlib.util
import math
import queue
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.source_units import ROOT, load_units


REQUEST_SPEC = importlib.util.spec_from_file_location(
    "study_settings_request_policy", ROOT / "chaoxing/api/request_policy.py")
REQUEST_POLICY = importlib.util.module_from_spec(REQUEST_SPEC)
REQUEST_SPEC.loader.exec_module(REQUEST_POLICY)


def settings_env(names):
    return load_units('chaoxing/main.py', names,
                      {'argparse': argparse, 'sys': sys, 'math': math, 'logger': Mock(),
                       'validate_request_settings': REQUEST_POLICY.validate_request_settings})


class StudySettingsTests(unittest.TestCase):
    def test_cli_defaults_do_not_override_saved_study_settings(self):
        parse = settings_env(['parse_args'])['parse_args']
        with patch.object(sys, 'argv', ['main.py']):
            args = parse()
        self.assertIsNone(args.jobs)
        self.assertIsNone(args.speed)
        self.assertIsNone(args.prefetch_work)
        self.assertIsNone(args.request_interval)
        self.assertIsNone(args.request_jitter)
        self.assertIsNone(args.cookie_cleanup)

    def test_config_settings_survive_and_explicit_cli_overrides(self):
        env = settings_env(['parse_args', 'validate_study_settings', 'apply_study_settings'])
        common = {'jobs': 4, 'speed': 1.5}
        with patch.object(sys, 'argv', ['main.py']):
            env['apply_study_settings'](common, env['parse_args']())
        self.assertEqual((common['jobs'], common['speed']), (4, 1.5))
        with patch.object(sys, 'argv', ['main.py', '-j', '2', '-s', '1.25']):
            env['apply_study_settings'](common, env['parse_args']())
        self.assertEqual((common['jobs'], common['speed']), (2, 1.25))

    def test_prefetch_config_and_explicit_boolean_flags(self):
        env = settings_env(['parse_args', 'validate_study_settings', 'apply_study_settings'])
        config = {'jobs': 1, 'speed': 1, 'prefetch_work': False}
        for flags, expected in (([], False), (['--prefetch-work'], True),
                                (['--no-prefetch-work'], False)):
            with patch.object(sys, 'argv', ['main.py'] + flags):
                env['apply_study_settings'](config, env['parse_args']())
            self.assertIs(config['prefetch_work'], expected)
        for invalid in ('false', 0, 1, None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                env['validate_study_settings'](dict(config, prefetch_work=invalid))

    def test_invalid_values_fail_instead_of_clamping_or_truncating(self):
        validate = settings_env(['validate_study_settings'])['validate_study_settings']
        for jobs in (0, -1, 17, 1.5, True, 'invalid', float('nan')):
            with self.subTest(jobs=jobs), self.assertRaises(ValueError):
                validate({'jobs': jobs, 'speed': 1})
        for speed in (0, -1, 2.1, True, 'invalid', float('nan'), float('inf')):
            with self.subTest(speed=speed), self.assertRaises(ValueError):
                validate({'jobs': 1, 'speed': speed})

    def test_real_init_config_preserves_stored_values(self):
        env = settings_env(['parse_args', 'validate_study_settings', 'apply_study_settings', 'init_config'])
        common = {'jobs': 4, 'speed': 1.5}
        env['load_config_from_root'] = lambda: (common, {}, {})
        with patch.object(sys, 'argv', ['main.py']):
            actual, _, _ = env['init_config']()
        self.assertEqual((actual['jobs'], actual['speed']), (4, 1.5))

    def test_interactive_settings_retry_and_keep_blank_defaults(self):
        env = settings_env(['parse_args', 'validate_study_settings', 'apply_study_settings'])
        common = {'jobs': 4, 'speed': 1.25}
        with patch.object(sys, 'argv', ['main.py', '--study-settings']), \
                patch('builtins.input', side_effect=['0', '', 'nan', '2']), \
                contextlib.redirect_stdout(io.StringIO()):
            env['apply_study_settings'](common, env['parse_args']())
        self.assertEqual((common['jobs'], common['speed']), (4, 2))

    def test_launcher_prompts_only_for_chapter_mode(self):
        env = load_units('main.py', ['run_chaoxing', 'run_chaoxing_exam'],
                         {'_run': Mock(), 'CHAOXING_DIR': 'synthetic'})
        env['run_chaoxing']()
        self.assertIn('--study-settings', env['_run'].call_args.args[1])
        env['run_chaoxing_exam']()
        self.assertNotIn('--study-settings', env['_run'].call_args.args[1])


class RequestSettingsTests(unittest.TestCase):
    def test_default_speed_jobs_and_request_settings_remain_customizable(self):
        tree = ast.parse((ROOT / 'chaoxing/main.py').read_text(encoding='utf-8-sig'))
        assignment = next(node for node in tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == 'CHAOXING_DEFAULTS'
                                  for target in node.targets))
        defaults = ast.literal_eval(assignment.value)['common']
        self.assertEqual((defaults['jobs'], defaults['speed']), (1, 1.0))
        self.assertEqual(defaults['request_interval'], 0.5)
        self.assertEqual(defaults['request_jitter'], 0.2)
        self.assertEqual(defaults['cookie_cleanup_names'], [])

    def test_saved_request_settings_survive_and_cli_can_override_them(self):
        env = settings_env(['parse_args', 'validate_study_settings', 'apply_study_settings'])
        common = {'jobs': 7, 'speed': 1.75, 'prefetch_work': False,
                  'request_interval': 0.8, 'request_jitter': 0.4,
                  'cookie_cleanup_names': ['synthetic_old_marker']}
        expected = dict(common)
        with patch.object(sys, 'argv', ['main.py']):
            env['apply_study_settings'](common, env['parse_args']())
        self.assertEqual(common, expected)
        flags = ['main.py', '-j', '8', '-s', '2', '--prefetch-work',
                 '--request-interval', '0.25', '--request-jitter', '0',
                 '--cookie-cleanup', 'synthetic_a, synthetic_b,synthetic_a']
        with patch.object(sys, 'argv', flags):
            env['apply_study_settings'](common, env['parse_args']())
        self.assertEqual(common, {'jobs': 8, 'speed': 2.0, 'prefetch_work': True,
                                  'request_interval': 0.25, 'request_jitter': 0.0,
                                  'cookie_cleanup_names': ['synthetic_a', 'synthetic_b']})

    def test_empty_cookie_cleanup_flag_explicitly_disables_cleanup(self):
        env = settings_env(['parse_args', 'validate_study_settings', 'apply_study_settings'])
        common = {'jobs': 3, 'speed': 1.25, 'cookie_cleanup_names': ['synthetic_marker']}
        with patch.object(sys, 'argv', ['main.py', '--cookie-cleanup', '']):
            env['apply_study_settings'](common, env['parse_args']())
        self.assertEqual(common['cookie_cleanup_names'], [])
        self.assertEqual((common['jobs'], common['speed']), (3, 1.25))

    def test_request_cli_values_are_validated_before_study_prompts(self):
        env = settings_env(['parse_args', 'validate_study_settings', 'apply_study_settings'])
        for flags in (['--request-interval', '0'], ['--request-jitter', 'nan'],
                      ['--cookie-cleanup', '_uid'], ['--cookie-cleanup', 'cf_clearance'],
                      ['--cookie-cleanup', 'synthetic_a,,synthetic_b'],
                      ['--cookie-cleanup', 'synthetic=value']):
            with self.subTest(flags=flags), patch.object(sys, 'argv', ['main.py', '--study-settings', *flags]), \
                    patch('builtins.input') as prompt, self.assertRaises(ValueError):
                env['apply_study_settings']({'jobs': 1, 'speed': 1}, env['parse_args']())
            prompt.assert_not_called()

    def test_request_settings_reach_client_without_changing_video_or_jobs(self):
        env = settings_env(['init_chaoxing'])
        account, provider, client = object(), Mock(), object()
        tiku = Mock()
        tiku.get_tiku_from_config.return_value = provider
        env.update({'Account': Mock(return_value=account), 'Tiku': Mock(return_value=tiku),
                    'Chaoxing': Mock(return_value=client), 'Notification': Mock()})
        cookies = SimpleNamespace(use_cookies=Mock(return_value={}))
        common = {'username': 'SYNTHETIC_USER', 'password': 'NOT_REAL', 'jobs': 6, 'speed': 1.75,
                  'request_interval': '0.75', 'request_jitter': '0',
                  'cookie_cleanup_names': ['synthetic_marker']}
        with patch.dict(sys.modules, {'api.cookies': cookies}):
            result = env['init_chaoxing'](common, {'delay': 0.2})
        self.assertIs(result, client)
        env['Chaoxing'].assert_called_once_with(account=account, tiku=provider, query_delay=0.2,
                                              request_interval=0.75, request_jitter=0.0,
                                              cookie_cleanup_names=['synthetic_marker'])
        self.assertEqual((common['jobs'], common['speed']), (6, 1.75))
        provider.init_tiku.assert_called_once_with()

    def test_invalid_request_config_fails_before_cookies_or_provider_are_loaded(self):
        env = settings_env(['init_chaoxing'])
        env.update({'Account': Mock(), 'Tiku': Mock(), 'Chaoxing': Mock()})
        cookies = SimpleNamespace(use_cookies=Mock(return_value={}))
        invalid = ({'request_interval': 0.09}, {'request_interval': float('inf')},
                   {'request_jitter': True}, {'request_jitter': 5.1},
                   {'cookie_cleanup_names': ['JSESSIONID']})
        for config in invalid:
            with self.subTest(config=config), patch.dict(sys.modules, {'api.cookies': cookies}), \
                    self.assertRaises(ValueError):
                env['init_chaoxing'](config, {})
        cookies.use_cookies.assert_not_called()
        env['Account'].assert_not_called()
        env['Tiku'].assert_not_called()
        env['Chaoxing'].assert_not_called()


class ConcurrentChapterTests(unittest.TestCase):
    def processor(self, behavior, jobs=4, speed=1.5, tasks=8):
        env = load_units('chaoxing/main.py',
                         ['ChapterResult', 'ChapterTask', 'log_error',
                          'validate_study_settings', 'JobProcessor'],
                         {'enum': enum, 'dataclass': dataclass, 'math': math,
                          'threading': threading, 'PriorityQueue': queue.PriorityQueue,
                          'ShutDown': getattr(queue, 'ShutDown', type('ShutDown', (Exception,), {})),
                          'logger': Mock(), 'traceback': __import__('traceback'),
                          'time': SimpleNamespace(sleep=lambda seconds: None),
                          'process_chapter': behavior})
        points = [env['ChapterTask'](i, {'id': str(i), 'title': str(i), 'has_finished': False})
                  for i in reversed(range(tasks))]
        return env['JobProcessor'](SimpleNamespace(start_course=Mock(), finish_course=Mock()), {}, points,
                                   {'jobs': jobs, 'speed': speed, 'notopen_action': 'retry'}), env

    def test_four_chapters_overlap_but_never_more_than_four(self):
        lock = threading.Lock()
        gate = threading.Event()
        running = maximum = 0
        first_started, speeds = [], []
        def work(cx, course, point, speed):
            nonlocal running, maximum
            with lock:
                running += 1
                maximum = max(maximum, running)
                first_started.append(int(point['id']))
                speeds.append(speed)
                if running == 4:
                    gate.set()
            self.assertTrue(gate.wait(2), 'Four workers did not overlap')
            with lock:
                running -= 1
            return env['ChapterResult'].SUCCESS
        p, env = self.processor(work)
        p.run()
        self.assertEqual(maximum, 4)
        self.assertEqual(set(first_started[:4]), {0, 1, 2, 3})
        self.assertEqual(speeds, [1.5] * 8)
        self.assertTrue(all(not t.is_alive() for t in p.threads))
        self.assertEqual(p.task_queue.unfinished_tasks, 0)

    def test_progress_failure_signals_running_workers_and_drains_queue(self):
        env = None
        def work(cx, course, point, speed):
            raise env['ProgressError']('synthetic rollback')
        p, env = self.processor(work, jobs=1)
        env['ProgressError'] = type('ProgressError', (ValueError,), {})
        p.run()
        self.assertTrue(p.stop_event.is_set())
        self.assertEqual(len(p.deferred_tasks), 8)
        self.assertIsNotNone(p.progress_error)

    def test_failure_stops_other_inflight_chapters_before_dequeueing_more(self):
        barrier = threading.Barrier(4)
        calls = []
        lock = threading.Lock()
        def work(cx, course, point, speed):
            with lock:
                calls.append(point['id'])
            barrier.wait(2)
            if point['id'] == '0':
                raise env['ProgressError']('synthetic online failure')
            self.assertTrue(cx._study_stop_event.wait(2))
            return env['ChapterResult'].DEFERRED
        p, env = self.processor(work)
        env['ProgressError'] = type('ProgressError', (ValueError,), {})
        p.run()
        self.assertEqual(set(calls), {'0', '1', '2', '3'})
        self.assertEqual(len(p.deferred_tasks), 8)
        self.assertTrue(p.stop_event.is_set())

    def test_parallel_completion_rereads_and_snapshots_are_serialized(self):
        results = load_units('chaoxing/main.py', ['ChapterResult'], {'enum': enum})['ChapterResult']
        points = [{'id': str(i), 'title': str(i), 'has_finished': False} for i in range(2)]
        final = [dict(p, has_finished=True) for p in points]
        cx, store = Mock(), Mock()
        cx.get_uid.return_value = 'synthetic-account'
        active = maximum = calls = 0
        lock = threading.Lock()
        def read(*args):
            nonlocal active, maximum, calls
            with lock:
                active += 1
                maximum = max(maximum, active)
                calls += 1
                current = points if calls == 1 else final
            time.sleep(.01)
            with lock:
                active -= 1
            return {'points': current}
        cx.get_course_point.side_effect = read
        class Processor:
            def __init__(self, cx, course, tasks, config):
                self.tasks, self.failed_tasks, self.deferred_tasks = tasks, [], []
                self.progress_error = None
            def run(self):
                def verify(task):
                    task.result = results.SUCCESS if self.verify_completion(task.point) else results.DEFERRED
                threads = [threading.Thread(target=verify, args=(task,)) for task in self.tasks]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(2)
                    if thread.is_alive():
                        raise AssertionError('Verification worker did not exit')
        env = load_units('chaoxing/main.py', ['_get_course_point_with_policy', 'process_course'],
                         {'logger': Mock(), 'ProgressStore': lambda: store, 'JobProcessor': Processor,
                          'threading': threading, 'ChapterResult': results,
                          'ChapterTask': lambda point, index: SimpleNamespace(point=point, index=index),
                          'print_course_tree': Mock()})
        p = env['process_course'](cx, {'courseId': 'c', 'clazzId': 'b', 'cpi': 'p', 'title': 'test'}, {})
        self.assertEqual(maximum, 1)
        self.assertEqual(store.check_and_record.call_count, 4)
        self.assertTrue(all(t.result is results.SUCCESS for t in p.tasks))

    def test_early_pending_chapters_selected_even_if_later_chapters_complete(self):
        result = load_units('chaoxing/main.py', ['ChapterResult'], {'enum': enum})['ChapterResult']
        points = [{'id': str(i), 'title': str(i), 'has_finished': i in (1, 3, 4)} for i in range(5)]
        final = [dict(p, has_finished=True) for p in points]
        cx = Mock()
        cx.get_uid.return_value = 'synthetic-account'
        cx.get_course_point.side_effect = [{'points': points}, {'points': final}]
        class Processor:
            def __init__(self, cx, course, tasks, config):
                self.tasks, self.failed_tasks, self.deferred_tasks = tasks, [], []
                self.progress_error = None
            def run(self):
                for task in self.tasks:
                    task.result = result.SUCCESS
        env = load_units('chaoxing/main.py', ['_get_course_point_with_policy', 'process_course'],
                         {'logger': Mock(), 'ProgressStore': Mock(), 'JobProcessor': Processor,
                          'threading': threading, 'ChapterResult': result,
                          'ChapterTask': lambda point, index: SimpleNamespace(point=point, index=index),
                          'print_course_tree': Mock()})
        p = env['process_course'](cx, {'courseId': 'c', 'clazzId': 'b', 'cpi': 'p', 'title': 'test'}, {})
        self.assertEqual([t.point['id'] for t in p.tasks], ['0', '2'])
        self.assertEqual([t.index for t in p.tasks], [0, 2])


if __name__ == '__main__':
    unittest.main()
