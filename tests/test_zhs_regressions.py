"""Offline regressions executing actual ZHS definitions without module imports."""
import ast
import hashlib
import math
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "zhs" / "fucker.py"


class AttrDict(dict):
    def __init__(self, *args, default=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.default = default

    def __getattr__(self, name):
        return self.get(name, self.default)


class OfflineDefinitions(unittest.TestCase):
    def setUp(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
        selected = []
        methods = {"__init__", "fuckHikeVideo", "saveStuStudyRecord", "_checkTimeLimit"}
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "Fucker":
                node.body = [item for item in node.body if isinstance(item, ast.FunctionDef) and item.name in methods]
                selected.append(node)
            elif isinstance(node, ast.ClassDef) and node.name in {"TimeLimitExceeded", "PptToTxt"}:
                selected.append(node)
        self.env = {
            "math": math, "os": os, "stat": stat, "Path": Path,
            "hashlib": hashlib, "urlparse": urlparse, "re": __import__("re"),
            "datetime": datetime, "json": __import__("json"), "partial": partial,
            "time": SimpleNamespace(time=Mock(return_value=1000), sleep=Mock()),
            "random": lambda: 0, "logger": Mock(), "progressBar": Mock(),
            "requests": SimpleNamespace(Session=Mock(return_value=Mock())),
            "Retry": Mock(), "HTTPAdapter": Mock(), "ObjDict": AttrDict,
            "urllib": SimpleNamespace(request=SimpleNamespace(getproxies=Mock(return_value={}))),
            "OpenAI": Mock(side_effect=AssertionError("Model client must not be constructed")),
            "getRealPath": Mock(side_effect=AssertionError("Real cache must not be accessed")),
        }
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(SOURCE), "exec"), self.env)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name) / "cache"
        self.cache.mkdir()

    def hike(self, total=10, end_thre=None, speed=30, responses=None, prev=0, limit=0):
        obj = self.env["Fucker"](end_thre=end_thre, speed=speed, limit=limit)
        ctx = SimpleNamespace(fucked_time=0)
        obj.context["course"] = ctx
        obj._checkCookies = Mock()
        obj._sessionReady = Mock()
        obj.getHikeContext = Mock(return_value=ctx)
        obj.stuViewFile = Mock(return_value=SimpleNamespace(totalTime=total, dataId="video"))
        obj.watchVideo = Mock()
        if responses is None:
            obj.hikeQuery = Mock(side_effect=lambda url, params, **kwargs: SimpleNamespace(rt=params["endWatchTime"]))
        else:
            obj.hikeQuery = Mock(side_effect=lambda url, params, **kwargs: SimpleNamespace(rt=responses(obj.hikeQuery.call_count, params)))
        # An implementation regression must fail rather than hang the test runner.
        def sleep(_seconds):
            if self.env["time"].sleep.call_count > 1000:
                raise AssertionError("Hike loop exceeded offline safety budget")
        self.env["time"].sleep.side_effect = sleep
        return obj, lambda: obj.fuckHikeVideo("course", "file", prev)

    def ppt(self):
        obj = object.__new__(self.env["PptToTxt"])
        obj._PptToTxt__download_path = str(self.cache)
        obj._PptToTxt__file_cache = {}
        obj._PptToTxt__session = Mock()
        obj._PptToTxt__session.get.return_value = Mock(status_code=200, iter_content=Mock(return_value=[b"presentation"]))
        files = Mock()
        files.create.side_effect = lambda **kwargs: SimpleNamespace(id=f"file-{files.create.call_count}")
        obj._PptToTxt__client = SimpleNamespace(files=files)
        return obj

    def symlink(self, target, link, is_directory=False):
        try:
            link.symlink_to(target, target_is_directory=is_directory)
        except OSError as exc:
            self.skipTest(f"Symlink unavailable: {exc}")


