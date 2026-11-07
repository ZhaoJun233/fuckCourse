"""Offline request policy regressions; no platform, credentials or Cookie files."""
import runpy
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from tests.source_units import load_units

POLICY = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'chaoxing/api/request_policy.py'))


def add_cookie(jar, name, domain='.chaoxing.com', path='/'):
    jar.set_cookie(Cookie(
        version=0, name=name, value='offline', port=None, port_specified=False,
        domain=domain, domain_specified=bool(domain), domain_initial_dot=domain.startswith('.'),
        path=path, path_specified=True, secure=False, expires=None, discard=True,
        comment=None, comment_url=None, rest={}))


class VirtualClock:
    def __init__(self):
        self.now = 10.0
        self.delays = []

    def clock(self):
        return self.now

    def sleep(self, delay):
        self.delays.append(delay)
        self.now += delay


class RequestSettingsTests(unittest.TestCase):
    def test_defaults_leave_custom_study_settings_untouched(self):
        config = {'jobs': 7, 'speed': 1.7, 'prefetch_work': False}
        POLICY['validate_request_settings'](config)
        self.assertEqual(7, config['jobs'])
        self.assertEqual(1.7, config['speed'])
        self.assertIs(False, config['prefetch_work'])
        self.assertEqual(0.5, config['request_interval'])
        self.assertEqual(0.2, config['request_jitter'])
        self.assertEqual([], config['cookie_cleanup_names'])

    def test_custom_timing_and_names_are_normalized(self):
        config = {'request_interval': '0.75', 'request_jitter': '0',
                  'cookie_cleanup_names': ' local_marker, local_marker, second_marker '}
        POLICY['validate_request_settings'](config)
        self.assertEqual(0.75, config['request_interval'])
        self.assertEqual(0.0, config['request_jitter'])
        self.assertEqual(['local_marker', 'second_marker'], config['cookie_cleanup_names'])

    def test_supported_timing_boundaries(self):
        for interval in (0.1, 30):
            for jitter in (0, 5):
                with self.subTest(interval=interval, jitter=jitter):
                    config = {'request_interval': interval, 'request_jitter': jitter}
                    POLICY['validate_request_settings'](config)
                    self.assertEqual(float(interval), config['request_interval'])
                    self.assertEqual(float(jitter), config['request_jitter'])

    def test_invalid_timing_does_not_partially_mutate_config(self):
        cases = {'request_interval': (0, 0.09, 30.01), 'request_jitter': (-0.1, 5.01)}
        invalid_common = (True, False, None, [], {}, '', 'bad', float('nan'), float('inf'))
        for key, invalid in cases.items():
            for value in invalid + invalid_common:
                with self.subTest(key=key, value=value):
                    config = {'jobs': 7, 'speed': 1.7, 'request_interval': '0.75',
                              'request_jitter': '0.1', 'cookie_cleanup_names': 'local_marker'}
                    config[key] = value
                    before = deepcopy(config)
                    with self.assertRaises(ValueError):
                        POLICY['validate_request_settings'](config)
                    # NaN is not equal to itself, so verify the preserved object for that field.
                    self.assertIs(value, config[key])
                    for unchanged in set(config) - {key}:
                        self.assertEqual(before[unchanged], config[unchanged])

    def test_invalid_names_do_not_partially_mutate_config_or_echo_values(self):
        config = {'request_interval': '0.75', 'request_jitter': '0.1',
                  'cookie_cleanup_names': 'local_marker=private-offline-value'}
        before = deepcopy(config)
        with self.assertRaises(ValueError) as raised:
            POLICY['validate_request_settings'](config)
        self.assertEqual(before, config)
        self.assertNotIn('private-offline-value', str(raised.exception))


