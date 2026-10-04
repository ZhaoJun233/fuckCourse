"""Verify allowlist construction, binary failure propagation and import probes."""
import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from build_support import expected_project_entries, project_datas, validate_archive_names
from scripts.verify_binary import verify_binary

ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_allowlist_uses_only_explicit_files_even_in_dirty_directory(self):
        entries = project_datas(ROOT)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for source, destination in entries:
                path = root / Path(source).relative_to(ROOT)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"synthetic source")
            for name in ("chaoxing/cookies.json", "zhs/logs/debug.log", "zhs/config.json", "welearn/config.json.corrupt.bak", "yuketang/__pycache__/cache.pyc"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"SYNTHETIC_SECRET")
            datas = project_datas(root)
            self.assertEqual(len(datas), len(entries))
            self.assertTrue(all(Path(source).is_file() for source, _ in datas))
            archive_names = {f"{target}/{Path(source).name}" for source, target in datas}
            self.assertEqual(archive_names, expected_project_entries())
            validate_archive_names(archive_names)

    def test_missing_build_input_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                project_datas(tmp)

    def test_archive_rejects_secret_and_unlisted_platform_entries(self):
        for name in ("zhs/logs/debug.log", "chaoxing/cookies.json", "yuketang/config.json", "welearn/private.py", "zhs/cache/data.bin", "config.json.corrupt.bak"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_archive_names(expected_project_entries() | {name})

    def test_archive_rejects_missing_required_entry(self):
        names = expected_project_entries()
        names.remove("zhs/main.py")
        with self.assertRaises(ValueError):
            validate_archive_names(names)

    def test_archive_accepts_windows_paths_and_third_party_entries(self):
        names = {name.replace("/", "\\") for name in expected_project_entries()}
        names.add("requests/cacert.pem")
        validate_archive_names(names)

    def verify_with_mock(self, responses):
        readers = ModuleType("PyInstaller.archive.readers")
        readers.CArchiveReader = Mock(return_value=SimpleNamespace(toc=expected_project_entries()))
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "app.exe"
            exe.touch()
            with patch.dict(sys.modules, {"PyInstaller.archive.readers": readers}), patch("scripts.verify_binary.subprocess.run", side_effect=responses) as run, contextlib.redirect_stdout(io.StringIO()):
                verify_binary(exe, timeout=3)
                return run.call_args_list

    def test_binary_runner_checks_menu_and_four_platforms(self):
        responses = [SimpleNamespace(returncode=0, stdout="menu", stderr="")]
        responses += [SimpleNamespace(returncode=0, stdout=f"SMOKE_IMPORT_OK {name}\n", stderr="") for name in ("chaoxing", "welearn", "zhs", "yuketang")]
        calls = self.verify_with_mock(responses)
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(call.kwargs["timeout"] == 3 for call in calls))
        self.assertEqual(calls[1].args[0][1:], ["--offline-smoke", "chaoxing"])

    def test_binary_nonzero_exit_propagates(self):
        with self.assertRaises(RuntimeError):
            self.verify_with_mock([SimpleNamespace(returncode=1, stdout="", stderr="failed")])

    def test_binary_probe_requires_completion_marker(self):
        with self.assertRaises(RuntimeError):
            self.verify_with_mock([SimpleNamespace(returncode=0, stdout="menu", stderr=""), SimpleNamespace(returncode=0, stdout="", stderr="")])

    def test_binary_timeout_propagates(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.verify_with_mock([subprocess.TimeoutExpired("app", 3)])


class SourceProbeTests(unittest.TestCase):
    def run_probe(self, platform, root=ROOT):
        if root == ROOT:
            command = [sys.executable, str(ROOT / "main.py"), "--offline-smoke", platform]
        else:
            command = [sys.executable, "-c", "from smoke_support import smoke_import; import sys; smoke_import(sys.argv[1], sys.argv[2])", str(root), platform]
        return subprocess.run(command, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, encoding="utf-8", errors="replace", timeout=60)

    def test_all_real_source_platform_imports_are_offline_and_successful(self):
        for platform in ("chaoxing", "welearn", "zhs", "yuketang"):
            with self.subTest(platform=platform):
                result = self.run_probe(platform)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(f"SMOKE_IMPORT_OK {platform}", result.stdout.splitlines())

    def test_probe_skips_top_level_business_operations(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "welearn"
            directory.mkdir()
            (directory / "welearn_decompiled.py").write_text('import json\nraise AssertionError("Login must not run")\n', encoding="utf-8")
            result = self.run_probe("welearn", Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_dependency_fails_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "welearn"
            directory.mkdir()
            (directory / "welearn_decompiled.py").write_text("import intentionally_missing_smoke_dependency\n", encoding="utf-8")
            result = self.run_probe("welearn", Path(tmp))
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("SMOKE_IMPORT_OK", result.stdout)

    def test_network_attempt_is_blocked_before_dns_or_connect(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "welearn"
            directory.mkdir()
            (directory / "welearn_decompiled.py").write_text("import attempt_network\n", encoding="utf-8")
            (directory / "attempt_network.py").write_text('import socket\nsocket.create_connection(("network-forbidden.invalid", 443))\n', encoding="utf-8")
            result = self.run_probe("welearn", Path(tmp))
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Network is forbidden", result.stderr)


if __name__ == "__main__":
    unittest.main()