class TestHikeRegressions(OfflineDefinitions):
    def test_default_fractional_threshold_uses_integer_protocol(self):
        obj, run = self.hike(speed=1.25)
        self.assertEqual(obj.end_thre, 0.91)
        run()
        params = obj.hikeQuery.call_args.args[1]
        self.assertEqual(params["endWatchTime"], 10)
        self.assertGreater(params["studyTotalTime"], 0)
        self.assertLess(obj.hikeQuery.call_count, 10)

    def test_integer_target_is_unchanged(self):
        for threshold, target in ((1.0, 10), (0.9, 9), (1.2, 12)):
            with self.subTest(threshold=threshold):
                obj, run = self.hike(end_thre=threshold)
                run()
                self.assertEqual(obj.hikeQuery.call_args.args[1]["endWatchTime"], target)

    def test_no_progress_fails_after_five_responses(self):
        obj, run = self.hike(responses=lambda count, params: 0)
        with self.assertRaisesRegex(RuntimeError, "no progress.*5"):
            run()
        self.assertEqual(obj.hikeQuery.call_count, 5)
        self.assertFalse(any("Fucked video" in str(call) for call in self.env["logger"].info.call_args_list))
        self.assertFalse(any(call.kwargs.get("suffix") == "done" for call in self.env["progressBar"].call_args_list))

    def test_continual_rollback_fails_after_five_responses(self):
        obj, run = self.hike(responses=lambda count, params: max(0, 8 - count), prev=8)
        with self.assertRaisesRegex(RuntimeError, "no progress.*5"):
            run()
        self.assertEqual(obj.hikeQuery.call_count, 5)

    def test_oscillation_does_not_reset_stall_counter(self):
        obj, run = self.hike(responses=lambda count, params: 7 if count % 2 else 8, prev=8)
        with self.assertRaisesRegex(RuntimeError, "no progress.*5"):
            run()
        self.assertEqual(obj.hikeQuery.call_count, 5)

    def test_new_high_water_progress_resets_stall_counter(self):
        values = [0, 0, 0, 0, 1, 1, 1, 1, 1, 10]
        obj, run = self.hike(responses=lambda count, params: values[count - 1])
        run()
        self.assertEqual(obj.hikeQuery.call_count, 10)

    def test_time_limit_is_checked_in_each_iteration(self):
        obj, run = self.hike(total=100, speed=1, limit=2 / 60)
        with self.assertRaises(self.env["TimeLimitExceeded"]):
            run()
        self.assertEqual(obj.context["course"].fucked_time, 2)
        obj.hikeQuery.assert_not_called()


