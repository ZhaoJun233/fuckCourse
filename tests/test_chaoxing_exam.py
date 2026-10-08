"""Offline protocol/interaction regressions; fixtures contain synthetic IDs only."""
import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

from tests.source_units import ROOT, load_units


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


exam = load_module("offline_exam", "chaoxing/api/exam.py")
with patch.dict(sys.modules, {"api.exam": exam}):
    runner = load_module("offline_exam_runner", "chaoxing/api/exam_runner.py")

COURSE = {"title": "测试课程", "courseId": "100001", "clazzId": "200001", "cpi": "300001"}
EXAM = {"name": "测试期末", "status": "待做", "remaining": "剩余1天", "stopped": False,
        "course": COURSE, "url": exam.HOST + "/exam-ans/exam/phone/task-exam?taskrefId=400001"}
FIELDS = {"courseId": COURSE["courseId"], "classId": COURSE["clazzId"], "testPaperId": "400001",
          "testUserRelationId": "500001", "userId": "600001", "enc": "SYNTHETIC_ENC",
          "remainTime": "1200", "encRemainTime": "999", "encLastUpdateTime": "1760000000000",
          "start": "0", "tempSave": "true", "timeOver": "false", "monitorforcesubmit": "0",
          "isphone": "true", "enterPageTime": "1760000000000", "questionId": "700001"}


def paper_html(code="0", changes=None, question=None):
    fields = dict(FIELDS)
    fields.update(changes or {})
    fields.update({"type700001": code, "typeName700001": "测试题型", "answer700001": ""})
    inputs = "".join(f'<input type="hidden" name="{key}" id="{key}" value="{value}">' for key, value in fields.items())
    choices = '<div class="singleChoice radioList" name="B"><cc>第二项</cc></div><div class="singleChoice radioList" name="A"><cc>第一项</cc></div>'
    body = question or '<div class="tit"><h3>单选题</h3>1.<span>（5分）</span><p>测试题干</p></div>' + choices
    return f'<form id="submitTest" method="post" action="{exam.SUBMIT_PATH}">{inputs}<div class="questionWrap" data="700001">{body}</div></form>'


def response(html="", status=200, result=None, location=None):
    value = Mock(status_code=status, text=html, headers={"Location": location} if location else {})
    value.json.return_value = result
    return value


