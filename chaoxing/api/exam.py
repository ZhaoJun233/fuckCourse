"""Independent, unmonitored mobile exams; never invoked by chapter workers.

The signature protocol is adapted from yatori-dev/yatori-go-core (MIT).
Copyright (c) 2023 ChangBaiQi
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
import json
import os
from pathlib import Path
import random
import re
import secrets
import time
import unicodedata
from urllib.parse import quote, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HOST = "https://mooc1-api.chaoxing.com"
LIST_PATH = "/exam-ans/exam/phone/task-list"
COVER_PATHS = {"/exam-ans/android/mtaskmsgspecial", "/exam-ans/exam/phone/task-exam"}
START_PATH = "/exam-ans/exam/phone/start"
QUESTION_PATH = "/exam-ans/exam/test/reVersionTestStartNew"
SHEET_PATH = "/exam-ans/exam/phone/loadAnswerStatic"
SUBMIT_PATH = "/exam-ans/exam/test/reVersionSubmitTestNew"
TYPES = {"0": "single", "1": "multiple", "2": "completion", "3": "judgement",
         "4": "shortanswer", "6": "shortanswer"}


class ExamError(RuntimeError):
    """A user-facing error without signed URLs, cookies or API credentials."""


def checked_url(raw, paths):
    url = urljoin(HOST, raw)
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.hostname != "mooc1-api.chaoxing.com"
            or parsed.port not in (None, 443) or parsed.username or parsed.password
            or parsed.path not in paths):
        raise ExamError("考试入口发生变化或需要人工操作，请在学习通内检查；未继续请求。")
    return url


def parse_exam_list(html):
    soup = BeautifulSoup(html, "lxml")
    nav = soup.select_one("ul.nav")
    if nav is None:
        raise ExamError("未识别考试列表（可能登录失效或接口变化），不能判断为没有考试。")
    exams = []
    for row in nav.select("li[data]"):
        name = row.select_one("p")
        spans = row.select("span")
        if not name or not spans:
            raise ExamError("考试列表结构变化，请在学习通内检查。")
        exams.append({"name": name.get_text(" ", strip=True),
                      "status": spans[0].get_text(" ", strip=True),
                      "remaining": spans[1].get_text(" ", strip=True) if len(spans) > 1 else "",
                      "url": checked_url(row["data"], COVER_PATHS),
                      "stopped": row.get("data-stopexam", "0") != "0"})
    return exams


def hidden_fields(soup):
    return {field.get("name") or field.get("id"): field.get("value", "")
            for field in soup.select("input[type=hidden]")
            if field.get("name") or field.get("id")}


def guard_page(soup, html):
    text = soup.get_text(" ", strip=True)
    if any(word in text for word in ("添加签名", "考生签名", "诚信考试承诺",
                                    "请使用指定的IP", "只允许在电脑考试客户端",
                                    "章节任务点未完成", "考试尚未开始")):
        raise ExamError("考试要求承诺签名、指定环境或前置条件，请本人在官方客户端完成；未绕过。")
    fields = hidden_fields(soup)
    for key in ("faceRecognitionCompare", "faceDetection", "needFace", "monitorStatus",
                "monitorforcesubmit", "captchaCaptchaId", "needCaptcha"):
        value = fields.get(key, "").strip().lower()
        if value not in {"", "0", "false", "-1"}:
            raise ExamError("考试需要人脸、验证码或监考验证，本程序不支持，请使用官方客户端。")
    # A monitor nonce can be present on unmonitored papers too. Only accept it
    # when the page explicitly reports inactive monitoring; never force it off.
    if fields.get("monitorEnc") and fields.get("monitorStatus", "").lower() not in {"0", "false", "-1"}:
        raise ExamError("无法确认考试的监考状态，请使用官方客户端。")
    for name in ("needface", "needFace", "monitorStatus", "needCaptcha"):
        if re.search(r"\b" + name + r"\s*=\s*['\"]?(?:true|[1-9]\d*)\b", html):
            raise ExamError("考试要求人工验证或监考，请使用官方客户端。")
    error = soup.select_one("p.blankTips, li.msg, h2.textCenter")
    if error:
        raise ExamError("平台未允许继续考试，请在官方客户端查看提示。")


def parse_paper(html, url):
    soup = BeautifulSoup(html, "lxml")
    guard_page(soup, html)
    form = soup.select_one("form#submitTest")
    if not form:
        raise ExamError("没有识别到独立考试答题表单，请使用官方客户端检查。")
    action = checked_url(urljoin(url, form.get("action", "")), {SUBMIT_PATH})
    fields = {}
    for field in form.select("input[name], textarea[name]"):
        if field.has_attr("disabled") or field.get("type") in {"submit", "button", "file"}:
            continue
        if field.get("type") in {"radio", "checkbox"} and not field.has_attr("checked"):
            continue
        name = field["name"]
        value = field.get_text() if field.name == "textarea" else field.get("value", "")
        if name in fields and fields[name] != value:
            raise ExamError("考试表单包含冲突字段，未保存答案。")
        fields[name] = value
    required = {"courseId", "classId", "testPaperId", "testUserRelationId", "userId",
                "enc", "remainTime", "encRemainTime", "encLastUpdateTime", "start"}
    if any(not fields.get(key) for key in required):
        raise ExamError("考试表单缺少必需参数，未保存答案。")
    try:
        if int(fields["remainTime"]) <= 0 or fields.get("timeOver", "false").lower() == "true":
            raise ExamError("考试时间已用完，请在官方客户端确认状态。")
        int(fields["start"])
    except ValueError:
        raise ExamError("考试计时参数格式变化，已停止。") from None
    nodes = form.select("div.questionWrap")
    if len(nodes) != 1:
        raise ExamError("当前只支持独立考试的单题页面，未保存答案。")
    node = nodes[0]
    qid = fields.get("questionId", "")
    if not qid or node.get("data", qid) != qid:
        raise ExamError("题目编号不一致，未保存答案。")
    code = fields.get("type" + qid)
    title = node.select_one(".tit")
    if not title:
        raise ExamError("题干结构变化，未保存答案。")
    for el in title.select("h3, script, style"):
        el.decompose()
    for el in title.find_all("span", recursive=False):
        if re.fullmatch(r"\s*[（(][\d.]+\s*分[）)]\s*", el.get_text()):
            el.decompose()
    title_text = re.sub(r"^\s*\d+[.、．]\s*", "", title.get_text(" ", strip=True))
    choices = {}
    for option in node.select(".radioList[name]"):
        label = option["name"]
        content = option.select_one("cc")
        if not content or label in choices or not re.fullmatch(r"[A-Z]", label):
            raise ExamError("选项结构变化或不唯一，未保存答案。")
        choices[label] = content.get_text(" ", strip=True)
    if soup.select_one("style#cxSecretStyle"):
        from api.font_decoder import FontDecoder
        decoder = FontDecoder(html)
        title_text = decoder.decode(title_text)
        choices = {key: decoder.decode(value) for key, value in choices.items()}
    content = title_text + "".join(choices.values())
    if not title_text or re.search(r"[\ue000-\uf8ff]", content) or node.select_one("img, audio, video, svg, math"):
        raise ExamError("题目含图片、公式或未解密文字，需人工答题，未猜测答案。")
    if code in {"0", "1"} and len(choices) < 2:
        raise ExamError("未完整识别选择题选项，未猜测答案。")
    blanks = [el["name"] for el in node.select("textarea[name]")]
    question = {"id": qid, "type": TYPES.get(code, "unsupported"), "code": code,
                "title": title_text, "choices": choices, "blanks": blanks,
                "options": "\n".join(f"{key} {value}" for key, value in choices.items())}
    return {"fields": fields, "question": question, "url": url, "action": action}


def normalized(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(text))).strip()


def answer_fields(question, answer):
    """Exact matching only: ambiguous/missing answers are never randomized."""
    if not isinstance(answer, str) or not answer.strip():
        return None
    qid, kind = question["id"], question["type"]
    parts = [part.strip() for part in answer.splitlines() if part.strip()]
    if kind in {"single", "multiple"}:
        letters = []
        for part in parts:
            matches = [key for key, value in question["choices"].items() if normalized(part) == normalized(value)]
            if not matches and part in question["choices"]:
                matches = [part]
            if len(matches) != 1:
                return None
            letters.append(matches[0])
        letters = sorted(set(letters))
        if not letters or (kind == "single" and len(letters) != 1):
            return None
        return {("answers" if kind == "multiple" else "answer") + qid: "".join(letters)}
    if kind == "judgement":
        value = normalized(answer).lower()
        if value in {"正确", "对", "是", "true", "√", "1"}:
            return {"answer" + qid: "true"}
        if value in {"错误", "错", "否", "false", "×", "0"}:
            return {"answer" + qid: "false"}
        return None
    if kind == "completion":
        blanks = question["blanks"]
        if not blanks or len(blanks) != len(parts) or len(set(blanks)) != len(blanks):
            return None
        return dict(zip(blanks, parts))
    if kind == "shortanswer":
        if len(question["blanks"]) != 1:
            return None
        return {question["blanks"][0]: answer.strip()}
    return None


def exam_signature(uid, qid):
    """Protocol signature (MIT reference documented in docs/chaoxing-exams.md)."""
    ts = str(int(time.time() * 1000))
    r1, r2 = secrets.randbelow(9), secrets.randbelow(9)
    seed = secrets.token_hex(16) + ts[4:] + str(r1) + str(r2) + str(qid)
    value = 0
    for char in seed:
        value = (value * 31 + ord(char)) & 0x7fffffff
    salt = f"{r1}{r2}{value % 10}"
    text = str(uid) + ("_" + str(qid) if qid else "") + "|" + salt
    digits = "".join(str(ord(char)) for char in text)
    step = len(digits) // 5
    multiplier = int("".join(digits[step * i] for i in range(1, 5)))
    increment = len(text) // 2 + 1
    state = (multiplier * int(digits[:10]) + increment) % 0x7fffffff
    position = f"({random.randint(100, 1000)}|{random.randint(100, 1000)})"
    encrypted = ""
    for char in position:
        encrypted += f"{ord(char) ^ (state * 255 // 0x7fffffff):02x}"
        state = (multiplier * state + increment) % 0x7fffffff
    return {"pos": encrypted + secrets.token_hex(4), "rd": random.random(),
            "value": quote(position, safe=""), "_edt": ts + salt}


class ExamClient:
    def __init__(self, session):
        self.session = session
        self.paper = None
        self.course = None
        self.started = False

    def request(self, method, url, **kwargs):
        try:
            response = self.session.request(method, url, timeout=(5, 20), allow_redirects=False, **kwargs)
            response.raise_for_status()
        except requests.RequestException:
            raise ExamError("考试请求超时或网络异常；未自动重试。若涉及开始/暂存/交卷，结果可能未知，请在官方客户端核对。") from None
        return response

    def get_html(self, url, paths):
        for _ in range(5):
            url = checked_url(url, paths)
            response = self.request("GET", url)
            if response.status_code in {301, 302, 303, 307, 308}:
                url = urljoin(url, response.headers.get("Location", ""))
                continue
            if response.status_code != 200:
                raise ExamError("考试页面返回异常状态，请在官方客户端检查。")
            return response.text, url
        raise ExamError("考试页面重定向过多，已停止。")

    def list_exams(self, course):
        url = HOST + LIST_PATH
        params = {"courseId": course["courseId"], "classId": course["clazzId"], "cpi": course["cpi"]}
        # The official list endpoint may redirect between its mobile aliases.
        # Follow only the same official endpoint; never hand the session an
        # arbitrary Location header or silently broaden the allowed host/path.
        for _ in range(3):
            response = self.request("GET", url, params=params)
            if response.status_code in {301, 302, 303, 307, 308}:
                url = checked_url(urljoin(url, response.headers.get("Location", "")), {LIST_PATH})
                params = None
                continue
            if response.status_code != 200:
                raise ExamError("考试列表无法读取，请重新登录或在官方客户端检查。")
            return [dict(exam, course=course) for exam in parse_exam_list(response.text)]
        raise ExamError("考试列表重定向过多，已停止；请在官方客户端检查。")

    def prepare(self, exam):
        if exam["status"] not in {"待做", "未做", "未完成", "进行中"} or exam.get("stopped"):
            raise ExamError("所选考试当前不可作答，未进入考试。")
        html, url = self.get_html(exam["url"], COVER_PATHS)
        soup = BeautifulSoup(html, "lxml")
        guard_page(soup, html)
        jump = soup.select_one("input#examJumpUrl")
        if jump and jump.get("value"):
            html, url = self.get_html(jump["value"], {"/exam-ans/exam/phone/task-exam"})
            soup = BeautifulSoup(html, "lxml")
            guard_page(soup, html)
        fields = hidden_fields(soup)
        if any(not fields.get(key) for key in ("testPaperId", "testUserRelationId", "cpi")):
            raise ExamError("考试封面缺少参数，可能有承诺书或客户端限制；未启动考试。")
        course = exam["course"]
        for key, expected in (("courseId", course["courseId"]), ("classId", course["clazzId"]), ("cpi", course["cpi"])):
            if fields.get(key, str(expected)) != str(expected):
                raise ExamError("考试封面与所选课程不一致，未启动考试。")
        return {"fields": fields, "exam": exam, "url": url,
                "need_code": bool(re.search(r"\bneedcode\s*=\s*['\"]?[1-9]\d*", html))}

    def start(self, meta, *, confirmed=False, code=""):
        if confirmed is not True:
            raise ExamError("未确认开始，未启动考试。")
        self.course = meta["exam"]["course"]
        fields = meta["fields"]
        response = self.request("GET", HOST + START_PATH, params={
            "courseId": self.course["courseId"], "classId": self.course["clazzId"],
            "examId": fields["testPaperId"], "examAnswerId": fields["testUserRelationId"],
            "cpi": fields["cpi"], "source": fields.get("source", "0"),
            "keyboardDisplayRequiresUserAction": "1", "imei": fields.get("imei", ""), "jt": "0", "code": code,
        })
        if response.status_code in {301, 302, 303}:
            html, url = self.get_html(response.headers.get("Location", ""), {QUESTION_PATH})
        elif response.status_code == 200:
            html, url = response.text, HOST + QUESTION_PATH
        else:
            raise ExamError("开始考试未得到有效响应，请在官方客户端核对；未重试。")
        self.accept_paper(html, url, fields)
        self.started = True
        return self.paper

    def accept_paper(self, html, url, expected=None):
        paper = parse_paper(html, url)
        fields = paper["fields"]
        expected = expected or (self.paper["fields"] if self.paper else {})
        for key in ("testPaperId", "testUserRelationId", "userId"):
            if key in expected and fields[key] != expected[key]:
                raise ExamError("试卷身份发生变化，未保存或提交。")
        if fields["courseId"] != str(self.course["courseId"]) or fields["classId"] != str(self.course["clazzId"]):
            raise ExamError("试卷与所选课程不一致，未保存或提交。")
        self.paper = paper

    def common_params(self):
        if not self.started or self.paper is None:
            raise ExamError("考试尚未启动。")
        fields = self.paper["fields"]
        return {"courseId": fields["courseId"], "classId": fields["classId"], "cpi": self.course["cpi"],
                "source": fields.get("source", "0"), "imei": fields.get("imei", ""), "enc": fields["enc"],
                "remainTimeParam": fields["encRemainTime"], "relationAnswerLastUpdateTime": fields["encLastUpdateTime"]}

    def sheet(self):
        params = self.common_params()
        fields = self.paper["fields"]
        params.update(examRelationId=fields["testPaperId"], examRelationAnswerId=fields["testUserRelationId"], start="0")
        response = self.request("GET", HOST + SHEET_PATH, params=params)
        if response.status_code != 200:
            raise ExamError("无法读取答题卡，已停止自动答题。")
        soup = BeautifulSoup(response.text, "lxml")
        guard_page(soup, response.text)
        rows = soup.select("ul li[data]")
        result = {}
        for row in rows:
            try:
                index = int(row["data"])
            except ValueError:
                raise ExamError("答题卡题号格式变化，已停止自动答题。") from None
            if index < 0 or index in result:
                raise ExamError("答题卡题号不唯一，已停止自动答题。")
            result[index] = "complated" in row.get("class", [])
        if not result or len(result) > 500:
            raise ExamError("未识别完整答题卡，已停止自动答题。")
        return dict(sorted(result.items()))

    def fetch(self, index):
        params = self.common_params()
        fields = self.paper["fields"]
        params.update(tId=fields["testPaperId"], id=fields["testUserRelationId"], p="1", isphone="true", start=str(index))
        response = self.request("GET", HOST + QUESTION_PATH, params=params)
        if response.status_code != 200:
            raise ExamError("无法读取题目，已停止自动答题。")
        self.accept_paper(response.text, HOST + QUESTION_PATH)
        if int(self.paper["fields"]["start"]) != index:
            raise ExamError("题号与请求不一致，已停止自动答题。")
        return self.paper

    def save(self, answer):
        fields = answer_fields(self.paper["question"], answer)
        if fields is None:
            raise ExamError("答案未准确匹配，不暂存、不随机猜测。")
        return self.send(fields, final=False)

    def submit(self, *, confirmed=False):
        if confirmed is not True:
            raise ExamError("未确认交卷，未提交试卷。")
        return self.send({}, final=True)

    def send(self, answers, *, final):
        self.common_params()
        fields = dict(self.paper["fields"])
        qid = self.paper["question"]["id"]
        if final:
            # Use server-saved answers; do not overwrite a question during final submission.
            fields = {key: value for key, value in fields.items()
                      if key != "questionId" and not re.match(r"(?:answer|answers|answerEditor|blankNum|score|typeName|type)\d", key)}
            qid = ""
        fields.update(answers)
        fields["tempSave"] = "false" if final else "true"
        params = {"classId": fields["classId"], "courseId": fields["courseId"], "cpi": self.course["cpi"],
                  "testPaperId": fields["testPaperId"], "testUserRelationId": fields["testUserRelationId"],
                  "version": "1", "tempSave": fields["tempSave"], "qid": qid,
                  **exam_signature(fields["userId"], qid)}
        response = self.request("POST", self.paper["action"], params=params, data=fields,
                                headers={"Referer": self.paper["url"], "X-Requested-With": "XMLHttpRequest"})
        try:
            result = response.json()
        except ValueError:
            raise ExamError("暂存或交卷返回格式异常，结果未知；请在官方客户端核对，未重试。") from None
        if response.status_code != 200 or not isinstance(result, dict) or result.get("status") != "success":
            raise ExamError("平台未确认暂存或交卷成功，请在官方客户端查看原因；未重试。")
        if final:
            self.started = False
        else:
            tokens = result.get("data", "")
            if not isinstance(tokens, str) or len(tokens.split("|")) != 3:
                raise ExamError("平台已返回暂存成功，但计时令牌格式变化；请在官方客户端核对，已停止。")
            updated, remaining, enc = tokens.split("|")
            if not updated.isdigit() or not remaining.isdigit() or not enc:
                raise ExamError("平台暂存响应参数异常，已停止自动答题。")
            self.paper["fields"].update(encLastUpdateTime=updated, encRemainTime=remaining, enc=enc)
        return result
