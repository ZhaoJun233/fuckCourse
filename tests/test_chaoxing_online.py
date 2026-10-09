"""Synthetic fixtures only; no HAR, credentials or live requests."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock
import requests


ROOT = Path(__file__).resolve().parents[1]
COURSE = {'courseId': '11', 'clazzId': '22', 'cpi': '33'}
REFER = ('https://mooc1.chaoxing.com/mycourse/studentstudy?'
         'chapterId=44&courseId=11&clazzid=22&cpi=33&enc=synthetic')
PAGE = ('<input id="passSimulateValue" value="false">'
        '<input id="detectUrl" value="https://detect.chaoxing.com/'
        'api/passport2-onlineinfo.js?fid=55&amp;key=synthetic&amp;refer=synthetic">')
BOOTSTRAP = '''if (document.cookie.indexOf("UID") < 0) { return; }
var fid = 55;
var fn = function() {
 var refer = encodeURIComponent("%s");
 requestAjax({url: window.location.protocol+"//" + "detect.chaoxing.com" +
 "/api/monitor?version=" + 1000000 + "&refer=" +refer + "&fid="+fid,
 success: function(json) {}});
}; setInterval(fn, 30000);''' % REFER


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def time(self):
        return 1000 + self.now


def load_module():
    spec = importlib.util.spec_from_file_location('offline_online', ROOT / 'chaoxing/api/online.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OnlineTests(unittest.TestCase):
    def setUp(self):
        self.m = load_module()
        self.clock = Clock()

    def start(self, *, page=PAGE, bootstrap=BOOTSTRAP, status='true', authenticated=True,
              transfer_target=REFER, poll_status=200):
        session = Mock()
        session.cookies = requests.cookies.cookiejar_from_dict({'UID': 'synthetic'} if authenticated else {})
        transfer = Mock(status_code=302, headers={'Location': transfer_target})
        responses = [transfer, Mock(status_code=200, text=page),
                     Mock(status_code=200, text=bootstrap)]

        def get(url, **kw):
            if responses:
                return responses.pop(0)
            callback = kw['params']['jsoncallback']
            # Actual protocol returns a JSONP-wrapped string containing a JS object.
            text = callback + '(' + json.dumps('{status:' + status + ',refer:"",http:"https"}') + ')'
            return Mock(status_code=poll_status, text=text)

        session.get.side_effect = get
        monitor = self.m.OnlineMonitor.start(session, COURSE, '44', 'synthetic', '55', clock=self.clock)
        return monitor, session

    def test_initial_check_and_thirty_second_cadence(self):
        monitor, session = self.start()
        self.assertEqual(monitor.verified_checks, 1)
        self.assertEqual(session.get.call_count, 4)
        self.clock.now = 29.999
        monitor.check()
        self.assertEqual(session.get.call_count, 4)
        self.clock.now = 30
        monitor.check()
        self.assertEqual(session.get.call_count, 5)
        self.assertEqual(monitor.verified_checks, 2)
        self.assertTrue(all(c.kwargs['allow_redirects'] is False for c in session.get.call_args_list))

    def test_actual_generic_presence_refer_is_preserved(self):
        monitor, session = self.start(bootstrap=BOOTSTRAP.replace(REFER, 'http://i.mooc.chaoxing.com'))
        self.assertIn('refer=http%3A%2F%2Fi.mooc.chaoxing.com', monitor.url)
        self.assertEqual(monitor.verified_checks, 1)

    def test_actual_http_bootstrap_uses_native_https_upgrade(self):
        monitor, session = self.start(page=PAGE.replace('https://detect.', 'http://detect.'))
        self.assertTrue(session.get.call_args_list[2].args[0].startswith('https://detect.chaoxing.com/'))
        self.assertEqual(monitor.verified_checks, 1)

    def test_explicit_rejection_never_becomes_success(self):
        with self.assertRaises(self.m.OnlineDetectionError):
            self.start(status='false')

    def test_non_boolean_status_is_rejected(self):
        for status in ['1', '"true"', 'null']:
            with self.subTest(status=status), self.assertRaises(self.m.OnlineDetectionError):
                self.start(status=status)

    def test_failed_poll_latches_and_never_retries(self):
        monitor, session = self.start()
        session.get.side_effect = OSError('private-token-do-not-echo')
        self.clock.now = 30
        with self.assertRaises(self.m.OnlineDetectionError) as error:
            monitor.check()
        self.assertNotIn('private-token', str(error.exception))
        count = session.get.call_count
        with self.assertRaises(self.m.OnlineDetectionError):
            monitor.check()
        self.assertEqual(count, session.get.call_count)

    def test_login_cookie_prerequisite_is_not_bypassed(self):
        with self.assertRaises(self.m.OnlineDetectionError):
            self.start(authenticated=False)

    def test_login_or_external_redirect_is_not_followed(self):
        for target in ['https://passport2.chaoxing.com/login', 'https://example.invalid/?secret=token',
                       REFER.replace('chapterId=44', 'chapterId=99')]:
            with self.subTest(target=target), self.assertRaises(self.m.OnlineDetectionError):
                self.start(transfer_target=target)

    def test_non_success_http_cannot_pass_detection(self):
        for status in [302, 403, 500]:
            with self.subTest(status=status), self.assertRaises(self.m.OnlineDetectionError):
                self.start(poll_status=status)

    def test_missed_cycle_is_not_backfilled_as_a_pass(self):
        monitor, session = self.start()
        self.clock.now = 60
        with self.assertRaises(self.m.OnlineDetectionError):
            monitor.check()
        self.assertEqual(session.get.call_count, 4)

    def test_official_skip_does_not_invent_a_pass(self):
        monitor, session = self.start(page='<input id="passSimulateValue" value="true">')
        self.assertFalse(monitor.enabled)
        self.assertEqual(monitor.verified_checks, 0)
        monitor.check()
        self.assertEqual(session.get.call_count, 2)

    def test_missing_configuration_fails_closed(self):
        for page in ['', '<input id="passSimulateValue" value="false">']:
            with self.subTest(page=page), self.assertRaises(self.m.OnlineDetectionError):
                self.start(page=page)

    def test_foreign_bootstrap_host_is_not_contacted(self):
        with self.assertRaises(self.m.OnlineDetectionError):
            self.start(page=PAGE.replace('detect.chaoxing.com', 'example.invalid'))

    def test_wrong_course_or_school_configuration_is_rejected(self):
        for script in [BOOTSTRAP.replace('courseId=11', 'courseId=99'),
                       BOOTSTRAP.replace('fid = 55', 'fid = 66')]:
            with self.subTest(script=script), self.assertRaises(self.m.OnlineDetectionError):
                self.start(bootstrap=script)

    def test_unknown_script_or_interval_is_not_executed_or_guessed(self):
        for script in ['throw new Error("secret")', BOOTSTRAP.replace('30000', '45000'),
                       BOOTSTRAP.replace('1000000', 'dangerous()')]:
            with self.subTest(script=script), self.assertRaises(self.m.OnlineDetectionError):
                self.start(bootstrap=script)

    def test_callback_mismatch_and_code_are_not_executed(self):
        for text in ['wrong("{status:true}")', 'cb({status:true}); dangerous()',
                     'cb("{status:true,status:false}")', 'cb("{status:true}");evil()']:
            with self.subTest(text=text), self.assertRaises(self.m.OnlineDetectionError):
                self.m.decode_monitor_response(text, 'cb')

    def test_har_never_is_a_runtime_input(self):
        source = (ROOT / 'chaoxing/api/online.py').read_text(encoding='utf-8')
        self.assertNotIn('mooc1.chaoxing.com.har', source)

    def test_course_page_requires_matching_context_and_server_signature(self):
        html = ''.join('<input id="%s" value="%s">' % (k, v)
                       for k, v in [('courseId', '11'), ('clazzId', '22'), ('cpi', '33'), ('enc', 'synthetic')])
        self.assertEqual(self.m.course_entry_signature(html, COURSE), 'synthetic')
        for invalid in [html.replace('value="22"', 'value="99"'), html.replace('id="enc"', 'id="missing"')]:
            with self.assertRaises(self.m.OnlineDetectionError):
                self.m.course_entry_signature(invalid, COURSE)


if __name__ == '__main__':
    unittest.main()
