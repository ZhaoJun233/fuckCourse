"""Offline regressions against actual configuration writers and queue workers."""
import ast
import builtins
import enum
import io
import json
import os
import queue
import sys
import tempfile
import threading
import traceback
import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]


def load_definitions(relative_path, names, namespace):
    path = ROOT / relative_path
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path),
                     feature_version=(3, 10))
    definitions = [node for node in tree.body
                   if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                   and node.name in names]
    if {node.name for node in definitions} != set(names):
        raise AssertionError("Missing actual definitions: " + relative_path)
    module = ast.Module(body=definitions, type_ignores=[])
    exec(compile(ast.fix_missing_locations(module), str(path), "exec"), namespace)
    return namespace


class TestCredentialWrites(unittest.TestCase):
    CASES = ("chaoxing", "welearn", "yuketang", "yuketang_cookies")

    @contextmanager
    def writer(self, case):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            namespace = {"os": os, "json": json, "Path": Path,
                         "logger": Mock(), "logging": Mock(),
                         "CONFIG_FILE": str(path)}
            env = {"FUCKCOURSE_CONFIG": str(path), "FUCKCOURSE_COOKIES": ""}
            if case == "chaoxing":
                load_definitions("chaoxing/api/base.py",
                                 {"_save_credentials_to_config"}, namespace)
                action = lambda: namespace["_save_credentials_to_config"]("user", "pass")
            elif case == "welearn":
                load_definitions("welearn/welearn_decompiled.py",
                                 {"_save_welearn_credentials"}, namespace)
                action = lambda: namespace["_save_welearn_credentials"]("user", "pass")
            else:
                load_definitions("yuketang/yuketang_login.py",
                                 {"_shared_cookies_path", "_shared_config_path",
                                  "_read_shared_json", "_write_shared_json", "save_config"},
                                 namespace)
                namespace["load_config"] = Mock(side_effect=AssertionError("Local fallback forbidden"))
                if case == "yuketang_cookies":
                    env = {"FUCKCOURSE_CONFIG": "", "FUCKCOURSE_COOKIES": str(path)}
                    action = lambda: namespace["save_config"](cookie_str="new-cookie")
                else:
                    action = lambda: namespace["save_config"](university_id="42")
            with patch.dict(os.environ, env):
                yield path, action, namespace

    def assert_updated(self, case, root):
        if case == "chaoxing":
            self.assertEqual(root[case]["common"]["username"], "user")
            self.assertEqual(root[case]["common"]["password"], "pass")
        elif case == "welearn":
            self.assertEqual(root[case]["username"], "user")
            self.assertEqual(root[case]["password"], "pass")
        elif case == "yuketang":
            self.assertEqual(root[case]["university_id"], "42")
        else:
            self.assertEqual(root["yuketang"], "new-cookie")

    def test_missing_file_can_initialize(self):
        for case in self.CASES:
            with self.subTest(case=case), self.writer(case) as (path, action, _):
                action()
                self.assert_updated(case, json.loads(path.read_text(encoding="utf-8")))

    def test_valid_root_preserves_other_platforms_and_settings(self):
        for case in self.CASES:
            with self.subTest(case=case), self.writer(case) as (path, action, _):
                root = {"unrelated": {"token": "keep", "settings": [1, 2]},
                        "chaoxing": {"common": {"speed": 1.5}, "extra": True},
                        "welearn": {"tree_view": False}, "yuketang": {"other": 7}}
                path.write_text(json.dumps(root), encoding="utf-8")
                action()
                result = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(result["unrelated"], root["unrelated"])
                self.assert_updated(case, result)
                if case == "chaoxing":
                    self.assertEqual(result[case]["common"]["speed"], 1.5)
                    self.assertTrue(result[case]["extra"])
                elif case == "welearn":
                    self.assertFalse(result[case]["tree_view"])
                elif case == "yuketang":
                    self.assertEqual(result[case]["other"], 7)

    def test_read_oserror_does_not_overwrite_original(self):
        for case in self.CASES:
            with self.subTest(case=case), self.writer(case) as (path, action, _):
                original = b'{"unrelated": {"keep": true}}'
                path.write_bytes(original)
                original_open, original_io_open = builtins.open, io.open

                def guarded_open(file, mode="r", *args, **kwargs):
                    if os.fspath(file) == str(path) and "r" in mode:
                        raise PermissionError("Injected configuration read failure")
                    return original_open(file, mode, *args, **kwargs)

                def guarded_io_open(file, mode="r", *args, **kwargs):
                    if os.fspath(file) == str(path) and "r" in mode:
                        raise PermissionError("Injected configuration read failure")
                    return original_io_open(file, mode, *args, **kwargs)

                with patch("builtins.open", side_effect=guarded_open), \
                        patch("io.open", side_effect=guarded_io_open):
                    with self.assertRaises(OSError):
                        action()
                self.assertEqual(path.read_bytes(), original)
                self.assertEqual(list(path.parent.glob("*.bak")), [])

    def test_non_object_root_does_not_overwrite_original(self):
        for case in self.CASES:
            for original in (b"[]", b"null", b"42", b'"text"'):
                with self.subTest(case=case, root=original), self.writer(case) as (path, action, _):
                    path.write_bytes(original)
                    with self.assertRaises(ValueError):
                        action()
                    self.assertEqual(path.read_bytes(), original)
                    self.assertEqual(list(path.parent.glob("*.bak")), [])

    def test_corrupt_root_uses_unique_backup_preserving_old_backups(self):
        for case in self.CASES:
            with self.subTest(case=case), self.writer(case) as (path, action, _):
                old_backup = Path(str(path) + ".corrupt.bak")
                old_backup.write_bytes(b"old recoverable configuration")
                originals = (b"{invalid-json\r\n", b"{second-corruption\n")
                for original in originals:
                    path.write_bytes(original)
                    action()
                    self.assert_updated(case, json.loads(path.read_text(encoding="utf-8")))
                self.assertEqual(old_backup.read_bytes(), b"old recoverable configuration")
                backups = [p for p in path.parent.glob("*.bak") if p != old_backup]
                self.assertEqual(len(backups), 2)
                self.assertEqual({p.read_bytes() for p in backups}, set(originals))

    def test_backup_creation_failure_does_not_overwrite_original(self):
        for case in self.CASES:
            with self.subTest(case=case), self.writer(case) as (path, action, _):
                original = b"{invalid-json"
                path.write_bytes(original)
                with patch("tempfile.NamedTemporaryFile", side_effect=PermissionError("Backup forbidden")), \
                        patch("os.replace", side_effect=PermissionError("Backup forbidden")), \
                        patch.object(Path, "replace", side_effect=PermissionError("Backup forbidden")):
                    with self.assertRaises(OSError):
                        action()
                self.assertEqual(path.read_bytes(), original)

    def test_empty_corrupt_file_is_backed_up_before_initialization(self):
        for case in self.CASES:
            with self.subTest(case=case), self.writer(case) as (path, action, _):
                path.write_bytes(b"")
                action()
                backups = list(path.parent.glob("*.bak"))
                self.assertEqual(len(backups), 1)
                self.assertEqual(backups[0].read_bytes(), b"")
                self.assert_updated(case, json.loads(path.read_text(encoding="utf-8")))

    def test_backup_write_failure_does_not_overwrite_original(self):
        for case in self.CASES:
            with self.subTest(case=case), self.writer(case) as (path, action, _):
                original = b"{invalid-json"
                path.write_bytes(original)
                backup = Mock()
                backup.__enter__ = Mock(return_value=backup)
                backup.__exit__ = Mock(return_value=False)
                backup.write.side_effect = OSError("Injected backup write failure")
                with patch("tempfile.NamedTemporaryFile", return_value=backup):
                    with self.assertRaises(OSError):
                        action()
                self.assertEqual(path.read_bytes(), original)


