"""Official login-online checks, distinct from video progress and proof of viewing.

Only parse the observed, limited bootstrap grammar. Never execute downloaded JS,
reuse a captured signature, invent configuration, or override server rejection.
"""
import ast
import json
import re
import secrets
import time
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup


class OnlineDetectionError(RuntimeError):
    pass


def _fail():
    raise OnlineDetectionError('超星在线检测失败或协议无法确认，已停止；请在官方页面检查登录、验证码或限制。') from None


def _input(html, name):
    nodes = BeautifulSoup(html, 'html.parser').find_all('input', id=name)
    if len(nodes) != 1:
        _fail()
    return str(nodes[0].get('value', ''))


def course_entry_signature(html, course):
    for key in ('courseId', 'clazzId', 'cpi'):
        if not course.get(key) or _input(html, key) != str(course[key]):
            _fail()
    enc = _input(html, 'enc')
    if not enc or len(enc) > 512 or not re.fullmatch(r'[A-Za-z0-9_-]+', enc):
        _fail()
    return enc


def _official_url(url, host, path):
    p = urlsplit(url)
    if (p.scheme != 'https' or p.netloc != host or p.path != path
            or p.username or p.password or p.fragment):
        _fail()
    return url


def _study_url(url, course, chapter):
    _official_url(url, 'mooc1.chaoxing.com', '/mycourse/studentstudy')
    q = parse_qs(urlsplit(url).query)
    expected = {'courseId': str(course['courseId']), 'clazzid': str(course['clazzId']),
                'cpi': str(course['cpi']), 'chapterId': str(chapter)}
    if any(q.get(k) != [v] for k, v in expected.items()) or not q.get('enc'):
        _fail()


_STRING = r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')'''


def _literal(text):
    # ast.literal_eval parses literals only; it never executes JS or Python calls.
    try:
        if text.startswith('"'):
            return json.loads(text)
        return ast.literal_eval(text)
    except (ValueError, SyntaxError):
        _fail()


def _bootstrap_config(script, course, chapter, school):
    if len(script) > 32768:
        _fail()
    fid = re.search(r'\bvar\s+fid\s*=\s*(\d+)\s*;', script)
    refer = re.search(r'\bvar\s+refer\s*=\s*encodeURIComponent\(\s*(' + _STRING + r')\s*\)', script)
    expression = re.search(r'\burl\s*:\s*(.*?)\s*,\s*success\s*:', script, re.S)
    intervals = re.findall(r'\bsetInterval\(\s*fn\s*,\s*(\d+)\s*\)', script)
    if (not fid or not refer or not expression or not intervals
            or set(intervals) != {'30000'} or fid.group(1) != str(school)):
        _fail()
    raw_refer = _literal(refer.group(1))
    # The actual bootstrap uses a generic online-presence refer, not a chapter URL.
    # Preserve that exact server-provided value; do not fabricate viewing context.
    if raw_refer != 'http://i.mooc.chaoxing.com':
        _study_url(raw_refer, course, chapter)
    values = {'window.location.protocol': 'https:', 'fid': fid.group(1),
              'refer': quote(raw_refer, safe="~()*!.'")}
    # Explicit concatenation of literals and the three observed variables only.
    token = re.compile(_STRING + r'|window\.location\.protocol|refer\b|fid\b|\d+')
    text = expression.group(1)
    parts, cursor = [], 0
    while cursor < len(text):
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        match = token.match(text, cursor)
        if not match:
            _fail()
        item = match.group()
        parts.append(str(_literal(item)) if item[0] in '\"\'' else values.get(item, item))
        cursor = match.end()
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor == len(text):
            break
        if text[cursor] != '+':
            _fail()
        cursor += 1
        if not text[cursor:].strip():
            _fail()
    url = ''.join(parts)
    _official_url(url, 'detect.chaoxing.com', '/api/monitor')
    q = parse_qs(urlsplit(url).query)
    if (set(q) != {'version', 'refer', 'fid'} or any(len(v) != 1 for v in q.values())
            or q['fid'] != [str(school)] or q['refer'] != [raw_refer]
            or not re.fullmatch(r'\d+', q['version'][0])):
        _fail()
    return url


def _flat_object(text):
    """The observed JSONP contains a string holding a flat JS object, not JSON."""
    token = re.compile(_STRING + r'|[A-Za-z_$][\w$]*|-?\d+(?:\.\d+)?|[{}:,]')
    items, cursor = [], 0
    while cursor < len(text):
        if text[cursor].isspace():
            cursor += 1
            continue
        match = token.match(text, cursor)
        if not match:
            _fail()
        items.append(match.group())
        cursor = match.end()
    if not items or items[0] != '{' or items[-1] != '}':
        _fail()
    result, index = {}, 1
    while index < len(items) - 1:
        key = items[index]
        key = _literal(key) if key.startswith(('"', "'")) else key
        if (not re.fullmatch(r'[A-Za-z_$][\w$]*', key) or key in result
                or index + 2 >= len(items) or items[index + 1] != ':'):
            _fail()
        value = items[index + 2]
        if value.startswith(('"', "'")):
            value = _literal(value)
        elif value in ('true', 'false', 'null'):
            value = {'true': True, 'false': False, 'null': None}[value]
        elif re.fullmatch(r'-?\d+(?:\.\d+)?', value):
            value = float(value)
        else:
            _fail()
        result[key] = value
        index += 3
        if index < len(items) - 1:
            if items[index] != ',' or index + 1 == len(items) - 1:
                _fail()
            index += 1
    return result