class ExamParsingTests(unittest.TestCase):
    def test_real_list_structure_and_empty_list(self):
        html = f'<ul class="nav"><li data="{EXAM["url"]}" data-stopexam="0"><div><p>测试期末</p><span>待做</span><span>剩余1天</span></div></li></ul>'
        self.assertEqual(exam.parse_exam_list(html)[0]["name"], "测试期末")
        self.assertEqual(exam.parse_exam_list('<ul class="nav"></ul>'), [])

    def test_login_or_unknown_page_is_not_empty_list(self):
        with self.assertRaises(exam.ExamError):
            exam.parse_exam_list('<html>请登录</html>')

    def test_disallowed_url_never_allowed(self):
        for url in ('https://evil.invalid' + exam.QUESTION_PATH, 'http://mooc1-api.chaoxing.com' + exam.QUESTION_PATH,
                    'https://user@mooc1-api.chaoxing.com' + exam.QUESTION_PATH, exam.HOST + exam.START_PATH,
                    'https://mooc1-api.chaoxing.com:1234' + exam.QUESTION_PATH):
            with self.subTest(url=url), self.assertRaises(exam.ExamError):
                exam.checked_url(url, {exam.QUESTION_PATH})

    def test_paper_preserves_shuffled_letter_mapping(self):
        value = exam.parse_paper(paper_html(), exam.HOST + exam.QUESTION_PATH)
        self.assertEqual(value['question']['title'], '测试题干')
        self.assertEqual(value['question']['options'], 'B 第二项\nA 第一项')
        self.assertEqual(exam.answer_fields(value['question'], '第一项'), {'answer700001': 'A'})

    def test_missing_form_or_required_fields_stops(self):
        for html in ('<html></html>', paper_html(changes={'enc': ''}), paper_html(changes={'remainTime': '0'})):
            with self.subTest(html=html[:30]), self.assertRaises(exam.ExamError):
                exam.parse_paper(html, exam.HOST + exam.QUESTION_PATH)

    def test_protected_pages_stop(self):
        for extra in ('诚信考试承诺', '添加签名', '<input type="hidden" id="faceRecognitionCompare" value="1">',
                      '<input type="hidden" id="captchaCaptchaId" value="SYNTHETIC">',
                      '<script>var monitorStatus=1;</script>', '<input type="hidden" id="monitorEnc" value="SYNTHETIC">'):
            with self.subTest(extra=extra), self.assertRaises(exam.ExamError):
                exam.parse_paper(paper_html() + extra, exam.HOST + exam.QUESTION_PATH)

    def test_monitor_nonce_with_explicit_inactive_status_is_not_active_monitoring(self):
        extra = '<input type="hidden" id="monitorEnc" value="SYNTHETIC_NONCE"><input type="hidden" id="monitorStatus" value="0">'
        value = exam.parse_paper(paper_html() + extra, exam.HOST + exam.QUESTION_PATH)
        self.assertEqual(value['question']['title'], '测试题干')

    def test_inline_spans_in_question_are_not_lost(self):
        html = paper_html().replace('<p>测试题干</p>', '<p>测试<span>重要</span>题干</p>')
        value = exam.parse_paper(html, exam.HOST + exam.QUESTION_PATH)
        self.assertIn('重要', value['question']['title'])

    def test_image_and_private_use_font_stops(self):
        for title in ('<p>测试<img src="x.png"></p>', '<p>\ue001</p>'):
            with self.assertRaises(exam.ExamError):
                exam.parse_paper(paper_html(question='<div class="tit">' + title + '</div>'), exam.HOST + exam.QUESTION_PATH)

    def test_conflicting_duplicate_fields_stops(self):
        html = paper_html().replace('</form>', '<input name="enc" value="OTHER"></form>')
        with self.assertRaises(exam.ExamError):
            exam.parse_paper(html, exam.HOST + exam.QUESTION_PATH)

    def test_answer_missing_ambiguous_or_unmatched_never_guessed(self):
        q = exam.parse_paper(paper_html(), exam.HOST + exam.QUESTION_PATH)['question']
        for answer in (None, '', '不存在的选项', 'A\nB', 42):
            self.assertIsNone(exam.answer_fields(q, answer))
        q['choices'] = {'A': '相同', 'B': '相同'}
        self.assertIsNone(exam.answer_fields(q, '相同'))

    def test_multiple_judgement_completion_and_shortanswer(self):
        q = exam.parse_paper(paper_html(code='1'), exam.HOST + exam.QUESTION_PATH)['question']
        self.assertEqual(exam.answer_fields(q, '第二项\n第一项'), {'answers700001': 'AB'})
        q.update(type='judgement')
        self.assertEqual(exam.answer_fields(q, '错误'), {'answer700001': 'false'})
        self.assertIsNone(exam.answer_fields(q, '可能正确'))
        q.update(type='completion', blanks=['answerEditor7000011', 'answerEditor7000012'])
        self.assertEqual(exam.answer_fields(q, '甲\n乙'), {'answerEditor7000011': '甲', 'answerEditor7000012': '乙'})
        self.assertIsNone(exam.answer_fields(q, '甲'))
        q.update(type='shortanswer', blanks=['answer700001'])
        self.assertEqual(exam.answer_fields(q, '参考说明'), {'answer700001': '参考说明'})
        q.update(type='unsupported')
        self.assertIsNone(exam.answer_fields(q, 'A'))


