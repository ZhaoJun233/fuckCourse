"""Offline regression checks against real platform definitions."""
import contextlib
import io
import json
import logging
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image
from tests.source_units import load_units


class TestFixesVerified(unittest.TestCase):
    def setUp(self):
        block = patch.object(requests.sessions.Session, "request", side_effect=AssertionError("Network forbidden"))
        block.start()
        self.addCleanup(block.stop)

    def pdf_function(self):
        return load_units("yuketang/main.py", ["images_to_pdf"], {"Image": Image, "io": io, "os": os, "tempfile": tempfile})["images_to_pdf"]

    def image_bytes(self):
        with Image.new("RGB", (8, 8), "white") as image:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()

    def test_image_download_is_isolated(self):
        download = load_units("yuketang/main.py", ["download_image"], {
            "requests": requests, "USER_AGENT": "review",
        })["download_image"]
        session = Mock(cookies={"sessionid": "SYNTHETIC"}, headers={"X-CSRFToken": "SYNTHETIC"})
        with patch.object(requests, "get", return_value=Mock(status_code=200, content=b"image")) as get:
            self.assertEqual(download(session, "https://cdn.invalid/image.png"), b"image")
        get.assert_called_once_with("https://cdn.invalid/image.png", headers={"User-Agent": "review"}, timeout=30)
        session.get.assert_not_called()

    def test_pdf_missing_corrupt_and_empty_pages_fail_without_output(self):
        convert = self.pdf_function()
        good = self.image_bytes()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            for index, pages in enumerate(([good, None], [good, b"not-an-image"], [])):
                with self.subTest(pages=index):
                    path = Path(tmp) / f"{index}.pdf"
                    self.assertFalse(convert(pages, str(path)))
                    self.assertFalse(path.exists())

    def test_pdf_good_pages_are_all_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "complete.pdf"
            self.assertTrue(self.pdf_function()([self.image_bytes(), self.image_bytes()], str(path)))
            self.assertEqual(len(re.findall(rb"/Type /Page\b", path.read_bytes())), 2)

    def test_pdf_closes_decoded_images_after_failure(self):
        image = Mock(mode="RGB")
        fake_image = Mock()
        fake_image.open.side_effect = [image, ValueError("bad page")]
        convert = load_units("yuketang/main.py", ["images_to_pdf"], {"Image": fake_image, "io": io})["images_to_pdf"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertFalse(convert([b"good", b"bad"], "unused.pdf"))
        image.close.assert_called_once()
        image.save.assert_not_called()

    def test_pdf_save_failure_preserves_existing_file_and_cleans_temp(self):
        image = Mock(mode="RGB")
        image.save.side_effect = OSError("Injected save failure")
        fake_image = Mock()
        fake_image.open.return_value = image
        convert = load_units("yuketang/main.py", ["images_to_pdf"], {
            "Image": fake_image, "io": io, "os": os, "tempfile": tempfile,
        })["images_to_pdf"]
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            path = Path(tmp) / "existing.pdf"
            path.write_bytes(b"ORIGINAL")
            self.assertFalse(convert([b"image"], str(path)))
            self.assertEqual(path.read_bytes(), b"ORIGINAL")
            self.assertEqual([item.name for item in Path(tmp).iterdir()], ["existing.pdf"])
        image.close.assert_called_once()

    def test_pdf_replace_failure_preserves_existing_file_and_cleans_temp(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            path = Path(tmp) / "existing.pdf"
            path.write_bytes(b"ORIGINAL")
            with patch.object(os, "replace", side_effect=PermissionError("Injected replace failure")):
                self.assertFalse(self.pdf_function()([self.image_bytes()], str(path)))
            self.assertEqual(path.read_bytes(), b"ORIGINAL")
            self.assertEqual([item.name for item in Path(tmp).iterdir()], ["existing.pdf"])

    def test_course_filter_empty_match_safely_aborts(self):
        function = load_units("chaoxing/main.py", ["filter_courses"], {"logger": Mock()})["filter_courses"]
        courses = [{"courseId": "101", "clazzId": "A", "title": "Math"}]
        self.assertEqual(function(courses, ["999"]), [])

    def score_run(self, responses, score=70):
        session = Mock(headers={}, cookies={})
        session.post.side_effect = [Mock(), Mock()] + responses
        namespace = load_units("welearn/welearn_decompiled.py", ["_build_cmi_data", "startstudy"], {
            "json": json, "logging": Mock(), "Session": lambda: session, "USER_AGENT": "test",
            "session": Mock(cookies={}), "uid": "u", "cid": "c", "classid": "cl",
            "way1Succeed": [], "way1Failed": [], "way2Succeed": [], "way2Failed": [],
        })
        with contextlib.redirect_stdout(io.StringIO()):
            namespace["startstudy"](score, {"id": "lesson"})
        rates = [call.kwargs["data"]["crate"] for call in session.post.call_args_list if "crate" in call.kwargs["data"]]
        return rates, namespace

    def test_score_valid_whitespace_json_does_not_retry_or_change_score(self):
        response = Mock(text='{"ret": 0}')
        response.json.return_value = json.loads(response.text)
        rates, state = self.score_run([response])
        self.assertEqual(rates, ["70"])
        self.assertEqual(state["way1Succeed"], [0])

    def test_score_failed_submission_retries_same_custom_score(self):
        failed = Mock()
        failed.json.return_value = {"ret": 1}
        success = Mock()
        success.json.return_value = {"ret": 0}
        rates, state = self.score_run([failed, success])
        self.assertEqual(rates, ["70", "70"])
        self.assertEqual(state["way2Succeed"], [0])

    def test_score_malformed_json_retries_same_score_and_reports_failure(self):
        malformed = Mock()
        malformed.json.side_effect = ValueError("bad JSON")
        rates, state = self.score_run([malformed, malformed])
        self.assertEqual(rates, ["70", "70"])
        self.assertEqual(state["way1Succeed"], [])
        self.assertEqual(state["way2Failed"], [0])

    def test_openai_actual_request_filters_control_keys(self):
        namespace = load_units("zhs/fucker.py", ["Openai.openaiCompletion"], {
            "requests": requests, "logger": Mock(), "time": Mock(),
        })
        client = namespace["Openai"]()
        client.baseUrl, client.apiKey, client.modelName, client.stream = "https://model.invalid/v1/", "synthetic", "model", False
        client.extra = {"stream": True, "model": "wrong", "messages": [], "temperature": .7, "reasoning_effort": "high"}
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": "answer"}}]}
        with patch.object(requests, "post", return_value=response) as post:
            self.assertEqual(client.openaiCompletion("prompt"), "answer")
        call = post.call_args
        self.assertEqual(call.args[0], "https://model.invalid/v1/chat/completions")
        self.assertEqual(call.kwargs["json"], {"model": "model", "stream": False, "messages": [{"role": "user", "content": "prompt"}], "temperature": .7, "reasoning_effort": "high"})

    def test_chaoxing_reasoning_effort_is_optional_and_preserves_deepseek_workaround(self):
        namespace = load_units("chaoxing/api/answer.py", ["AI._completion_kwargs", "AI._is_deepseek_v4"])
        client = namespace["AI"]()
        client.endpoint, client.model = "https://model.invalid/v1", "gemini-test"
        for config in (None, {}, {"reasoning_effort": ""}):
            with self.subTest(config=config):
                client._conf = config
                self.assertEqual(client._completion_kwargs(model="gemini-test"), {"model": "gemini-test"})
        client._conf = {"reasoning_effort": "high"}
        self.assertEqual(client._completion_kwargs(model="gemini-test"), {"model": "gemini-test", "reasoning_effort": "high"})
        self.assertEqual(client._conf, {"reasoning_effort": "high"})
        client.endpoint, client.model = "https://api.deepseek.com", "deepseek-v4-pro"
        self.assertEqual(client._completion_kwargs(model=client.model), {
            "model": "deepseek-v4-pro", "reasoning_effort": "high",
            "extra_body": {"thinking": {"type": "disabled"}},
        })

    def test_actual_ai_video_low_speed_reports_nonzero_progress(self):
        from types import SimpleNamespace
        namespace = load_units("zhs/fucker.py", ["Fucker.fuckAiVideo"], {
            "logger": Mock(), "time": Mock(), "AI_KEY": "synthetic",
        })
        client = namespace["Fucker"]()
        client.speed, client.progressbar_view = .1, False
        client._checkCookies, client._sessionReady, client.watchVideo = Mock(), Mock(), Mock()
        client.zhidaoQuery = Mock(return_value=SimpleNamespace(data=[SimpleNamespace(time=4)]))
        reported = []
        client.reportAiVideoProcess = lambda *args, **kwargs: reported.append(args[4])
        client.fuckAiVideo(1, 2, 3, 4)
        self.assertEqual(reported, [1, 2, 3, 4])

    def test_prompt_actual_generation_truncates_integer_tokens(self):
        namespace = load_units("zhs/fucker.py", ["Openai.generateAnswer"], {"re": re, "json": json, "logger": Mock()})
        client = namespace["Openai"]()
        client.encoder = Mock()
        client.encoder.encode.return_value = list(range(30000))
        client.encoder.decode.return_value = "truncated"
        client.useZhidao = False
        client.openaiCompletion = Mock(return_value='```answer\n[{"id": "A", "content": "ok"}]\n```')
        self.assertEqual(client.generateAnswer("long"), ["A"])
        client.encoder.decode.assert_called_once_with(list(range(2100, 30000)))
        client.openaiCompletion.assert_called_once_with("truncated")


if __name__ == "__main__":
    unittest.main()