class CookieMaintenanceTests(unittest.TestCase):
    def test_empty_names_are_a_no_op_without_accessing_the_jar(self):
        jar = Mock()
        for names in (None, '', ' ', [], ()):
            with self.subTest(names=names):
                self.assertEqual(0, POLICY['clear_cookie_markers'](jar, names))
        self.assertEqual([], jar.mock_calls)

    def test_normalization_preserves_case_and_deduplicates(self):
        self.assertEqual(('marker', 'Marker', 'second'),
                         POLICY['normalize_cookie_cleanup_names'](' marker, Marker, marker, second '))
        self.assertEqual(('marker',), POLICY['normalize_cookie_cleanup_names'](('marker', ' marker ')))

    def test_authentication_and_verification_names_are_protected(self):
        names = ('JSESSIONID', 'UID', '_uid', '_d', 'route', 'fid', 'uf', 'vc3',
                 'xxtenc', 'SID', 'cx_p_token', 'p_auth_token', 'csrf_key', 'xsrf_key',
                 'passport', 'sso_id', 'captcha_ticket', 'challenge_pass',
                 'turnstile', 'cf_clearance', '__cf_bm', '__cf_unknown',
                 '__Host-JSESSIONID', '__Secure-sessionid', '__Host-UID', '__Secure-_uid',
                 '__Host-SID', '__Secure-fid', '__Host-route', '__Secure-vc3')
        for name in names:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    POLICY['normalize_cookie_cleanup_names']([name])

    def test_invalid_names_are_rejected_before_any_cookie_is_cleared(self):
        jar = Mock()
        for names in ('marker=offline', 'Cookie: marker=offline', 'marker;other',
                      'marker\nother', ',marker', 'marker,', 'marker,,other',
                      ['marker', 'JSESSIONID'], ['marker', None], {'marker': 'offline'}):
            with self.subTest(names=names):
                with self.assertRaises(ValueError):
                    POLICY['clear_cookie_markers'](jar, names)
        self.assertEqual([], jar.mock_calls)

    def test_cleanup_is_name_and_domain_scoped_and_keeps_authentication(self):
        jar = CookieJar()
        for domain in ('', 'chaoxing.com', '.chaoxing.com', 'fanya.chaoxing.com'):
            add_cookie(jar, 'marker', domain)
        add_cookie(jar, 'marker', 'CHAOXING.COM', '/uppercase')
        add_cookie(jar, 'marker', '.chaoxing.com', '/study')
        for domain in ('example.com', 'notchaoxing.com', 'chaoxing.com.example.com'):
            add_cookie(jar, 'marker', domain)
        add_cookie(jar, 'Marker')
        add_cookie(jar, 'JSESSIONID')
        add_cookie(jar, 'p_auth_token')
        add_cookie(jar, 'unrelated')
        self.assertEqual(6, POLICY['clear_cookie_markers'](jar, ['marker']))
        remaining = {(c.name, c.domain, c.path) for c in jar}
        self.assertEqual({('marker', 'example.com', '/'), ('marker', 'notchaoxing.com', '/'),
                          ('marker', 'chaoxing.com.example.com', '/'),
                          ('Marker', '.chaoxing.com', '/'), ('JSESSIONID', '.chaoxing.com', '/'),
                          ('p_auth_token', '.chaoxing.com', '/'), ('unrelated', '.chaoxing.com', '/')},
                         remaining)
        self.assertEqual(0, POLICY['clear_cookie_markers'](jar, ['marker']))

    def test_shared_store_persistence_preserves_other_platforms(self):
        root = {'chaoxing': {'marker': 'offline'},
                'zhihuishu': {'login': 'other-offline'}, 'other_platform': {'session': 'offline'}}
        read_store = Mock(return_value=deepcopy(root))
        write_store = Mock()
        units = load_units('chaoxing/api/cookies.py', ['save_cookies'], {
            'requests': SimpleNamespace(Session=object),
            '_read_root_cookies': read_store, '_write_root_cookies': write_store})
        session = SimpleNamespace(cookies=SimpleNamespace(items=lambda: [('JSESSIONID', 'offline')]))
        units['save_cookies'](session)
        write_store.assert_called_once_with({
            'chaoxing': {'JSESSIONID': 'offline'},
            'zhihuishu': root['zhihuishu'], 'other_platform': root['other_platform']})


