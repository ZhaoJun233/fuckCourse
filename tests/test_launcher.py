"""Frozen launcher must preserve platform output until the user acknowledges it."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

from tests.source_units import load_units


class FrozenLauncherTests(unittest.TestCase):
    def test_platform_returns_wait_before_menu_and_restore_environment(self):
        for source in ("print('章节任务点未完成')", "raise SystemExit(0)",
                       "raise RuntimeError('测试错误')", "raise KeyboardInterrupt"):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                Path(directory, "main.py").write_text(source, encoding="utf-8")
                old_cwd, old_argv, old_path = os.getcwd(), sys.argv, list(sys.path)

                def acknowledge(prompt):
                    self.assertIn("返回菜单", prompt)
                    self.assertEqual(os.getcwd(), old_cwd)
                    self.assertIs(sys.argv, old_argv)
                    self.assertEqual(sys.path, old_path)
                    return ""

                ask = Mock(side_effect=acknowledge)
                env = {"os": os, "sys": sys, "input": ask, "print": Mock()}
                run = load_units("main.py", ["_run_frozen"], env)["_run_frozen"]
                with contextlib.redirect_stdout(io.StringIO()):
                    run(directory, ["main.py", "--exam"], "chaoxing-exam")
                ask.assert_called_once()

    def test_selected_exam_prerequisite_survives_until_launcher_acknowledgment(self):
        source = '''
from tests.test_chaoxing_exam import exam, runner, COURSE, EXAM, response
from unittest.mock import Mock
html = '<ul class="nav"><li data="' + EXAM['url'] + '"><p>测试期末</p><span>待做</span></li></ul>'
session = Mock()
session.request.side_effect = [response(html), response('<h2 class="textCenter">章节任务点未完成</h2>')]
client = exam.ExamClient(session)
cx = Mock()
cx.get_course_list.return_value = [COURSE]
runner.run_exam_mode(cx, client=client, ask=lambda prompt: '1')
assert session.request.call_count == 2
assert all(call.args[0] == 'GET' for call in session.request.call_args_list)
'''
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "main.py").write_text(source, encoding="utf-8")
            output = io.StringIO()

            def acknowledge(prompt):
                self.assertIn("章节任务点未完成", output.getvalue())
                self.assertIn("未启动考试", output.getvalue())
                self.assertNotIn("[chaoxing-exam] error:", output.getvalue())
                return ""

            ask = Mock(side_effect=acknowledge)
            env = {"os": os, "sys": sys, "input": ask}
            run = load_units("main.py", ["_run_frozen"], env)["_run_frozen"]
            with contextlib.redirect_stdout(output):
                run(directory, ["main.py", "--exam"], "chaoxing-exam")
            ask.assert_called_once()

    def test_closed_console_does_not_raise_while_waiting(self):
        for failure in (EOFError, KeyboardInterrupt):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                Path(directory, "main.py").write_text("pass", encoding="utf-8")
                ask = Mock(side_effect=failure)
                env = {"os": os, "sys": sys, "input": ask, "print": Mock()}
                run = load_units("main.py", ["_run_frozen"], env)["_run_frozen"]
                run(directory, ["main.py"], "chaoxing")
                ask.assert_called_once()