def decode_monitor_response(text, callback):
    if len(text) > 16384:
        _fail()
    match = re.fullmatch(r'\s*' + re.escape(callback) + r'\s*\((.*)\)\s*;?\s*', text, re.S)
    if not match:
        _fail()
    inner = match.group(1).strip()
    if inner.startswith(('"', "'")):
        inner = _literal(inner)
    result = _flat_object(inner)
    if result.get('status') is not True:
        _fail()
    return result


class OnlineMonitor:
    interval = 30.0

    def __init__(self, session, url=None, *, clock=time):
        self.session, self.url, self.clock = session, url, clock
        self.enabled = url is not None
        self.verified_checks = 0
        self.last_check = None
        self.failed = False

    @staticmethod
    def _get(session, url, **kwargs):
        return session.get(url, allow_redirects=False, timeout=(4, 6), **kwargs)

    @classmethod
    def start(cls, session, course, chapter, enc, school, *, clock=time):
        try:
            if not chapter or not enc or not school:
                _fail()
            refer = 'https://mooc1.chaoxing.com/mycourse/studentstudy?' + urlencode({
                'chapterId': chapter, 'courseId': course['courseId'], 'clazzid': course['clazzId'],
                'cpi': course['cpi'], 'enc': enc, 'mooc2': 1, 'hidetype': 0, 'fanyaVersion': 0})
            transfer = 'https://mooc1.chaoxing.com/mycourse/transfer?' + urlencode({
                'moocId': course['courseId'], 'clazzid': course['clazzId'],
                'linkTime': int(clock.time() * 1000), 'ut': 's', 'refer': refer})
            response = cls._get(session, transfer)
            if response.status_code != 302:
                _fail()
            target = urljoin(transfer, response.headers.get('Location', ''))
            _study_url(target, course, chapter)
            response = cls._get(session, target)
            if response.status_code != 200:
                _fail()
            skip = _input(response.text, 'passSimulateValue')
            if skip == 'true':
                return cls(session, clock=clock)
            if skip != 'false':
                _fail()
            bootstrap = _input(response.text, 'detectUrl')
            # chapterDetect.js upgrades its http bootstrap to the HTTPS page's
            # protocol before loading it. This is present in the actual trace.
            if bootstrap.startswith('http://'):
                bootstrap = 'https://' + bootstrap[len('http://'):]
            _official_url(bootstrap, 'detect.chaoxing.com', '/api/passport2-onlineinfo.js')
            q = parse_qs(urlsplit(bootstrap).query)
            if not {'fid', 'key', 'refer'}.issubset(q) or q.get('fid') != [str(school)]:
                _fail()
            # Native chapterDetect appends _v to the official, signed bootstrap URL.
            response = cls._get(session, bootstrap, params={
                '_v': str(int(clock.time() * 1000)) + '_' + str(secrets.randbelow(100000))})
            if response.status_code != 200:
                _fail()
            # The captured official bootstrap itself returns without UID. Respect
            # that login prerequisite instead of issuing anonymous presence checks.
            guard = re.search(r'document\.cookie\.indexOf\(\s*(' + _STRING +
                              r')\s*\)\s*<\s*0', response.text)
            if not guard or _literal(guard.group(1)) != 'UID':
                _fail()
            if not any(cookie.name == 'UID' and cookie.value and cookie.domain in
                       ('', '.chaoxing.com', 'chaoxing.com', 'detect.chaoxing.com')
                       for cookie in session.cookies):
                _fail()
            monitor = cls(session, _bootstrap_config(response.text, course, chapter, school), clock=clock)
            monitor.check()
            return monitor
        except Exception:
            _fail()

    def check(self):
        if self.failed:
            _fail()
        if not self.enabled:
            return
        started = self.clock.monotonic()
        if self.last_check is not None and started - self.last_check < self.interval:
            return
        try:
            if self.last_check is not None and started - self.last_check >= self.interval * 2:
                _fail()  # A missed cycle is not silently backfilled as a successful check.
            callback = 'jsonp' + str(secrets.randbelow(10 ** 18))
            response = self._get(self.session, self.url, params={
                'jsoncallback': callback, 't': int(self.clock.time() * 1000)})
            if response.status_code != 200 or self.clock.monotonic() - started >= self.interval:
                _fail()
            decode_monitor_response(response.text, callback)
            self.last_check = started
            self.verified_checks += 1
        except Exception:
            self.failed = True
            _fail()