class TestPptRegressions(OfflineDefinitions):
    def test_same_basename_distinct_urls_have_distinct_cached_bytes(self):
        obj = self.ppt()
        obj._PptToTxt__session.get.side_effect = [
            Mock(status_code=200, iter_content=Mock(return_value=[b"course A"])),
            Mock(status_code=200, iter_content=Mock(return_value=[b"course B"])),
        ]
        url_a = "https://example.invalid/course-A/slides.pptx?token=private-A"
        url_b = "https://example.invalid/course-B/slides.pptx?token=private-B"
        path_a = Path(obj._PptToTxt__getFilePath(url_a))
        path_b = Path(obj._PptToTxt__getFilePath(url_b))
        self.assertNotEqual(path_a, path_b)
        self.assertEqual(path_a.read_bytes(), b"course A")
        self.assertEqual(path_b.read_bytes(), b"course B")
        self.assertNotIn("private", path_a.name)
        self.assertIn(hashlib.sha256(url_a.encode()).hexdigest(), path_a.name)
        self.assertEqual(path_a.suffix, ".pptx")
        self.assertEqual(obj._PptToTxt__getFilePath(url_a), str(path_a))
        self.assertEqual(obj._PptToTxt__session.get.call_count, 2)

    def test_query_is_part_of_url_identity(self):
        obj = self.ppt()
        first = obj._PptToTxt__getFilePath("https://example.invalid/slides.pptx?version=1")
        second = obj._PptToTxt__getFilePath("https://example.invalid/slides.pptx?version=2")
        self.assertNotEqual(first, second)

    def test_generated_names_have_safe_extensions(self):
        obj = self.ppt()
        for url in ("https://example.invalid/", "https://example.invalid/a.bad%3Aext", "https://example.invalid/../../x.PPTX"):
            with self.subTest(url=url):
                path = Path(obj._PptToTxt__getFilePath(url))
                self.assertEqual(path.parent.resolve(), self.cache.resolve())
                self.assertRegex(path.name, r"^url-[a-f0-9]{64}\.[a-z0-9]{1,10}$")

    def test_upload_identity_uses_content_not_name_or_size(self):
        obj = self.ppt()
        path = self.cache / "same.pptx"
        path.write_bytes(b"AAAA")
        first = obj._PptToTxt__uploadFile(str(path))
        path.write_bytes(b"BBBB")
        second = obj._PptToTxt__uploadFile(str(path))
        self.assertNotEqual(first, second)
        copy = self.cache / "other.pptx"
        copy.write_bytes(b"BBBB")
        self.assertEqual(obj._PptToTxt__uploadFile(str(copy)), second)
        self.assertEqual(obj._PptToTxt__client.files.create.call_count, 2)

    def test_remote_filename_and_size_do_not_prove_content_identity(self):
        obj = self.ppt()
        path = self.cache / "same.pptx"
        path.write_bytes(b"AAAA")
        obj._PptToTxt__client.files.list.return_value = SimpleNamespace(data=[
            SimpleNamespace(filename=path.name, bytes=4, id="unverified", created_at=0)
        ])
        obj._PptToTxt__initialize_cache()
        self.assertNotEqual(obj._PptToTxt__uploadFile(str(path)), "unverified")

    def test_explicit_target_rejects_escape_and_prefix_sibling_before_request(self):
        obj = self.ppt()
        for target in (Path(self.temp.name) / "escape.pptx", Path(str(self.cache) + "-evil") / "x.pptx", self.cache):
            with self.subTest(target=target), self.assertRaises(ValueError):
                obj._PptToTxt__downloadFile("https://example.invalid/a.pptx", str(target))
        obj._PptToTxt__session.get.assert_not_called()

    def test_explicit_nested_target_is_allowed(self):
        obj = self.ppt()
        target = self.cache / "nested" / "slides.pptx"
        self.assertEqual(obj._PptToTxt__downloadFile("https://example.invalid/a.pptx", str(target)), str(target))
        self.assertEqual(target.read_bytes(), b"presentation")

    def test_cached_symlink_is_rejected(self):
        obj = self.ppt()
        url = "https://example.invalid/slides.pptx"
        path = Path(obj._PptToTxt__getFilePath(url))
        path.unlink()
        outside = Path(self.temp.name) / "outside.pptx"
        outside.write_bytes(b"outside")
        self.symlink(outside, path)
        with self.assertRaises(ValueError):
            obj._PptToTxt__getFilePath(url)
        self.assertEqual(obj._PptToTxt__session.get.call_count, 1)
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_nested_symlink_and_dangling_symlink_are_rejected(self):
        obj = self.ppt()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        link = self.cache / "link"
        self.symlink(outside, link, is_directory=True)
        with self.assertRaises(ValueError):
            obj._PptToTxt__downloadFile("https://example.invalid/a.pptx", str(link / "x.pptx"))
        dangling = self.cache / "dangling.pptx"
        self.symlink(outside / "missing.pptx", dangling)
        with self.assertRaises(ValueError):
            obj._PptToTxt__downloadFile("https://example.invalid/a.pptx", str(dangling))
        obj._PptToTxt__session.get.assert_not_called()

    def test_symlink_cache_root_is_rejected(self):
        obj = self.ppt()
        root_link = Path(self.temp.name) / "root-link"
        self.symlink(self.cache, root_link, is_directory=True)
        obj._PptToTxt__download_path = str(root_link)
        with self.assertRaises(ValueError):
            obj._PptToTxt__getFilePath("https://example.invalid/a.pptx")
        obj._PptToTxt__session.get.assert_not_called()

    def test_download_default_target_matches_cache_identity(self):
        obj = self.ppt()
        url = "https://example.invalid/a/slides.pptx?key=private"
        downloaded = obj._PptToTxt__downloadFile(url)
        self.assertEqual(obj._PptToTxt__getFilePath(url), downloaded)
        self.assertEqual(obj._PptToTxt__session.get.call_count, 1)

    def test_upload_rejects_outside_cache_before_client_call(self):
        obj = self.ppt()
        outside = Path(self.temp.name) / "outside.pptx"
        outside.write_bytes(b"outside")
        with self.assertRaises(ValueError):
            obj._PptToTxt__uploadFile(str(outside))
        obj._PptToTxt__client.files.create.assert_not_called()

    def test_download_revalidates_target_after_request(self):
        obj = self.ppt()
        target = self.cache / "slides.pptx"
        outside = Path(self.temp.name) / "outside.pptx"
        outside.write_bytes(b"outside")
        def response(*args, **kwargs):
            self.symlink(outside, target)
            return Mock(status_code=200, iter_content=Mock(return_value=[b"replacement"]))
        obj._PptToTxt__session.get.side_effect = response
        with self.assertRaises(ValueError):
            obj._PptToTxt__downloadFile("https://example.invalid/a.pptx", str(target))
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_parse_rejects_cached_symlink_before_upload(self):
        obj = self.ppt()
        url = "https://example.invalid/slides.pptx"
        target = Path(obj._PptToTxt__getFilePath(url))
        target.unlink()
        outside = Path(self.temp.name) / "outside.pptx"
        outside.write_bytes(b"outside")
        self.symlink(outside, target)
        self.assertEqual(obj.parseTxt(url), "")
        obj._PptToTxt__client.files.create.assert_not_called()
        self.assertEqual(obj._PptToTxt__session.get.call_count, 1)

    @unittest.skipUnless(os.name == "nt", "Windows junction coverage")
    def test_windows_junction_escape_is_rejected(self):
        obj = self.ppt()
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        junction = self.cache / "junction"
        env = dict(os.environ, TEST_JUNCTION=str(junction), TEST_JUNCTION_TARGET=str(outside))
        result = subprocess.run([
            "pwsh", "-NoProfile", "-Command",
            "New-Item -ItemType Junction -Path $env:TEST_JUNCTION -Target $env:TEST_JUNCTION_TARGET -ErrorAction Stop | Out-Null",
        ], env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.addCleanup(lambda: os.rmdir(junction) if junction.exists() else None)
        with self.assertRaises(ValueError):
            obj._PptToTxt__downloadFile("https://example.invalid/a.pptx", str(junction / "x.pptx"))
        obj._PptToTxt__session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