class TestJobProcessorLifecycle(unittest.TestCase):
    def run_processor(self, behavior, *, notopen_action="retry", legacy=True, task_count=4):
        stop_error = getattr(queue, "ShutDown", type("ShutDown", (Exception,), {}))
        queues, spawned = [], []
        abandon = threading.Event()

        class TestQueue(queue.PriorityQueue):
            def __getattribute__(self, name):
                if name == "shutdown" and legacy:
                    raise AttributeError(name)
                return super().__getattribute__(name)

            def get(self, block=True, timeout=None):
                if not block or timeout is not None:
                    return super().get(block, timeout)
                while not abandon.is_set():
                    try:
                        return super().get(timeout=0.05)
                    except queue.Empty:
                        pass
                raise stop_error()

        def create_queue():
            q = TestQueue()
            queues.append(q)
            return q

        def create_thread(*args, **kwargs):
            thread = threading.Thread(*args, **kwargs)
            spawned.append(thread)
            return thread

        calls = {}
        call_lock = threading.Lock()

        def process_chapter(_chaoxing, _course, point, _speed):
            with call_lock:
                attempt = calls.get(point["title"], 0) + 1
                calls[point["title"]] = attempt
            return behavior(namespace["ChapterResult"], attempt)

        namespace = {"dataclass": dataclass, "enum": enum, "Any": Any,
                     "Chaoxing": object, "PriorityQueue": create_queue,
                     "ShutDown": stop_error, "logger": Mock(), "sys": sys,
                     "threading": SimpleNamespace(Thread=create_thread,
                                                  current_thread=threading.current_thread),
                     "time": SimpleNamespace(sleep=lambda _seconds: None),
                     "traceback": traceback, "process_chapter": process_chapter}
        load_definitions("chaoxing/main.py",
                         {"ChapterResult", "ChapterTask", "log_error", "JobProcessor"}, namespace)
        tasks = [namespace["ChapterTask"](i, {"title": "chapter-" + str(i)})
                 for i in range(task_count)]
        processor = namespace["JobProcessor"](object(), {}, tasks,
                                              {"jobs": 3, "speed": 1, "notopen_action": notopen_action})
        errors = []

        def run():
            try:
                processor.run()
            except BaseException as exc:
                errors.append(exc)

        runner = threading.Thread(target=run, daemon=True)
        try:
            runner.start()
            runner.join(timeout=3)
            self.assertFalse(runner.is_alive(), "JobProcessor.run blocked")
            self.assertEqual(errors, [], "JobProcessor.run raised")
            self.assertEqual(len(spawned), 4, "Expected three task workers plus retry worker")
            self.assertTrue(all(not thread.is_alive() for thread in spawned),
                            "Worker or retry thread survived JobProcessor.run")
            self.assertEqual(set(processor.threads), set(spawned), "Retry worker must be tracked")
            for q in queues:
                self.assertEqual(q.unfinished_tasks, 0, "Unbalanced task_done accounting")
                self.assertTrue(q.empty(), "Queue retained tasks or stop signals")
            return processor, calls
        finally:
            # This test-only escape also reclaims threads when testing the broken implementation.
            abandon.set()
            for thread in spawned:
                thread.join(timeout=1)
            runner.join(timeout=1)

    def test_success_reclaims_workers_without_queue_shutdown(self):
        processor, calls = self.run_processor(lambda results, _attempt: results.SUCCESS)
        self.assertEqual(set(calls.values()), {1})
        self.assertEqual(processor.failed_tasks, [])

    def test_empty_queue_reclaims_all_workers(self):
        processor, calls = self.run_processor(lambda results, _attempt: results.SUCCESS, task_count=0)
        self.assertEqual(calls, {})
        self.assertEqual(processor.failed_tasks, [])

    def test_error_retries_then_success_reclaims_retry_worker(self):
        processor, calls = self.run_processor(
            lambda results, attempt: results.ERROR if attempt < 3 else results.SUCCESS)
        self.assertEqual(set(calls.values()), {3})
        self.assertEqual([task.tries for task in processor.tasks], [2] * 4)
        self.assertEqual(processor.failed_tasks, [])

    def test_error_retry_limit_is_preserved(self):
        processor, calls = self.run_processor(lambda results, _attempt: results.ERROR)
        self.assertEqual(set(calls.values()), {5})
        self.assertEqual(len(processor.failed_tasks), 4)
        self.assertEqual([task.tries for task in processor.tasks], [5] * 4)

    def test_not_open_retries_remain_bounded(self):
        processor, calls = self.run_processor(lambda results, _attempt: results.NOT_OPEN)
        self.assertEqual(set(calls.values()), {5})
        self.assertEqual([task.tries for task in processor.tasks], [5] * 4)

    def test_not_open_continue_skips_without_retry(self):
        processor, calls = self.run_processor(lambda results, _attempt: results.NOT_OPEN,
                                              notopen_action="continue")
        self.assertEqual(set(calls.values()), {1})
        self.assertEqual([task.tries for task in processor.tasks], [1] * 4)

    def test_processing_exception_is_retried_to_limit(self):
        def fail(_results, _attempt):
            raise RuntimeError("Injected chapter failure")

        processor, calls = self.run_processor(fail)
        self.assertEqual(set(calls.values()), {5})
        self.assertEqual(len(processor.failed_tasks), 4)

    @unittest.skipUnless(hasattr(queue.PriorityQueue, "shutdown"), "Requires Python 3.13 queue")
    def test_python313_queue_also_reclaims_retry_worker(self):
        processor, calls = self.run_processor(
            lambda results, attempt: results.ERROR if attempt == 1 else results.SUCCESS,
            legacy=False)
        self.assertEqual(set(calls.values()), {2})
        self.assertEqual(processor.failed_tasks, [])


if __name__ == "__main__":
    unittest.main()