class RequestPacerTests(unittest.TestCase):
    def make_pacer(self, clock, *, interval=0.5, jitter=0):
        return POLICY['RequestPacer'](interval, jitter, clock=clock.clock,
                                      sleep=clock.sleep, uniform=lambda low, high: high)

    def test_custom_intervals_and_jitter_apply_to_each_actual_grant(self):
        clock = VirtualClock()
        pacer = self.make_pacer(clock, interval=0.75, jitter=0.2)
        for _ in range(3):
            self.assertTrue(pacer.wait())
        self.assertEqual(3, len(clock.delays))
        for delay in clock.delays:
            self.assertAlmostEqual(0.95, delay)
        self.assertAlmostEqual(12.85, clock.now)

    def test_delayed_wakeup_does_not_allow_the_next_caller_to_burst(self):
        clock = VirtualClock()
        def delayed_sleep(delay):
            clock.sleep(delay)
            if len(clock.delays) == 1:
                clock.now += 4
        pacer = POLICY['RequestPacer'](0.5, 0, clock=clock.clock,
                                      sleep=delayed_sleep, uniform=lambda low, high: 0)
        self.assertTrue(pacer.wait())
        self.assertTrue(pacer.wait())
        self.assertEqual([0.5, 0.5], clock.delays)
        self.assertEqual(15, clock.now)

    def test_cancellation_before_wait_never_sleeps_or_changes_the_gate(self):
        clock = VirtualClock()
        pacer = self.make_pacer(clock)
        stop = threading.Event()
        stop.set()
        self.assertFalse(pacer.wait(stop))
        self.assertEqual([], clock.delays)
        self.assertTrue(pacer.wait())
        self.assertEqual([0.5], clock.delays)

    def test_cancellation_during_wait_does_not_grant_a_request(self):
        clock = VirtualClock()
        stop = Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        pacer = self.make_pacer(clock)
        self.assertFalse(pacer.wait(stop))
        stop.wait.assert_called_once_with(0.5)
        self.assertEqual([], clock.delays)
        self.assertEqual(10, clock.now)

    def test_stop_set_at_the_end_of_wait_is_checked_again(self):
        clock = VirtualClock()
        stopped = threading.Event()
        stop = SimpleNamespace(is_set=stopped.is_set)
        def wait(delay):
            clock.sleep(delay)
            stopped.set()
            return False
        stop.wait = wait
        self.assertFalse(self.make_pacer(clock).wait(stop))
        self.assertEqual([0.5], clock.delays)

    def test_concurrent_waiters_share_one_gate_without_pre_reserved_slots(self):
        clock = VirtualClock()
        entered, release = threading.Event(), threading.Event()
        def gated_sleep(delay):
            if not clock.delays:
                entered.set()
                if not release.wait(2):
                    raise AssertionError('Test did not release pacing gate')
            clock.sleep(delay)
        pacer = POLICY['RequestPacer'](0.5, 0, clock=clock.clock,
                                      sleep=gated_sleep, uniform=lambda low, high: 0)
        with ThreadPoolExecutor(max_workers=6) as pool:
            first = pool.submit(pacer.wait)
            try:
                self.assertTrue(entered.wait(2))
                rest = [pool.submit(pacer.wait) for _ in range(5)]
                acquired = pacer._lock.acquire(blocking=False)
                if acquired:
                    pacer._lock.release()
                self.assertFalse(acquired)
            finally:
                release.set()
            self.assertTrue(first.result(timeout=2))
            self.assertTrue(all(future.result(timeout=2) for future in rest))
        self.assertEqual([0.5] * 6, clock.delays)
        self.assertEqual(13, clock.now)

    def test_cancellation_while_queued_for_the_gate_is_rechecked(self):
        clock = VirtualClock()
        entered, release, checked = threading.Event(), threading.Event(), threading.Event()
        stopped = threading.Event()
        def gated_sleep(delay):
            entered.set()
            if not release.wait(2):
                raise AssertionError('Test did not release pacing gate')
            clock.sleep(delay)
        def is_set():
            result = stopped.is_set()
            checked.set()
            return result
        stop = SimpleNamespace(is_set=is_set, wait=Mock())
        pacer = POLICY['RequestPacer'](0.5, 0, clock=clock.clock,
                                      sleep=gated_sleep, uniform=lambda low, high: 0)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(pacer.wait)
            try:
                self.assertTrue(entered.wait(2))
                queued = pool.submit(pacer.wait, stop)
                self.assertTrue(checked.wait(2))
                stopped.set()
            finally:
                release.set()
            self.assertTrue(first.result(timeout=2))
            self.assertFalse(queued.result(timeout=2))
        stop.wait.assert_not_called()
        self.assertEqual([0.5], clock.delays)


class RequestActivityTests(unittest.TestCase):
    def test_stopped_requests_are_not_admitted(self):
        activity = POLICY['RequestActivity']()
        stop = threading.Event()
        stop.set()
        self.assertFalse(activity.begin(stop))
        activity.wait_for_idle()

    def test_multiple_requests_must_all_finish_before_cleanup_can_proceed(self):
        activity = POLICY['RequestActivity']()
        stop = threading.Event()
        self.assertTrue(activity.begin(stop))
        self.assertTrue(activity.begin(stop))
        entered = threading.Event()
        original_wait = activity._condition.wait
        def observed_wait(timeout=None):
            entered.set()
            return original_wait(timeout)
        activity._condition.wait = observed_wait
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(activity.wait_for_idle)
            try:
                self.assertTrue(entered.wait(2))
                activity.end()
                self.assertFalse(waiting.done())
                stop.set()
                self.assertFalse(activity.begin(stop))
            finally:
                activity.end()
            self.assertIsNone(waiting.result(timeout=2))

    def test_unmatched_completion_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'No active course request to finish'):
            POLICY['RequestActivity']().end()


if __name__ == '__main__':
    unittest.main()
