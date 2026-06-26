import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CHAOXING_DIR = ROOT / "chaoxing"
sys.path.insert(0, str(CHAOXING_DIR))

api_pkg = types.ModuleType("api")
sys.modules.setdefault("api", api_pkg)

answer_mod = types.ModuleType("api.answer")
answer_mod.Tiku = object
sys.modules["api.answer"] = answer_mod

base_mod = types.ModuleType("api.base")
base_mod.Chaoxing = object
base_mod.Account = object
base_mod.StudyResult = object
sys.modules["api.base"] = base_mod

exceptions_mod = types.ModuleType("api.exceptions")
exceptions_mod.LoginError = RuntimeError
exceptions_mod.InputFormatError = ValueError
sys.modules["api.exceptions"] = exceptions_mod

logger_mod = types.ModuleType("api.logger")


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


logger_mod.logger = _Logger()
sys.modules["api.logger"] = logger_mod

notification_mod = types.ModuleType("api.notification")
notification_mod.Notification = object
sys.modules["api.notification"] = notification_mod

live_mod = types.ModuleType("api.live")
live_mod.Live = object
sys.modules["api.live"] = live_mod

live_process_mod = types.ModuleType("api.live_process")
live_process_mod.LiveProcessor = object
sys.modules["api.live_process"] = live_process_mod

import main as chaoxing_main  # noqa: E402


class JobProcessorTests(unittest.TestCase):
    def _processor(self, notopen_action="retry"):
        task = chaoxing_main.ChapterTask(
            index=0,
            point={"title": "locked chapter", "has_finished": False},
        )
        config = {
            "jobs": 1,
            "speed": 1.0,
            "notopen_action": notopen_action,
        }
        processor = chaoxing_main.JobProcessor(
            chaoxing=object(),
            course={"title": "course"},
            tasks=[task],
            config=config,
        )
        processor.max_tries = 2
        return processor, task

    def test_not_open_retry_eventually_fails(self):
        processor, task = self._processor()

        with patch.object(chaoxing_main, "process_chapter", return_value=chaoxing_main.ChapterResult.NOT_OPEN):
            self.assertFalse(processor.run())

        self.assertEqual(task.tries, 2)
        self.assertEqual(processor.failed_tasks, [task])

    def test_not_open_continue_is_successful_skip(self):
        processor, task = self._processor(notopen_action="continue")

        with patch.object(chaoxing_main, "process_chapter", return_value=chaoxing_main.ChapterResult.NOT_OPEN):
            self.assertTrue(processor.run())

        self.assertEqual(task.tries, 1)
        self.assertEqual(processor.failed_tasks, [])


if __name__ == "__main__":
    unittest.main()