class ExamClientTests(unittest.TestCase):
    def setUp(self):
        self.block = patch('requests.Session.request', side_effect=AssertionError('Live requests forbidden'))
        self.block.start()
        self.addCleanup(self.block.stop)
        self.session = Mock()
        self.client = exam.ExamClient(self.session)
        self.client.course = COURSE
        self.client.paper = exam.parse_paper(paper_html(), exam.HOST + exam.QUESTION_PATH)
        self.client.started = True

    def test_list_is_get_only_and_no_start_or_submit(self):
        self.session.request.return_value = response('<ul class="nav"></ul>')
        self.assertEqual(self.client.list_exams(COURSE), [])
        call = self.session.request.call_args
        self.assertEqual(call.args, ('GET', exam.HOST + exam.LIST_PATH))
        self.assertEqual(call.kwargs['params']['classId'], COURSE['clazzId'])
        self.assertEqual(call.kwargs['timeout'], (5, 20))
        self.assertFalse(call.kwargs['allow_redirects'])

    def test_list_follows_only_official_list_redirect(self):
        html = '<ul class="nav"></ul>'
        self.session.request.side_effect = [
            response(status=302, location=exam.LIST_PATH),
            response(html),
        ]
        self.assertEqual(self.client.list_exams(COURSE), [])
        self.assertEqual(self.session.request.call_count, 2)
        self.assertIsNone(self.session.request.call_args_list[1].kwargs.get('params'))

    def test_bad_list_redirect_never_requested(self):
        self.session.request.return_value = response(status=302, location='https://evil.invalid/')
        with self.assertRaises(exam.ExamError):
            self.client.list_exams(COURSE)
        self.assertEqual(self.session.request.call_count, 1)

    def test_start_requires_exact_confirmation(self):
        with self.assertRaises(exam.ExamError):
            self.client.start({}, confirmed=False)
        self.session.request.assert_not_called()

    def test_submit_requires_exact_confirmation(self):
        with self.assertRaises(exam.ExamError):
            self.client.submit()
        self.session.request.assert_not_called()

    def test_prepare_blocks_commitment_before_start(self):
        self.session.request.return_value = response('<p>诚信考试承诺，添加签名</p>')
        with self.assertRaises(exam.ExamError):
            self.client.prepare(EXAM)
        self.assertEqual(self.session.request.call_count, 1)
        self.assertEqual(self.session.request.call_args.args[0], 'GET')

    def test_completed_or_stopped_exam_cannot_enter(self):
        for value in (dict(EXAM, status='已完成'), dict(EXAM, stopped=True)):
            with self.assertRaises(exam.ExamError):
                self.client.prepare(value)
        self.session.request.assert_not_called()

    def test_prepare_keeps_official_parameters_no_bypass_flags(self):
        cover = ''.join(f'<input type="hidden" id="{key}" value="{value}">' for key, value in
                        {'testPaperId': '400001', 'testUserRelationId': '500001', 'cpi': COURSE['cpi']}.items())
        self.session.request.return_value = response(cover)
        meta = self.client.prepare(EXAM)
        self.assertEqual(meta['fields']['testPaperId'], '400001')
        url = self.session.request.call_args.args[1]
        self.assertNotIn('examsignal', url)
        self.assertNotIn('redo=', url)

    def test_start_follows_only_official_question_redirect(self):
        meta = {'exam': EXAM, 'fields': {'testPaperId': '400001', 'testUserRelationId': '500001', 'cpi': COURSE['cpi']}}
        self.session.request.side_effect = [response(status=302, location=exam.QUESTION_PATH), response(paper_html())]
        self.client.start(meta, confirmed=True)
        self.assertTrue(self.client.started)
        self.assertEqual(self.session.request.call_count, 2)
        params = self.session.request.call_args_list[0].kwargs['params']
        self.assertNotIn('captchavalidate', params)
        self.assertNotIn('faceDetectionResult', params)

    def test_bad_start_redirect_never_requested(self):
        meta = {'exam': EXAM, 'fields': {'testPaperId': '400001', 'testUserRelationId': '500001', 'cpi': COURSE['cpi']}}
        self.session.request.return_value = response(status=302, location='https://evil.invalid/')
        with self.assertRaises(exam.ExamError):
            self.client.start(meta, confirmed=True)
        self.assertEqual(self.session.request.call_count, 1)

    def test_network_failure_is_single_attempt_and_redacted(self):
        self.session.request.side_effect = requests.Timeout('SYNTHETIC_SECRET_URL')
        with self.assertRaises(exam.ExamError) as caught:
            self.client.save('第一项')
        self.assertNotIn('SYNTHETIC_SECRET_URL', str(caught.exception))
        self.assertEqual(self.session.request.call_count, 1)

    def test_save_uses_temp_save_and_updates_server_tokens(self):
        self.session.request.return_value = response(result={'status': 'success', 'data': '1760000001234|998|NEXT_ENC'})
        self.client.save('第一项')
        call = self.session.request.call_args
        self.assertEqual(call.args, ('POST', exam.HOST + exam.SUBMIT_PATH))
        self.assertEqual(call.kwargs['data']['answer700001'], 'A')
        self.assertEqual(call.kwargs['data']['tempSave'], 'true')
        self.assertEqual(call.kwargs['params']['tempSave'], 'true')
        self.assertEqual(self.client.paper['fields']['enc'], 'NEXT_ENC')
        self.assertEqual(call.kwargs['data']['remainTime'], FIELDS['remainTime'])

    def test_final_uses_saved_answers_not_current_question(self):
        self.session.request.return_value = response(result={'status': 'success'})
        self.client.submit(confirmed=True)
        data = self.session.request.call_args.kwargs['data']
        self.assertEqual(data['tempSave'], 'false')
        self.assertNotIn('questionId', data)
        self.assertNotIn('answer700001', data)
        self.assertFalse(self.client.started)

    def test_failure_or_invalid_response_never_reported_success(self):
        for result in ({'status': 'fail', 'msg': 'SYNTHETIC_SECRET'}, {'status': True}, {'status': 'success', 'data': ''}):
            self.session.request.return_value = response(result=result)
            with self.assertRaises(exam.ExamError) as caught:
                self.client.save('第一项')
            self.assertNotIn('SYNTHETIC_SECRET', str(caught.exception))

    def test_sheet_uses_server_indices_without_guessing(self):
        self.session.request.return_value = response('<ul><h4 class="cardTit">一.单选题</h4><li data="1"></li><li data="9" class="complated"></li></ul>')
        self.assertEqual(self.client.sheet(), {1: False, 9: True})

    def test_invalid_sheet_and_mismatched_identity_stop(self):
        for html in ('<html>登录</html>', '<ul><li data="1"></li><li data="1"></li></ul>'):
            self.session.request.return_value = response(html)
            with self.assertRaises(exam.ExamError):
                self.client.sheet()
        with self.assertRaises(exam.ExamError):
            self.client.accept_paper(paper_html(changes={'testPaperId': 'OTHER'}), exam.HOST + exam.QUESTION_PATH)

    def test_fetch_has_no_forced_monitor_or_time_override(self):
        self.session.request.return_value = response(paper_html(changes={'start': '9'}))
        self.client.fetch(9)
        params = self.session.request.call_args.kwargs['params']
        self.assertEqual(params['start'], '9')
        self.assertEqual(params['remainTimeParam'], '999')
        self.assertNotIn('monitorStatus', params)
        self.assertNotIn('monitorOp', params)

    def test_signature_wire_format_is_double_encoded_value(self):
        signature = exam.exam_signature('600001', '700001')
        prepared = requests.Request('POST', exam.HOST + exam.SUBMIT_PATH, params=signature).prepare()
        self.assertIn('value=%2528', prepared.url)
        self.assertRegex(signature['pos'], r'^[0-9a-f]+$')
        self.assertRegex(signature['_edt'], r'^\d{16}$')


class ExamWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.client.prepare.return_value = {'need_code': False}
        self.client.sheet.side_effect = [{0: False}, {0: True}]
        self.client.fetch.return_value = exam.parse_paper(paper_html(), exam.HOST + exam.QUESTION_PATH)
        self.tiku = Mock(DISABLE=False)
        self.tiku.query.return_value = '第一项'
        self.emit = Mock()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def run_session(self, replies):
        runner.run_exam_session(self.client, EXAM, self.tiku, ask=Mock(side_effect=replies),
                                emit=self.emit, review_dir=self.temp.name)

    def test_default_cancel_does_not_start(self):
        self.run_session([''])
        self.client.start.assert_not_called()
        self.tiku.query.assert_not_called()

    def test_save_by_default_never_submits_even_with_tiku_submit_true(self):
        self.tiku.SUBMIT = True
        self.run_session(['开始考试', ''])
        self.client.save.assert_called_once_with('第一项')
        self.client.submit.assert_not_called()
        text = next(Path(self.temp.name).glob('*.json')).read_text(encoding='utf-8')
        self.assertIn('平台确认已暂存', text)
        for private in ('SYNTHETIC_ENC', '600001', '500001', '700001', 'https://'):
            self.assertNotIn(private, text)

    def test_submit_requires_separate_confirmation_and_complete_sheet(self):
        self.run_session(['开始考试', '提交考试'])
        self.client.submit.assert_called_once_with(confirmed=True)
        self.assertEqual(self.client.fetch.call_count, 2)

    def test_missing_answer_never_guesses_or_submits(self):
        self.tiku.query.return_value = None
        self.run_session(['开始考试'])
        self.client.save.assert_not_called()
        self.client.submit.assert_not_called()

    def test_query_exception_is_redacted_and_skipped(self):
        self.tiku.query.side_effect = RuntimeError('SYNTHETIC_API_KEY')
        self.run_session(['开始考试'])
        self.client.save.assert_not_called()
        self.assertNotIn('SYNTHETIC_API_KEY', str(self.emit.call_args_list))

    def test_already_saved_answer_not_overwritten(self):
        self.client.sheet.side_effect = [{0: True}]
        self.run_session(['开始考试', ''])
        self.tiku.query.assert_not_called()
        self.client.save.assert_not_called()

    def test_incomplete_server_sheet_prevents_final(self):
        self.client.sheet.side_effect = [{0: False}, {0: False}]
        with self.assertRaises(exam.ExamError):
            self.run_session(['开始考试', '提交考试'])
        self.client.submit.assert_not_called()

    def test_disabled_provider_and_empty_code_do_not_start(self):
        self.tiku.DISABLE = True
        with self.assertRaises(exam.ExamError):
            self.run_session([])
        self.tiku.DISABLE = False
        self.client.prepare.return_value = {'need_code': True}
        with self.assertRaises(exam.ExamError):
            self.run_session(['开始考试', ''])
        self.client.start.assert_not_called()

    def test_read_only_mode_has_no_prepare_or_start_calls(self):
        cx = Mock(tiku=self.tiku)
        cx.get_course_list.return_value = [COURSE]
        self.client.list_exams.return_value = [EXAM]
        ask = Mock(side_effect=AssertionError('Read-only mode must not ask to start'))
        runner.run_exam_mode(cx, list_only=True, client=self.client, ask=ask, emit=self.emit)
        self.client.prepare.assert_not_called()
        self.client.start.assert_not_called()

    def test_list_failure_is_visible_not_silent(self):
        cx = Mock(tiku=self.tiku)
        cx.get_course_list.return_value = [COURSE]
        self.client.list_exams.side_effect = exam.ExamError('无法读取列表')
        runner.run_exam_mode(cx, list_only=True, client=self.client, emit=self.emit)
        self.assertIn('无法读取列表', str(self.emit.call_args_list))

    def test_platform_entry_routes_exam_not_chapter_workers(self):
        notification = Mock()
        notification.get_notification_from_config.return_value = notification
        cx = Mock()
        env = {'init_config': lambda: ({'exam_mode': True, 'exam_list_only': True}, {}, {}),
               'init_chaoxing': lambda *args: cx, 'Notification': lambda: notification,
               'login_with_retry': Mock(), 'run_exam_mode': Mock(), 'logger': Mock(),
               'filter_courses': Mock(), 'sys': sys, 'traceback': traceback}
        main = load_units('chaoxing/main.py', ['main'], env)['main']
        main()
        env['login_with_retry'].assert_called_once()
        env['run_exam_mode'].assert_called_once_with(cx, None, list_only=True)
        env['filter_courses'].assert_not_called()
        cx.get_course_list.assert_not_called()

    def test_launcher_and_cli_independent_mode(self):
        env = {'_run': Mock(), 'CHAOXING_DIR': 'synthetic-dir'}
        launch = load_units('main.py', ['run_chaoxing_exam'], env)['run_chaoxing_exam']
        launch()
        env['_run'].assert_called_once_with('synthetic-dir', ['main.py', '--exam'], 'chaoxing-exam')
        parse = load_units('chaoxing/main.py', ['parse_args'], {'argparse': argparse, 'sys': sys})['parse_args']
        with patch.object(sys, 'argv', ['main.py', '--exam-list']):
            self.assertTrue(parse().exam_list)


if __name__ == '__main__':
    unittest.main()
