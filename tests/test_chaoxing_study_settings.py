"""Offline tests for chapter concurrency, settings precedence and directory order."""
import argparse
import contextlib
from dataclasses import dataclass
import enum
import io
import math
import queue
import sys
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.source_units import load_units


def settings_env(names):
    return load_units('chaoxing/main.py', names,
                      {'argparse': argparse, 'sys': sys, 'math': math, 'logger': Mock()})


class StudySettingsTests(unittest.TestCase):
    def test_cli_defaults_do_not_override_saved_study_settings(self):
        parse = settings_env(['parse_args'])['parse_args']
        with patch.object(sys, 'argv', ['main.py']):
            args = parse()
        self.assertIsNone(args.jobs)
        self.assertIsNone(args.speed)

    def test_config_settings_survive_and_explicit_cli_overrides(self):
        env = settings_env(['parse_args', 'validate_study_settings', 'apply_study_settings'])
        common = {'jobs': 4, 'speed': 1.5}
        with patch.object(sys, 'argv', ['main.py']):
            env['apply_study_settings'](common, env['parse_args']())
        self.assertEqual(common, {'jobs': 4, 'speed': 1.5})
        with patch.object(sys, 'argv', ['main.py', '-j', '2', '-s', '1.25']):
            env['apply_study_settings'](common, env['parse_args']())
        self.assertEqual(common, {'jobs': 2, 'speed': 1.25})

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
        self.assertEqual(common, {'jobs': 4, 'speed': 2})

    def test_launcher_prompts_only_for_chapter_mode(self):
        env = load_units('main.py', ['run_chaoxing', 'run_chaoxing_exam'],
                         {'_run': Mock(), 'CHAOXING_DIR': 'synthetic'})
        env['run_chaoxing']()
        self.assertIn('--study-settings', env['_run'].call_args.args[1])
        env['run_chaoxing_exam']()
        self.assertNotIn('--study-settings', env['_run'].call_args.args[1])


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
        return env['JobProcessor'](SimpleNamespace(), {}, points,
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
        env = load_units('chaoxing/main.py', ['process_course'],
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
        env = load_units('chaoxing/main.py', ['process_course'],
                         {'logger': Mock(), 'ProgressStore': Mock(), 'JobProcessor': Processor,
                          'threading': threading, 'ChapterResult': result,
                          'ChapterTask': lambda point, index: SimpleNamespace(point=point, index=index),
                          'print_course_tree': Mock()})
        p = env['process_course'](cx, {'courseId': 'c', 'clazzId': 'b', 'cpi': 'p', 'title': 'test'}, {})
        self.assertEqual([t.point['id'] for t in p.tasks], ['0', '2'])
        self.assertEqual([t.index for t in p.tasks], [0, 2])


if __name__ == '__main__':
    unittest.main()
