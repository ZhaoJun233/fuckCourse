"""Cancellable course request pacing and opt-in, name-only Cookie maintenance.

Cookie maintenance never retries a rejected request. Authentication and
challenge/clearance Cookies are protected, and an empty list changes nothing.
"""

import math
import random
import re
import threading
import time


DEFAULT_REQUEST_INTERVAL = 0.5
DEFAULT_REQUEST_JITTER = 0.2

# Chaoxing identity/session Cookies and common authentication/verification names.
_PROTECTED_COOKIE_NAMES = frozenset({
    "_uid", "uid", "_d", "route", "fid", "uf", "cx_p", "cxid", "uc", "uc2", "uc3",
    "vc", "vc2", "vc3", "cxtoken", "xxtenc", "xxtenc2", "jsessionid",
    "session", "sessionid", "sid", "sso", "passport", "csrf", "xsrf",
    "cf_clearance", "__cf_bm",
})
_PROTECTED_COOKIE_TERMS = (
    "auth", "session", "token", "csrf", "xsrf", "sso", "passport",
    "captcha", "challenge", "turnstile",
)
_COOKIE_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")


def _timing_value(value, label, minimum, maximum):
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value)
        if not math.isfinite(number) or not minimum <= number <= maximum:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{label}必须是 {minimum:g}–{maximum:g} 秒的有限数值。") from None
    return number


def normalize_cookie_cleanup_names(value):
    """Accept names only; validate the whole list before any Cookie is touched."""
    if value is None or isinstance(value, str) and not value.strip():
        return ()
    if isinstance(value, str):
        names = value.split(",")
    elif isinstance(value, (list, tuple)):
        names = value
    else:
        raise ValueError("Cookie 清理名单必须是名称列表或逗号分隔的名称，不能填写 Cookie 值。")

    result = []
    seen = set()
    for item in names:
        if not isinstance(item, str) or not _COOKIE_NAME.fullmatch(item.strip()):
            raise ValueError("Cookie 清理名单只能包含有效名称，不能填写值或 Cookie 请求头。")
        name = item.strip()
        folded = name.lower()
        # Standard Cookie prefixes do not turn an identity Cookie into a marker.
        for prefix in ('__host-', '__secure-'):
            if folded.startswith(prefix):
                folded = folded[len(prefix):]
                break
        if (folded in _PROTECTED_COOKIE_NAMES or folded.startswith("__cf_")
                or any(term in folded for term in _PROTECTED_COOKIE_TERMS)):
            raise ValueError(f"不能清理会话、登录或验证 Cookie：{name}。")
        if name not in seen:
            result.append(name)
            seen.add(name)
    return tuple(result)


def validate_request_settings(config):
    """Normalize request policy settings without changing video speed or jobs."""
    interval = _timing_value(config.get("request_interval", DEFAULT_REQUEST_INTERVAL),
                             "请求基础间隔", 0.1, 30.0)
    jitter = _timing_value(config.get("request_jitter", DEFAULT_REQUEST_JITTER),
                           "请求附加间隔", 0.0, 5.0)
    names = normalize_cookie_cleanup_names(config.get("cookie_cleanup_names", []))
    config.update(request_interval=interval, request_jitter=jitter,
                  cookie_cleanup_names=list(names))


def clear_cookie_markers(cookie_jar, names):
    """Remove opted-in names only from the Chaoxing jar, never foreign domains.

    Empty-domain Cookies are created by the application's saved-Cookie loader.
    Domain and path are preserved for every retained Cookie. Call before workers
    start or after all in-flight requests have stopped, never during updates.
    """
    selected = frozenset(normalize_cookie_cleanup_names(names))
    if not selected:
        return 0
    matches = []
    for cookie in cookie_jar:
        domain = cookie.domain.lstrip(".").lower()
        belongs_to_platform = not domain or domain == "chaoxing.com" or domain.endswith(".chaoxing.com")
        if belongs_to_platform and cookie.name in selected:
            matches.append((cookie.domain, cookie.path, cookie.name))
    for domain, path, name in matches:
        cookie_jar.clear(domain=domain, path=path, name=name)
    return len(matches)


class RequestActivity:
    """Track authorized requests so Cookie maintenance can wait for responses."""

    def __init__(self):
        self._condition = threading.Condition()
        self._active = 0

    def begin(self, stop_event):
        with self._condition:
            if stop_event.is_set():
                return False
            self._active += 1
            return True

    def end(self):
        with self._condition:
            if self._active <= 0:
                raise RuntimeError("No active course request to finish")
            self._active -= 1
            if self._active == 0:
                self._condition.notify_all()

    def wait_for_idle(self):
        with self._condition:
            self._condition.wait_for(lambda: self._active == 0)


class RequestPacer:
    """Share a cancellation-aware dispatch gate across all course workers.

    Wait under the lock and timestamp the actual grant, rather than reserving
    future slots. Delayed threads therefore cannot all consume old reservations
    and burst together after a slow wakeup. The HTTP response itself is not held
    under this lock, so chapter requests can still be in flight concurrently.
    """

    def __init__(self, interval=DEFAULT_REQUEST_INTERVAL, jitter=DEFAULT_REQUEST_JITTER,
                 *, clock=time.monotonic, sleep=time.sleep, uniform=random.uniform):
        self.interval = _timing_value(interval, "请求基础间隔", 0.1, 30.0)
        self.jitter = _timing_value(jitter, "请求附加间隔", 0.0, 5.0)
        self._clock = clock
        self._sleep = sleep
        self._uniform = uniform
        self._lock = threading.Lock()
        self._last_dispatch = clock()

    def wait(self, stop_event=None):
        if stop_event is not None and stop_event.is_set():
            return False
        with self._lock:
            if stop_event is not None and stop_event.is_set():
                return False
            base_wait = max(self._last_dispatch + self.interval - self._clock(), 0.0)
            delay = base_wait + self._uniform(0.0, self.jitter)
            if stop_event is None:
                self._sleep(delay)
            elif stop_event.wait(delay):
                return False
            if stop_event is not None and stop_event.is_set():
                return False
            self._last_dispatch = self._clock()
            return True
