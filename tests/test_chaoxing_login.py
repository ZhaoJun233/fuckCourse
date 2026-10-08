"""Offline login regressions against actual Chaoxing definitions."""
import sys
import traceback
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from tests.source_units import load_units


class LoginError(Exception):
    pass


class TestChaoxingLogin(unittest.TestCase):
    def setUp(self):
        block = patch.object(requests.sessions.Session, "request",
                             side_effect=AssertionError("Network forbidden"))
        block.start()
        self.addCleanup(block.stop)
        self.session = Mock()
        self.response = Mock()
        self.response.json.return_value = {"status": False, "msg2": "wrong password"}
        self.session.post.return_value = self.response
        factory = patch.object(requests, "Session", return_value=self.session)
        factory.start()
        self.addCleanup(factory.stop)
        self.env = {
            "requests": requests, "RequestException": requests.RequestException,
            "Timeout": requests.Timeout, "logger": Mock(), "gc": SimpleNamespace(HEADERS={}),
            "SessionManager": Mock(), "save_cookies": Mock(),
            "_save_credentials_to_config": Mock(), "getpass": Mock(return_value="correct-password"),
            "input": Mock(), "LoginError": LoginError,
        }
        load_units("chaoxing/api/base.py", ["Chaoxing.login"], self.env)
        self.client = self.env["Chaoxing"]()
        self.client.account = SimpleNamespace(username="synthetic-user", password="wrong-password")
        self.client.cipher = SimpleNamespace(encrypt=lambda value: "ENC:" + value)
        self.client._validate_cookie_session = Mock(return_value=False)

    def retry_login(self, cookies=False):
        function = load_units("chaoxing/main.py", ["login_with_retry"], self.env)["login_with_retry"]
        return function(self.client, login_with_cookies=cookies)

    def test_password_login_has_bounded_connect_and_read_timeout(self):
        self.client.login()
        self.assertEqual(self.session.post.call_args.kwargs.get("timeout"), (5, 15))

    def test_wrong_password_does_not_save_credentials_or_cookies(self):
        self.assertFalse(self.client.login()["status"])
        self.env["_save_credentials_to_config"].assert_not_called()
        self.env["save_cookies"].assert_not_called()

    def test_successful_password_login_saves_verified_credentials(self):
        self.response.json.return_value = {"status": True}
        self.assertTrue(self.client.login()["status"])
        self.env["_save_credentials_to_config"].assert_called_once_with("synthetic-user", "wrong-password")
        self.env["save_cookies"].assert_called_once_with(self.session)

    def test_expired_cookie_prompt_does_not_save_failed_credentials(self):
        self.client.account = SimpleNamespace(username="", password="")
        self.env["input"].side_effect = ["synthetic-user", "wrong-password"]
        self.assertFalse(self.client.login(login_with_cookies=True)["status"])
        self.env["_save_credentials_to_config"].assert_not_called()
        self.env["getpass"].assert_called_once()

    def test_valid_cookies_do_not_persist_unverified_password(self):
        self.client._validate_cookie_session.return_value = True
        self.assertTrue(self.client.login(login_with_cookies=True)["status"])
        self.session.post.assert_not_called()
        self.env["_save_credentials_to_config"].assert_not_called()

    def test_wrong_password_can_retry_and_only_verified_password_is_saved(self):
        good = Mock()
        good.json.return_value = {"status": True}
        self.session.post.side_effect = [self.response, good]
        self.env["input"].side_effect = ["y", ""]
        self.assertTrue(self.retry_login(cookies=True)["status"])
        self.assertEqual(self.session.post.call_count, 2)
        self.client._validate_cookie_session.assert_called_once()
        self.assertEqual(self.session.post.call_args.kwargs["data"]["password"], "ENC:correct-password")
        self.env["_save_credentials_to_config"].assert_called_once_with("synthetic-user", "correct-password")

    def test_user_can_cancel_retry_without_saving_or_another_request(self):
        self.env["input"].return_value = "n"
        with self.assertRaises(LoginError):
            self.retry_login()
        self.assertEqual(self.session.post.call_count, 1)
        self.env["getpass"].assert_not_called()
        self.env["_save_credentials_to_config"].assert_not_called()

    def test_empty_retry_password_stops_without_another_request(self):
        self.env["input"].side_effect = ["y", ""]
        self.env["getpass"].return_value = ""
        with self.assertRaises(LoginError):
            self.retry_login()
        self.assertEqual(self.session.post.call_count, 1)
        self.env["_save_credentials_to_config"].assert_not_called()

    def test_network_failure_exits_without_password_retry_or_save(self):
        for error in (requests.Timeout("synthetic timeout"),
                      requests.ConnectionError("synthetic network error")):
            with self.subTest(error=type(error).__name__):
                self.session.post.side_effect = error
                with self.assertRaises(LoginError):
                    self.retry_login()
        self.env["input"].assert_not_called()
        self.env["_save_credentials_to_config"].assert_not_called()

    def test_http_error_does_not_retry_as_wrong_password(self):
        self.response.raise_for_status.side_effect = requests.HTTPError("synthetic HTTP error")
        with self.assertRaises(LoginError):
            self.retry_login()
        self.env["input"].assert_not_called()
        self.env["_save_credentials_to_config"].assert_not_called()

    def test_platform_main_uses_retry_flow_before_fetching_courses(self):
        good = Mock()
        good.json.return_value = {"status": True}
        self.session.post.side_effect = [self.response, good]
        self.env["input"].side_effect = ["y", ""]
        self.client.get_course_list = Mock(return_value=[])
        notification = Mock()
        notification.get_notification_from_config.return_value = notification
        self.env.update({
            "init_config": lambda: ({"use_cookies": False}, {}, {}),
            "init_chaoxing": lambda *args: self.client,
            "Notification": lambda: notification,
            "filter_courses": lambda *args: [], "sys": sys, "traceback": traceback,
        })
        functions = load_units("chaoxing/main.py", ["login_with_retry", "main"], self.env)
        functions["main"]()
        self.assertEqual(self.session.post.call_count, 2)
        self.client.get_course_list.assert_called_once()

    def test_initial_prompt_does_not_save_before_login(self):
        tiku = Mock()
        tiku.get_tiku_from_config.return_value = tiku
        self.env.update({"Account": lambda u, p: SimpleNamespace(username=u, password=p),
                         "Tiku": lambda: tiku, "Chaoxing": Mock()})
        self.env["input"].side_effect = ["synthetic-user", "wrong-password"]
        function = load_units("chaoxing/main.py", ["init_chaoxing"], self.env)["init_chaoxing"]
        with patch.dict(sys.modules, {"api.cookies": SimpleNamespace(use_cookies=lambda: {})}):
            function({}, {})
        self.env["_save_credentials_to_config"].assert_not_called()
        self.env["getpass"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
