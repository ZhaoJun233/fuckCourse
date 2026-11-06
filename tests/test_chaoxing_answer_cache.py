"""Offline cache/query regressions: isolated source units, temporary files, no providers."""

import json
import os
import re
import shutil
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from unittest.mock import MagicMock, Mock, patch

from tests.source_units import load_units


class AnswerCacheTests(unittest.TestCase):
    def setUp(self):
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.cache_path = Path(temporary_directory.name) / "cache.json"
        # Executing only these definitions avoids imports that initialize real providers.
        check_answer = load_units(
            "chaoxing/api/answer_check.py",
            ["check_answer", "check_single", "check_multiple", "check_judgement",
             "check_completion", "cut"],
        )["check_answer"]
        self.logger = Mock()
        self.namespace = load_units(
            "chaoxing/api/answer.py", ("CacheDAO", "Tiku", "TikuFallback",
                                     "_normalize_question_title", "question_cache_key"),
            {"json": json, "os": os, "shutil": shutil, "tempfile": tempfile,
             "threading": threading, "time": time, "deepcopy": deepcopy,
             "Path": Path, "Optional": Optional, "sub": re.sub,
             "logger": self.logger, "check_answer": check_answer},
        )
        self.CacheDAO = self.namespace["CacheDAO"]
        self.CacheDAO.DEFAULT_CACHE_FILE = str(self.cache_path)
        self.Tiku = self.namespace["Tiku"]

    def provider(self, answer="A"):
        provider = self.Tiku()
        provider.name = "offline-provider"
        provider._query = Mock(return_value=answer)
        return provider

    @staticmethod
    def question(**changes):
        question = {"title": "相同题干", "type": "single",
                    "options": ["A. 第一项", "B. 第二项"]}
        question.update(changes)
        return question

    def stored_cache(self):
        return json.loads(self.cache_path.read_text(encoding="utf8"))

    def test_complete_options_and_order_distinguish_same_title(self):
        provider = self.provider()
        provider._query.side_effect = ["A", "B", "A"]
        original = self.question()
        changed_last_option = self.question(options=["A. 第一项", "B. 不同项"])
        reordered = self.question(options=list(reversed(original["options"])))
        for question, answer in [(original, "A"), (changed_last_option, "B"),
                                 (reordered, "A"), (original, "A")]:
            self.assertEqual(provider.query(question), answer)
        self.assertEqual(provider._query.call_count, 3)
        self.assertEqual(len(self.stored_cache()), 3)

    def test_public_key_is_pure_and_matches_query_for_spaced_number_prefix(self):
        key_for = self.namespace["question_cache_key"]
        question = self.question(title="12 34相同题干（1.0分）")
        original = deepcopy(question)
        key = key_for(question)
        self.assertEqual(question, original)
        self.assertIsNone(self.CacheDAO._instance)
        self.assertFalse(self.cache_path.exists())
        self.assertEqual(self.provider().query(question), "A")
        self.assertEqual(self.stored_cache(), {key: "A"})
        self.assertEqual(question, original)

    def test_string_options_are_preserved_in_full(self):
        key_for = self.namespace["question_cache_key"]
        first = self.question(options="A. 第一项\nB. 第二项")
        second = self.question(options="A. 第一项\nB. 不同项")
        self.assertNotEqual(key_for(first), key_for(second))
        self.assertEqual(json.loads(key_for(first)[3:])["options"], first["options"])

    def test_public_key_rejects_incomplete_or_nonserializable_question(self):
        key_for = self.namespace["question_cache_key"]
        with self.assertRaises(KeyError):
            key_for({"title": "missing type"})
        with self.assertRaises(TypeError):
            key_for(self.question(options=object()))

    def test_question_type_is_part_of_cache_identity(self):
        provider = self.provider()
        provider._query.side_effect = ["A", "A###B"]
        self.assertEqual(provider.query(self.question()), "A")
        self.assertEqual(provider.query(self.question(type="multiple")), "A###B")
        self.assertEqual(provider.query(self.question()), "A")
        self.assertEqual(provider._query.call_count, 2)

    def test_normalized_title_hits_versioned_cache_without_mutating_input(self):
        provider = self.provider(" A ")
        question = self.question(title=" 12相同题干（1.0分） ")
        original = deepcopy(question)
        self.assertEqual(provider.query(question), "A")
        self.assertEqual(question, original)
        self.assertEqual(provider.query(self.question()), "A")
        provider._query.assert_called_once_with(self.question())
        key, = self.stored_cache()
        self.assertTrue(key.startswith("v2:"))
        self.assertEqual(json.loads(key[len("v2:"):]), self.question())

    def test_exam_option_records_are_stable_but_include_all_fields(self):
        provider = self.provider()
        provider._query.side_effect = ["A", "B"]
        question = self.question(
            id="question-1", options=[{"label": "A", "text": "第一项", "value": "10"}]
        )
        same = self.question(
            id="question-2", options=[{"value": "10", "text": "第一项", "label": "A"}]
        )
        different = deepcopy(question)
        different["options"][0]["value"] = "11"
        self.assertEqual(provider.query(question), "A")
        self.assertEqual(provider.query(same), "A")
        self.assertEqual(provider.query(different), "B")
        self.assertEqual(provider._query.call_count, 2)

    def test_legacy_keys_are_retained_but_never_used_by_query(self):
        self.cache_path.write_text(
            json.dumps({"相同题干": "A", "另一道旧题": "旧答案"}), encoding="utf8"
        )
        provider = self.provider("B")
        self.assertEqual(provider.query(self.question()), "B")
        self.assertEqual(provider.query(self.question()), "B")
        provider._query.assert_called_once()
        self.assertEqual(self.CacheDAO().get_cache("相同题干"), "A")
        cache = self.stored_cache()
        self.assertEqual(cache["相同题干"], "A")
        self.assertEqual(cache["另一道旧题"], "旧答案")
        self.assertEqual(sum(key.startswith("v2:") for key in cache), 1)

    def test_query_deep_copies_nested_data_on_miss_and_hit(self):
        question = self.question(
            title="1相同题干（1.0分）",
            options=[{"label": "A", "text": "第一项", "extra": ["原始"]}],
            metadata={"tags": ["原始"]},
        )
        original = deepcopy(question)
        provider = self.provider()

        def mutate_provider_input(received):
            received["options"][0]["extra"].append("修改")
            received["metadata"]["tags"].clear()
            received["title"] = "provider 改写"
            return "A"

        provider._query.side_effect = mutate_provider_input
        self.assertEqual(provider.query(question), "A")
        self.assertEqual(question, original)
        self.assertEqual(provider.query(question), "A")
        self.assertEqual(question, original)
        provider._query.assert_called_once()

    def test_failures_empty_results_and_wrong_types_are_not_cached(self):
        failures = [None, "", "  ", "A###B", False, 0, {"error": "failed"}]
        for index, failed_answer in enumerate(failures):
            with self.subTest(answer=failed_answer):
                question = self.question(title=f"失败题目-{index}")
                provider = self.provider()
                provider._query.side_effect = [failed_answer, "B"]
                previous = dict(self.CacheDAO()._cache)
                self.assertIsNone(provider.query(question))
                self.assertEqual(self.CacheDAO()._cache, previous)
                self.assertEqual(provider.query(question), "B")
                self.assertEqual(provider.query(question), "B")
                self.assertEqual(provider._query.call_count, 2)

    def test_whitespace_short_answer_is_not_cached(self):
        provider = self.provider("   ")
        self.assertIsNone(provider.query(self.question(type="short")))
        self.assertEqual(self.CacheDAO()._cache, {})
        self.assertFalse(self.cache_path.exists())

    def test_provider_exception_preserves_input_and_allows_retry(self):
        question = self.question(metadata={"tags": ["original"]})
        original = deepcopy(question)
        provider = self.provider()

        def fail(received):
            received["metadata"]["tags"].clear()
            raise RuntimeError("offline failure")

        provider._query.side_effect = fail
        with self.assertRaisesRegex(RuntimeError, "offline failure"):
            provider.query(question)
        self.assertEqual(question, original)
        self.assertEqual(self.CacheDAO()._cache, {})
        provider._query.side_effect = None
        provider._query.return_value = "B"
        self.assertEqual(provider.query(question), "B")

    def test_disabled_query_does_not_initialize_cache_or_call_provider(self):
        provider = self.provider()
        provider.DISABLE = True
        question = self.question()
        original = deepcopy(question)
        self.assertIsNone(provider.query(question))
        provider._query.assert_not_called()
        self.assertIsNone(self.CacheDAO._instance)
        self.assertEqual(question, original)

    def test_fallback_keeps_provider_order_and_caches_success(self):
        unavailable = self.provider()
        unavailable._query.side_effect = RuntimeError("offline unavailable")
        invalid = self.provider("A###B")
        usable = self.provider("B")
        fallback = self.namespace["TikuFallback"]()
        fallback.providers = [unavailable, invalid, usable]
        question = self.question()
        original = deepcopy(question)
        self.assertEqual(fallback.query(question), "B")
        self.assertEqual(fallback.query(question), "B")
        for provider in fallback.providers:
            provider._query.assert_called_once()
        self.assertEqual(question, original)
        self.assertEqual(len(self.stored_cache()), 1)

    def test_optionless_questions_remain_supported(self):
        provider = self.provider("some answer")
        question = {"title": "简答题", "type": "short"}
        self.assertEqual(provider.query(question), "some answer")
        self.assertEqual(provider.query(question), "some answer")
        provider._query.assert_called_once_with(question)

    def test_concurrent_initialization_loads_once_and_waits_until_ready(self):
        workers = 12
        start = threading.Barrier(workers)
        load_started = threading.Event()
        release_load = threading.Event()
        original_read = self.CacheDAO._read_cache
        self.cache_path.write_text(json.dumps({"saved": "A"}), encoding="utf8")

        def blocked_read(cache):
            load_started.set()
            if not release_load.wait(5):
                raise AssertionError("test did not release cache initialization")
            return original_read(cache)

        def construct():
            start.wait(timeout=5)
            cache = self.CacheDAO()
            return cache, cache.get_cache("saved")

        with patch.object(self.CacheDAO, "_read_cache", autospec=True,
                          side_effect=blocked_read) as read_cache:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [executor.submit(construct) for _ in range(workers)]
                try:
                    self.assertTrue(load_started.wait(5))
                    self.assertFalse(any(future.done() for future in futures))
                finally:
                    release_load.set()
                results = [future.result(timeout=5) for future in futures]
            read_cache.assert_called_once()
        first_cache = results[0][0]
        self.assertTrue(all(cache is first_cache for cache, _ in results))
        self.assertEqual([answer for _, answer in results], ["A"] * workers)

    def test_repeated_construction_does_not_reset_cache_or_change_path(self):
        cache = self.CacheDAO()
        cache.add_cache("saved", "A")
        with patch.object(self.CacheDAO, "_read_cache", side_effect=AssertionError("reload")):
            same = self.CacheDAO(str(self.cache_path.with_name("other.json")))
        self.assertIs(same, cache)
        self.assertEqual(same.cache_file, self.cache_path)
        self.assertEqual(same.get_cache("saved"), "A")

    def test_concurrent_reads_and_writes_preserve_all_entries_on_disk(self):
        workers = 12
        cache = self.CacheDAO()
        start = threading.Barrier(workers)

        def write_and_read(index):
            start.wait(timeout=5)
            for number in range(3):
                key = f"question-{index}-{number}"
                cache.add_cache(key, f"answer-{index}-{number}")
                self.assertEqual(cache.get_cache(key), f"answer-{index}-{number}")

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(write_and_read, index) for index in range(workers)]
            for future in futures:
                future.result(timeout=10)
        expected = {f"question-{index}-{number}": f"answer-{index}-{number}"
                    for index in range(workers) for number in range(3)}
        self.assertEqual(cache._cache, expected)
        self.assertEqual(self.stored_cache(), expected)
        self.logger.error.assert_not_called()

    def test_reads_use_the_same_lock_as_writes(self):
        cache = self.CacheDAO()
        cache.add_cache("saved", "A")
        real_lock = cache._lock
        entered = []

        class RecordingLock:
            def __enter__(self):
                entered.append(True)
                return real_lock.__enter__()

            def __exit__(self, *args):
                return real_lock.__exit__(*args)

        cache._lock = RecordingLock()
        self.assertEqual(cache.get_cache("saved"), "A")
        self.assertEqual(entered, [True])


class AIClientLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.client = self.client_stub()
        self.http_client = self.client_stub()
        self.openai = Mock(return_value=self.client)
        self.httpx = SimpleNamespace(Client=Mock(return_value=self.http_client))
        namespace = load_units(
            "chaoxing/api/answer.py", ["Tiku", "AI", "_remove_md_json_wrapper"],
            {"os": os, "re": re, "json": json, "time": time, "logger": Mock(),
             "OpenAI": self.openai, "httpx": self.httpx,
             "ExitStack": ExitStack, "contextmanager": contextmanager},
        )
        self.provider = namespace["AI"]()
        self.provider.http_proxy = ""
        self.provider.endpoint = "https://offline.invalid/v1"
        self.provider.key = "offline-placeholder"
        self.provider.model = "offline-model"
        self.provider.min_interval_seconds = 0
        self.question = {"title": "测试题目", "type": "single",
                         "options": "A 第一项\nB 第二项"}
        self.completions = self.client.chat.completions.create
        self.completions.return_value = SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content=
                json.dumps({"Answer": ["第一项"]})))
        ])

    @staticmethod
    def client_stub():
        client = MagicMock()
        client.__enter__.return_value = client
        client.close.return_value = None
        client.__exit__.side_effect = lambda *args: client.close()
        return client

    def assert_bounded_client(self, proxy=False):
        expected = {"base_url": self.provider.endpoint, "api_key": self.provider.key,
                    "timeout": 30, "max_retries": 0}
        if proxy:
            expected["http_client"] = self.http_client
            self.httpx.Client.assert_called_once_with(
                proxy=self.provider.http_proxy, timeout=30
            )
            self.http_client.close.assert_called_once()
        else:
            self.httpx.Client.assert_not_called()
        self.openai.assert_called_once_with(**expected)
        self.client.close.assert_called_once()

    def test_query_uses_timeout_without_retries_and_closes_client(self):
        self.assertEqual(self.provider._query(self.question), "第一项")
        self.completions.assert_called_once()
        self.assert_bounded_client()

    def test_proxy_query_closes_both_clients(self):
        self.provider.http_proxy = "http://offline-proxy.invalid:8080"
        self.assertEqual(self.provider._query(self.question), "第一项")
        self.assert_bounded_client(proxy=True)

    def test_query_failure_propagates_and_closes_without_retry(self):
        self.completions.side_effect = TimeoutError("offline request timed out")
        with self.assertRaisesRegex(TimeoutError, "offline request timed out"):
            self.provider._query(self.question)
        self.completions.assert_called_once()
        self.assert_bounded_client()

    def test_malformed_query_response_closes_proxy_clients(self):
        self.provider.http_proxy = "http://offline-proxy.invalid:8080"
        self.completions.return_value.choices[0].message.content = "not JSON"
        self.assertIsNone(self.provider._query(self.question))
        self.assert_bounded_client(proxy=True)

    def test_connection_check_uses_bounded_context(self):
        self.assertTrue(self.provider.check_llm_connection())
        self.completions.assert_called_once()
        self.assert_bounded_client()

    def test_failed_connection_check_closes_proxy_clients_without_retry(self):
        self.provider.http_proxy = "http://offline-proxy.invalid:8080"
        self.completions.side_effect = TimeoutError("offline check timed out")
        self.assertFalse(self.provider.check_llm_connection())
        self.completions.assert_called_once()
        self.assert_bounded_client(proxy=True)

    def test_sdk_constructor_failure_still_closes_proxy_transport(self):
        self.provider.http_proxy = "http://offline-proxy.invalid:8080"
        self.openai.side_effect = RuntimeError("offline initialization failed")
        with self.assertRaisesRegex(RuntimeError, "offline initialization failed"):
            self.provider._query(self.question)
        self.http_client.close.assert_called_once()
        self.completions.assert_not_called()
        self.openai.assert_called_once()


if __name__ == "__main__":
    unittest.main()
