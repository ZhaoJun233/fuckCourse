# -*- coding: utf-8 -*-
import functools
import json
import math
import os
import random
import re
import sys
import threading
import time
from difflib import SequenceMatcher
from enum import Enum, IntEnum
from hashlib import md5
from getpass import getpass
from typing import Optional, Literal, Any
try:
    from typing import Self
except ImportError:
    Self = Any

import requests
from loguru import logger
from requests import RequestException
from requests.adapters import HTTPAdapter
from api.answer import *
from api.answer_check import cut
from api.cipher import AESCipher
from api.config import GlobalConst as gc
from api.cookies import save_cookies, use_cookies
from api.decode import (
    decode_course_list,
    decode_course_point,
    decode_course_card,
    decode_media_completion,
    decode_course_folder,
    decode_questions_info,
)
from api.exceptions import MaxRetryExceeded

_progress_lock = threading.Lock()
_progress_bars = {}  # key -> bar_text, insertion-ordered on CPython 3.7+


def _display_width(s):
    """Return terminal display columns for a string (CJK chars = 2, ASCII = 1)."""
    w = 0
    for c in s:
        w += 2 if ord(c) > 127 else 1
    return w


def _term_width():
    try:
        return os.get_terminal_size().columns - 4
    except Exception:
        return 76


def _redraw_bars():
    """Redraw all active progress bars in-place. Caller must hold _progress_lock."""
    n = len(_progress_bars)
    if n == 0:
        return
    lines = "\n".join(f"\r\033[K{t}" for t in _progress_bars.values())
    sys.stdout.write(f"\033[s\033[{n}F{lines}\033[u")
    sys.stdout.flush()


def _draw_progress_bar(iteration, total, prefix="", suffix=""):
    if total <= 0:
        return
    width = _term_width()
    percent = f"{100 * iteration / total:.1f}"

    def _fmt(sec):
        s = int(sec) % 60
        m = (int(sec) % 3600) // 60
        h = int(sec) // 3600
        if h > 0:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    time_info = f"{_fmt(iteration)}/{_fmt(total)}"
    full_suffix = f"| {percent}% {suffix} {time_info}"
    suffix_dw = _display_width(full_suffix)

    key = prefix
    display_prefix = prefix
    full_prefix = f"{display_prefix} |"
    while _display_width(full_prefix) + suffix_dw + 3 > width and len(display_prefix) > 0:
        display_prefix = display_prefix[:-1]
        full_prefix = f"{display_prefix} |"

    bar_len = width - _display_width(full_prefix) - suffix_dw
    if bar_len < 3:
        bar_len = 3
    filled = int(bar_len * iteration // total)
    bar = "#" * filled + " " * (bar_len - filled)
    bar_text = f"{full_prefix}{bar}{full_suffix}"

    with _progress_lock:
        is_new = key not in _progress_bars
        _progress_bars[key] = bar_text
        if is_new:
            print()  # Reserve a new terminal line for this bar
        _redraw_bars()


def _wipe_bar(key):
    """Remove a completed progress bar and redraw remaining ones."""
    with _progress_lock:
        if key not in _progress_bars:
            return
        del _progress_bars[key]
        n = len(_progress_bars)
        if n > 0:
            lines = "\n".join(f"\r\033[K{t}" for t in _progress_bars.values())
            sys.stdout.write(f"\033[s\033[{n+1}F{lines}\033[K\033[u")
        else:
            sys.stdout.write(f"\033[1F\r\033[K")
        sys.stdout.flush()


def get_timestamp():
    return str(int(time.time() * 1000))


def _save_credentials_to_config(username, password):
    config_path = os.environ.get("FUCKCOURSE_CONFIG", "")
    if not config_path:
        return
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            root = json.load(f)
    except FileNotFoundError:
        root = {}
    except json.JSONDecodeError:
        import tempfile
        with open(config_path, "rb") as source, tempfile.NamedTemporaryFile(
            mode="wb", prefix=os.path.basename(config_path) + ".corrupt.",
            suffix=".bak", dir=os.path.dirname(os.path.abspath(config_path)),
            delete=False,
        ) as backup:
            backup.write(source.read())
            bak = backup.name
        logger.warning(f"配置文件 {config_path} 格式损坏，已备份至 {bak}")
        root = {}
    if not isinstance(root, dict):
        raise ValueError("Configuration root must be a JSON object")
    section = root.get("chaoxing", {})
    if "common" not in section:
        section["common"] = {}
    section["common"]["username"] = username
    section["common"]["password"] = password
    root["chaoxing"] = section
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(root, f, indent=4, ensure_ascii=False)


class SessionManager:
    _instance = None
    _lock = threading.RLock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
            return cls._instance

    def __init__(self):
        with self._lock:
            if hasattr(self, "_session"):
                return
            session = requests.Session()
            session.mount("https://", HTTPAdapter(max_retries=2))
            session.mount("http://", HTTPAdapter(max_retries=2))
            session.request = functools.partial(session.request, timeout=5)
            session.headers.clear()
            session.headers.update(gc.HEADERS)
            session.cookies.update(use_cookies())
            self._session = session

    @classmethod
    def get_instance(cls) -> Self:
        return cls()

    @classmethod
    def get_session(cls) -> requests.Session:
        instance = cls.get_instance()
        return instance._session

    @classmethod
    def update_cookies(cls):
        cls.get_instance()._session.cookies.update(use_cookies())


class Account:
    username = None
    password = None
    last_login = None
    isSuccess = None

    def __init__(self, _username, _password):
        self.username = _username
        self.password = _password


class RateLimiter:
    def __init__(self, call_interval):
        self.last_call = time.time()
        self.lock = threading.Lock()
        self.call_interval = call_interval

    def limit_rate(self, random_time=False, random_min=0.0, random_max=1.0):
        with self.lock:
            now = time.time()
            base_wait = max(self.last_call + self.call_interval - now, 0)
            extra_wait = random.uniform(random_min, random_max) if random_time else 0
            call_wait = base_wait + extra_wait
            self.last_call = now + call_wait

        time.sleep(call_wait)


class StudyResult(Enum):
    SUCCESS = 0
    FORBIDDEN = 1  # 403
    ERROR = 2
    TIMEOUT = 3
    DEFERRED = 4  # Not yet available; keep pending without immediate retries.

    def is_success(self):
        return self == StudyResult.SUCCESS

    def is_failure(self):
        return self != StudyResult.SUCCESS


class SignType(IntEnum):
    NORMAL = 0
    GESTURE = 3
    LOCATION = 4


class ActivityStatus(IntEnum):
    ACTIVE = 1
    INACTIVE = 2


class ActivityType(IntEnum):
    SIGNIN = 2


class Chaoxing:
    def __init__(self, account: Account = None, tiku: Tiku = None, **kwargs):
        self.account = account
        self.cipher = AESCipher()
        self.tiku = tiku
        self.kwargs = kwargs
        self.rollback_times = 0
        self.rate_limiter = RateLimiter(0.5)  # 其他接口速率限制比较松
        self.video_log_limiter = RateLimiter(2)  # 上报进度极其容易卡验证码，限制2s一次

    def login(self, login_with_cookies=False):
        if login_with_cookies:
            logger.info("Logging in with cookies")
            SessionManager.update_cookies()
            logger.debug("Logged in with cookies (masked for privacy)")
            if not self._validate_cookie_session():
                logger.warning("Cookie 登录校验失败，尝试使用账号密码重新登录")
                if self.account and self.account.username and self.account.password:
                    return self.login(login_with_cookies=False)
                # cookies 失效且没有保存凭据，提示用户输入
                if self.account:
                    self.account.username = input("Cookies已过期，请输入手机号: ")
                    self.account.password = getpass("请输入密码: ")
                if self.account and self.account.username and self.account.password:
                    return self.login(login_with_cookies=False)
                return {"status": False, "msg": "cookies 已失效，请更新 cookies 或提供账号密码"}
            logger.info("登录成功...")
            return {"status": True, "msg": "登录成功"}

        _session = requests.Session()
        _url = "https://passport2.chaoxing.com/fanyalogin"
        _data = {
            "fid": "-1",
            "uname": self.cipher.encrypt(self.account.username),
            "password": self.cipher.encrypt(self.account.password),
            "refer": "https%3A%2F%2Fi.chaoxing.com",
            "t": True,
            "forbidotherlogin": 0,
            "validate": "",
            "doubleFactorLogin": 0,
            "independentId": 0,
        }
        logger.trace("正在尝试登录...")
        resp = _session.post(_url, headers=gc.HEADERS, data=_data, timeout=(5, 15))
        resp.raise_for_status()
        result = resp.json()
        if result.get("status") == True:
            save_cookies(_session)
            _save_credentials_to_config(self.account.username, self.account.password)
            SessionManager.update_cookies()
            logger.info("登录成功...")
            return {"status": True, "msg": "登录成功"}
        else:
            return {"status": False, "msg": str(result.get("msg2") or result.get("msg") or "登录失败")}

    def _validate_cookie_session(self) -> bool:
        session = SessionManager.get_instance()._session
        if not session.cookies.get("_uid"):
            return False

        test_session = requests.Session()
        test_session.headers.update(gc.HEADERS)
        test_session.cookies.update(session.cookies.get_dict())

        try:
            resp = test_session.post(
                "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata",
                data={"courseType": 1, "courseFolderId": 0, "query": "", "superstarClass": 0},
                timeout=8,
            )
        except RequestException as exc:
            logger.debug("Cookie validation request failed: {}", exc)
            return False

        if resp.status_code != 200:
            return False

        if "passport2.chaoxing.com" in resp.text or "login" in resp.text.lower():
            return False

        return True

    def get_fid(self):
        _session = SessionManager.get_session()
        return _session.cookies.get("fid", 1024)

    def get_uid(self):
        s = SessionManager.get_session()
        if "_uid" in s.cookies:
            return s.cookies["_uid"]
        if "UID" in s.cookies:
            return s.cookies["UID"]
        raise ValueError("Cannot get uid !")

    def get_course_list(self):
        _session = SessionManager.get_session()
        _url = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata"
        _data = {"courseType": 1, "courseFolderId": 0, "query": "", "superstarClass": 0}
        logger.trace("正在读取所有的课程列表...")

        # 接口突然抽风, 增加headers
        # 有可能只是referer的问题
        _headers = {
            "Referer": "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction?moocDomain=https://mooc1-1.chaoxing.com/mooc-ans",
        }
        _resp = _session.post(_url, headers=_headers, data=_data)
        # logger.trace(f"原始课程列表内容:\n{_resp.text}")
        logger.info("课程列表读取完毕...")
        course_list = decode_course_list(_resp.text)

        _interaction_url = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction"
        _interaction_resp = _session.get(_interaction_url)
        course_folder = decode_course_folder(_interaction_resp.text)
        for folder in course_folder:
            _data = {
                "courseType": 1,
                "courseFolderId": folder["id"],
                "query": "",
                "superstarClass": 0,
            }
            _resp = _session.post(_url, data=_data)
            course_list += decode_course_list(_resp.text)
        return course_list

    def get_activity_list(self, course: dict) -> list[dict]:
        s = SessionManager.get_session()
        url = "https://mobilelearn.chaoxing.com/v2/apis/active/student/activelist"
        params = {
            "fid": self.get_fid(),
            "courseId": course["courseId"],
            "classId": course["clazzId"],
            "showNotStartedActive": 0,
            "_": get_timestamp()
        }
        resp = s.get(url, params=params, allow_redirects=False)
        if resp.status_code != 200:
            logger.error("Failed to get activity list, return code: " + str(resp.status_code))
            logger.debug("Request url: " + resp.url)
            return []

        data = resp.json()
        if data["result"] != 1:
            logger.error("Unknown status: {} {}", data["result"], data["errorMsg"])
            logger.debug("Request url: " + resp.url)
            return []

        return data["data"]["activeList"]


    def pre_sign(self, course: dict, activity_id):
        s = SessionManager.get_session()
        params = {
            "general": 1,
            "sys": 1,
            "ls": 1,
            "appType": 15,
            "tid": '',
            "ut": 's',
            "uid": self.get_uid(),
            "activePrimaryId": activity_id,
            "courseId": course["courseId"],
            "classId": course["clazzId"],
        }
        resp = s.get('https://mobilelearn.chaoxing.com/newsign/preSign', params=params)
        resp_txt = resp.text
        logger.debug("Request url" + resp.url)
        if resp.status_code != 200:
            logger.error("Failed to get sign in, return code: " + str(resp.status_code) + "message: " + resp_txt)

        return resp_txt


    def sign_in_normal(self, course: dict, activity_id, name="", obj_id="aaa", lat=-1, lon=-1, type_=SignType.NORMAL):
        s = SessionManager.get_session()
        params = {
            "activeId": activity_id,
            "uid": self.get_uid(),
            "fid": self.get_fid(),
            "courseId": course["courseId"],
            "classId": course["clazzId"],
            "clientip": "",
            "objectId": obj_id,
            "name": name,
            "useragent": "",
            "latitude": lat,
            "longitude": lon,
            "appType": "15",
        }

        resp = s.get("https://mobilelearn.chaoxing.com/pptSign/stuSignajax", params=params)

        resp_txt = resp.text
        if resp.status_code != 200:
            logger.error("Failed to get sign in, return code: " + str(resp.status_code) + "message: " + resp_txt)

        if type_ != SignType.LOCATION:
            return resp_txt

        pattern = r"[^0-9\.]*(.+)米[^0-9\.]*"
        msg = re.match(pattern, resp_txt)
        logger.warning(f"距离签到位置 {msg}m")
        # TOD0: Implement triangulation for location signs
        return resp_txt


    def get_course_point(self, _courseid, _clazzid, _cpi):
        _session = SessionManager.get_session()
        _url = f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?courseid={_courseid}&clazzid={_clazzid}&cpi={_cpi}&ut=s"
        logger.trace("URL: " + _url)
        logger.trace("开始读取课程所有章节...")
        _resp = _session.get(_url)
        if _resp.status_code != 200:
            raise ValueError("无法读取平台章节进度，停止处理。")
        logger.info("课程章节读取成功...")
        return decode_course_point(_resp.text)

    def get_job_list(self, course: dict, point: dict) -> tuple[list[dict], dict]:
        _session = SessionManager.get_session()
        self.rate_limiter.limit_rate()
        job_list = []
        job_info = {}
        cards_params = {
            "clazzid": course["clazzId"],
            "courseid": course["courseId"],
            "knowledgeid": point["id"],
            "ut": "s",
            "cpi": course["cpi"],
            "v": "2025-0424-1038-3",
            "mooc2": 1
        }

        # 学习界面任务卡片数, 很少有3个的. 逐个 num 请求，连续两轮无新卡则早停，避免 5-6 次无效 HTTP 请求
        consecutive_empty = 0
        for _possible_num in "0123456":

            logger.trace("开始读取章节所有任务点...")

            cards_params.update({"num": _possible_num})
            _resp = _session.get("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards", params=cards_params)
            if _resp.status_code != 200:
                logger.error(f"未知错误: {_resp.status_code} 正在跳过")
                logger.error(_resp.text)
                return [], {}

            _job_list, _job_info = decode_course_card(_resp.text)
            for job in _job_list:
                job["cardnum"] = _possible_num
            if _job_info.get("notOpen", False):
                # 直接返回, 节省一次请求
                logger.info("该章节未开放")
                return [], _job_info

            job_list += _job_list
            job_info.update(_job_info)

            if _job_list:
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    break

        if not job_list:
            self.study_emptypage(course, point)

        logger.trace(f"原始任务点列表内容:\n{_resp.text}")
        logger.info("章节任务点读取成功...")

        return job_list, job_info

    def get_enc(self, clazzId, jobid, objectId, playingTime, duration, userid):
        return md5(
            f"[{clazzId}][{userid}][{jobid}][{objectId}][{playingTime * 1000}][d_yHJ!$pdA~5][{duration * 1000}][0_{duration}]"
            .encode()).hexdigest()

    def video_progress_log(
            self,
            _session,
            _course,
            _job,
            _job_info,
            _dtoken,
            _duration,
            _playingTime,
            _type: str = "Video",
            _isdrag: int = 3,
            headers: Optional[dict] = None,
    ) -> tuple[bool, int]:

        if headers is None:
            logger.warning("null headers")
            headers = gc.VIDEO_HEADERS

        self.video_log_limiter.limit_rate(random_time=True, random_max=2)

        if "courseId" in _job["otherinfo"]:
            logger.error(_job["otherinfo"])
            raise RuntimeError("this is not possible")

        enc = self.get_enc(_course["clazzId"], _job["jobid"], _job["objectid"], _playingTime, _duration, self.get_uid())
        params = {
            "clazzId": _course["clazzId"],
            "playingTime": _playingTime,
            "duration": _duration,
            "clipTime": f"0_{_duration}",
            "objectId": _job["objectid"],
            "otherInfo": _job["otherinfo"],
            "courseId": _course["courseId"],
            "jobid": _job["jobid"],
            "userid": self.get_uid(),
            "isdrag": _isdrag,
            "view": "pc",
            "enc": enc,
            "dtype": _type
        }

        _url = (
            f"https://mooc1.chaoxing.com/mooc-ans/multimedia/log/a/"
            f"{_course['cpi']}/"
            f"{_dtoken}"
        )

        face_capture_enc = _job["videoFaceCaptureEnc"]
        att_duration = _job["attDuration"]
        att_duration_enc = _job["attDurationEnc"]

        if face_capture_enc:
            params["videoFaceCaptureEnc"] = face_capture_enc
        if att_duration:
            params["attDuration"] = att_duration
        if att_duration_enc:
            params["attDurationEnc"] = att_duration_enc

        rt = _job['rt']
        if not rt:
            rt_search = re.search(r"-rt_([1d])", _job['otherinfo'])
            if rt_search:
                rt_char = rt_search.group(1)
                rt = "0.9" if rt_char == "d" else "1"
                logger.trace(f"Got rt from otherinfo: {rt}")

        if rt:
            logger.trace(f"Got rt: {rt}")
            _job['rt'] = rt
            params.update({"rt": rt,
                           "_t": get_timestamp()})
            resp = _session.get(_url, params=params, headers=headers)
        else:
            logger.warning("Failed to get rt")
            for rt in [0.9, 1]:
                params.update({"rt": rt,
                               "_t": get_timestamp()})
                resp = _session.get(_url, params=params, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if not isinstance(data, dict) or type(data.get("isPassed")) is not bool:
                        return False, 422
                    return data["isPassed"], 200
                # elif resp.ok:
                #    # TODO: 处理验证码
                #    pass
                elif resp.status_code == 403:
                    logger.warning("出现403报错, 正常尝试切换rt")

                else:
                    logger.warning("未知错误 jobid={}, status_code={}, 摘要:\n{}",
                                   _job.get("jobid"),
                                   resp.status_code,
                                   resp.text[:200])
                    break

        if resp.status_code == 200:
            data = resp.json()
            if not isinstance(data, dict) or type(data.get("isPassed")) is not bool:
                return False, 422
            return data["isPassed"], 200

        elif resp.status_code == 403:
            logger.debug(
                "视频进度上报返回403, jobid={}, 摘要={}",
                _job.get("jobid"),
                resp.text[:200],
            )

            # 若出现两个rt参数都返回403的情况, 则跳过当前任务
            logger.error("出现403报错, 尝试修复无效, 正在跳过当前任务点...")
            logger.error("请求url: {}", resp.url)
            logger.error("请求头: {}", dict(_session.headers) | headers)
            return False, 403

        logger.error(f"未知错误: {resp.status_code}")
        logger.error("请求url:", resp.url)
        logger.error("请求头：", dict(_session.headers) | headers)
        return False, resp.status_code

    def _refresh_video_status(self, session: requests.Session, job: dict, _type: Literal["Video", "Audio"]) \
            -> Optional[dict]:
        self.rate_limiter.limit_rate(random_time=True, random_max=0.2)
        headers = gc.VIDEO_HEADERS if _type == "Video" else gc.AUDIO_HEADERS
        info_url = (
            f"https://mooc1.chaoxing.com/ananas/status/{job['objectid']}?"
            f"k={self.get_fid()}&flag=normal"
        )
        try:
            resp = session.get(info_url, timeout=8, headers=headers)
        except RequestException as exc:
            logger.debug("刷新视频状态失败: {}", exc)
            return None

        if resp.status_code != 200:
            logger.debug("刷新视频状态返回码异常: {}" % resp.status_code)
            logger.debug(resp.text)
            return None

        try:
            data = resp.json()
        except ValueError as exc:
            logger.debug("解析视频状态响应失败: {}", exc)
            return None

        if data.get("status") == "success":
            return data

        return None

    def _recover_after_forbidden(self, session: requests.Session, job: dict, _type: Literal["Video", "Audio"]):
        SessionManager.update_cookies()
        refreshed = self._refresh_video_status(session, job, _type)
        if refreshed:
            return refreshed

        # FIXME: Temporarily disabled for multithreading support
        if False and self.account and self.account.username and self.account.password:
            login_result = self.login(login_with_cookies=False)
            if login_result.get("status"):
                SessionManager.update_cookies()
                return self._refresh_video_status(session, job, _type)
            logger.warning("账号密码登录失败: {}", login_result.get("msg"))

        return None

    def confirm_video_completion(self, course: dict, job: dict, job_info: dict) -> Optional[bool]:
        """Independent, read-only task-card check; never calls get_job_list."""
        knowledge_id = job_info.get("knowledgeid")
        if not knowledge_id:
            return None
        session = SessionManager.get_session()
        tabs = [str(job["cardnum"])] if "cardnum" in job else list("0123456")
        for tab in tabs:
            self.rate_limiter.limit_rate()
            try:
                response = session.get("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards", params={
                    "clazzid": course["clazzId"], "courseid": course["courseId"],
                    "knowledgeid": knowledge_id, "ut": "s", "cpi": course["cpi"],
                    "v": "2025-0424-1038-3", "mooc2": 1, "num": tab}, timeout=8)
            except RequestException:
                return None
            if response.status_code != 200:
                return None
            result = decode_media_completion(response.text, job)
            if result is not None:
                return result
        return None

    def study_video(self, _course, _job, _job_info, _speed: float = 1.0,
                    _type: Literal["Video", "Audio"] = "Video") -> StudyResult:
        # Conservative wall-clock timing is not proof of native player viewing.
        # No instant-end report, forged heartbeat, captcha bypass or auto replay.
        name = _job.get("name", "媒体任务")
        try:
            speed = float(_speed)
            if not math.isfinite(speed) or speed <= 0:
                return StudyResult.ERROR
            if speed != 1:
                logger.warning("保守模式固定按1倍真实时间处理媒体；忽略配置倍速。")
            session = SessionManager.get_session()
            headers = gc.VIDEO_HEADERS if _type == "Video" else gc.AUDIO_HEADERS
            info_url = f"https://mooc1.chaoxing.com/ananas/status/{_job['objectid']}?k={self.get_fid()}&flag=normal"
            metadata = session.get(info_url, headers=headers, timeout=8).json()
            if not isinstance(metadata, dict) or metadata.get("status") != "success":
                return StudyResult.ERROR
            token = metadata.get("dtoken")
            raw_duration = float(metadata.get("duration", 0))
            raw_bookmark = float(_job.get("playTime", 0))
            if (not token or not math.isfinite(raw_duration) or raw_duration <= 0
                    or not raw_duration.is_integer() or not math.isfinite(raw_bookmark)
                    or raw_bookmark < 0 or raw_bookmark / 1000 > raw_duration):
                logger.error("媒体时长或历史位置无效，未上报。")
                return StudyResult.ERROR
            duration = int(raw_duration)
            position = raw_bookmark / 1000
            if position >= duration:
                if self.confirm_video_completion(_course, _job, _job_info) is True:
                    return StudyResult.SUCCESS
                logger.warning("历史位置已到结尾但任务未确认完成，保留待办；请在官方播放器核查，不自动重播。")
                return StudyResult.DEFERRED
            try:
                interval = float(_job_info.get("reportTimeInterval", 60))
                if not math.isfinite(interval) or not 1 <= interval <= 300:
                    interval = 60
            except (ValueError, TypeError, OverflowError):
                interval = 60
            logger.info("开始任务: {}, 总时长: {}s, 历史播放位置: {}s（非累计观看证明）",
                        name, duration, int(position))
            deadline = time.monotonic() + (duration - position) + interval * 3 + 30
            end_reports = 0
            event = 3
            while True:
                passed, state = self.video_progress_log(
                    session, _course, _job, _job_info, token, duration, int(position),
                    _type, headers=headers, _isdrag=event)
                # Exclude request and limiter delay from the simulated position.
                last_tick = last_report = time.monotonic()
                if passed is True:
                    for attempt in range(3):
                        confirmed = self.confirm_video_completion(_course, _job, _job_info)
                        if confirmed is True:
                            logger.info("平台任务卡即时复核完成: {}（不保证长期保留）", name)
                            return StudyResult.SUCCESS
                        if attempt < 2:
                            time.sleep(2)
                    logger.warning("上报结果与独立任务卡不一致，保留待办，不自动重播: {}", name)
                    return StudyResult.DEFERRED
                if state != 200:
                    logger.warning("媒体上报失败（HTTP {}），停止，不切换音频重播: {}", state, name)
                    return StudyResult.FORBIDDEN if state == 403 else StudyResult.ERROR
                if position >= duration:
                    end_reports += 1
                    if end_reports >= 3:
                        logger.warning("结尾3次上报仍未确认完成，停止: {}", name)
                        return StudyResult.TIMEOUT
                while True:
                    now = time.monotonic()
                    if now >= deadline:
                        logger.warning("媒体任务达到总时间预算，未标记完成: {}", name)
                        return StudyResult.TIMEOUT
                    position = min(duration, position + max(0, now - last_tick))
                    last_tick = now
                    _draw_progress_bar(position, duration, prefix=name, suffix="本地计时/非平台完成")
                    # At the end keep native report cadence, never tight-loop full duration.
                    if now - last_report >= interval or (position >= duration and event != 4):
                        event = 4 if position >= duration else 0
                        break
                    time.sleep(min(1, interval - (now - last_report)))
        except (RequestException, ValueError, TypeError, OverflowError, KeyError):
            logger.error("媒体处理失败，未确认完成（请求或数据异常，敏感详情不输出）。")
            return StudyResult.ERROR
        finally:
            _wipe_bar(name)

    def study_document(self, _course, _job) -> StudyResult:
        """
        Study a document in Chaoxing platform.

        This method makes a GET request to fetch document information for a given course and job.

        Args:
            _course (dict): Dictionary containing course information with keys:
                - courseId: ID of the course
                - clazzId: ID of the class
            _job (dict): Dictionary containing job information with keys:
                - jobid: ID of the job
                - otherinfo: String containing node information
                - jtoken: Authentication token for the job

        Returns:
            requests.Response: Response object from the GET request

        Note:
            This method requires the following helper functions:
            - init_session(): To initialize a new session
            - get_timestamp(): To get current timestamp
            - re module for regular expression matching
        """
        _session = SessionManager.get_session()
        _url = f"https://mooc1.chaoxing.com/ananas/job/document?jobid={_job['jobid']}&knowledgeid={re.findall(r'nodeId_(.*?)-', _job['otherinfo'])[0]}&courseid={_course['courseId']}&clazzid={_course['clazzId']}&jtoken={_job['jtoken']}&_dc={get_timestamp()}"
        _resp = _session.get(_url)
        if _resp.status_code != 200:
            return StudyResult.ERROR
        else:
            return StudyResult.SUCCESS

    def study_work(self, _course, _job, _job_info) -> StudyResult:
        # FIXME: 这一块可以单独搞一个类出来了，方法里面又套方法，每一次调用都会创建新的方法，十分浪费
        if self.tiku.DISABLE or not self.tiku:
            return StudyResult.SUCCESS
        _ORIGIN_HTML_CONTENT = ""  # 用于配合输出网页源码, 帮助修复#391错误

        def random_answer(options: str) -> str:
            answer = ""
            if not options:
                return answer

            if q["type"] == "multiple":
                logger.debug(f"当前选项列表[cut前] -> {options}")
                _op_list = multi_cut(options)
                logger.debug(f"当前选项列表[cut后] -> {_op_list}")

                if not _op_list:
                    logger.error(
                        "选项为空, 未能正确提取题目选项信息! 请反馈并提供以上信息"
                    )
                    return answer

                available_options = len(_op_list)
                select_count = 0

                # 根据可用选项数量调整可能选择的选项数
                if available_options <= 1:
                    select_count = available_options
                else:
                    max_possible = min(4, available_options)
                    min_possible = min(2, available_options)

                    weights_map = {
                        2: [1.0],
                        3: [0.3, 0.7],
                        4: [0.1, 0.5, 0.4],
                        5: [0.1, 0.4, 0.3, 0.2],
                    }

                    weights = weights_map.get(max_possible, [0.3, 0.4, 0.3])
                    possible_counts = list(range(min_possible, max_possible + 1))

                    weights = weights[:len(possible_counts)]

                    weights_sum = sum(weights)
                    if weights_sum > 0:
                        weights = [w / weights_sum for w in weights]

                    select_count = random.choices(possible_counts, weights=weights, k=1)[0]

                selected_options = random.sample(_op_list, select_count) if select_count > 0 else []

                for option in selected_options:
                    answer += option[:1]  # 取首字为答案，例如A或B

                answer = "".join(sorted(answer))
            elif q["type"] == "single":
                answer = random.choice(options.split("\n"))[:1]  # 取首字为答案, 例如A或B
            # 判断题处理
            elif q["type"] == "judgement":
                # answer = self.tiku.jugement_select(_answer)
                answer = "true" if random.choice([True, False]) else "false"
            logger.info(f"随机选择 -> {answer}")
            return answer

        def multi_cut(answer: str):
            """
            将多选题答案字符串按特定字符进行切割, 并返回切割后的答案列表

            参数:
            answer(str): 多选题答案字符串.

            返回:
            list[str]: 切割后的答案列表,如果无法切割, 则返回默认的选项列表None

            注意:
            如果无法从网页中提取题目信息,将记录警告日志并返回None
            """
            # cut_char = [',','，','|','\n','\r','\t','#','*','-','_','+','@','~','/','\\','.','&',' ']    # 多选答案切割符
            # ',' 在常规被正确划分的, 选项中出现, 导致 multi_cut 无法正确划分选项 #391
            # IndexError: Cannot choose from an empty sequence #391
            # 同时为了避免没有考虑到的 case, 应该先按照 '\n' 匹配, 匹配不到再按照其他字符匹配
            cut_char = [
                "\n",
                ",",
                "，",
                "|",
                "\r",
                "\t",
                "#",
                "*",
                "-",
                "_",
                "+",
                "@",
                "~",
                "/",
                "\\",
                ".",
                "&",
                " ",
                "、",
            ]  # 多选答案切割符
            res = cut(answer)
            if res is None:
                logger.warning(
                    f"未能从网页中提取题目信息, 以下为相关信息：\n\t{answer}\n\n{_ORIGIN_HTML_CONTENT}\n"
                )  # 尝试输出网页内容和选项信息
                logger.warning("未能正确提取题目选项信息! 请反馈并提供以上信息")
                return None
            else:
                return res

        def clean_res(res):
            cleaned_res = []
            if isinstance(res, str):
                res = [res]
            for c in res:
                # 仅在字符串长度大于1时才尝试去除开头的字母编号，防止误删单个字母答案
                cleaned = re.sub(r'^[A-Za-z]\s*[.、:：)?）]?\s*|[.,!?;:，。！？；：]', '', c) if len(c) > 1 else c
                cleaned_res.append(cleaned.strip())

            return cleaned_res

        def normalize_text(text: str) -> str:
            if not isinstance(text, str):
                text = str(text)
            # 统一常见异体字符，降低“风/⻛”类差异导致的匹配失败。
            char_map = str.maketrans({
                '⻛': '风',
                '⻔': '门',
                '⻋': '车',
                '⻢': '马',
            })
            normalized = text.translate(char_map)
            normalized = re.sub(r'^[A-Za-z]\s*[.、:：)?）]?\s*', '', normalized)
            normalized = re.sub(r'\s+', '', normalized)
            normalized = re.sub(r'[，。！？；：,.!?;:()（）\[\]【】"“”‘’\-_/\\|]', '', normalized)
            return normalized.lower()

        def get_option_text(option: str) -> str:
            return re.sub(r'^[A-Za-z]\s*[.、:：)?）]?\s*', '', option).strip()

        def best_option_by_similarity(target: str, options: list, threshold: float = 0.8) -> str:
            if not target or not options:
                return ""
            target_norm = normalize_text(target)
            if not target_norm:
                return ""

            best_letter = ""
            best_score = 0.0
            for option in options:
                option_text = get_option_text(option)
                option_norm = normalize_text(option_text)
                if not option_norm:
                    continue
                score = SequenceMatcher(None, target_norm, option_norm).ratio()
                if score > best_score:
                    best_score = score
                    best_letter = option[:1]

            if best_score >= threshold:
                logger.info(f"相似度兜底匹配成功: {best_letter} (score={best_score:.2f}, threshold={threshold:.2f})")
                return best_letter
            return ""

        def is_subsequence(a, o):
            iter_o = iter(o)
            return all(c in iter_o for c in a)

        # FIXME: Use tenacity for retrying
        def with_retry(max_retries=3, delay=1):
            def decorator(func):
                def wrapper(*args, **kwargs):
                    retries = 0
                    while retries < max_retries:
                        try:
                            _resp = func(*args, **kwargs)

                            # 未创建完成该测验则不进行答题，目前遇到的情况是未创建完成等同于没题目
                            if '教师未创建完成该测验' in _resp.text:
                                raise PermissionError("教师未创建完成该测验")

                            questions = decode_questions_info(_resp.text)

                            if _resp.status_code == 200 and questions.get("questions"):
                                return (_resp, questions)

                            logger.warning(
                                f"无效响应 (Code: {getattr(_resp, 'status_code', 'Unknown')}), 重试中... ({retries + 1}/{max_retries})")

                        except requests.exceptions.RequestException as e:
                            logger.warning(f"请求失败: {str(e)[:50]}, 重试中... ({retries + 1}/{max_retries})")
                        retries += 1
                        time.sleep(delay * (2 ** retries))
                    raise MaxRetryExceeded(f"超过最大重试次数 ({max_retries})")

                return wrapper

            return decorator

        # 学习通这里根据参数差异能重定向至两个不同接口, 需要定向至https://mooc1.chaoxing.com/mooc-ans/workHandle/handle
        _session = SessionManager.get_session()

        _url = "https://mooc1.chaoxing.com/mooc-ans/api/work"

        @with_retry(max_retries=3, delay=1)
        def fetch_response():
            return _session.get(
                _url,
                params={
                    "api": "1",
                    "workId": _job["jobid"].replace("work-", ""),
                    "jobid": _job["jobid"],
                    "originJobId": _job["jobid"],
                    "needRedirect": "true",
                    "skipHeader": "true",
                    "knowledgeid": str(_job_info["knowledgeid"]),
                    "ktoken": _job_info["ktoken"],
                    "cpi": _job_info["cpi"],
                    "ut": "s",
                    "clazzId": _course["clazzId"],
                    "type": "",
                    "enc": _job["enc"],
                    "mooc2": "1",
                    "courseid": _course["courseId"],
                }
            )

        final_resp = {}
        questions = {}

        try:
            final_resp, questions = fetch_response()
        except Exception as e:
            logger.error(f"请求失败: {e}")
            return StudyResult.ERROR

        _ORIGIN_HTML_CONTENT = final_resp.text  # 用于配合输出网页源码, 帮助修复#391错误

        # 搜题
        total_questions = len(questions["questions"])
        found_answers = 0
        for q in questions["questions"]:
            logger.debug(f"当前题目信息 -> {q}")
            # 添加搜题延迟 #428 - 默认0s延迟
            query_delay = self.kwargs.get("query_delay", 0)
            time.sleep(query_delay)
            res = self.tiku.query(q)
            answer = ""
            if not res:
                # 随机答题
                answer = random_answer(q["options"])
                q[f'answerSource{q["id"]}'] = "random"
            else:
                # 根据响应结果选择答案
                if q["type"] == "multiple":
                    # 多选处理
                    options_list = multi_cut(q["options"])
                    res_list = multi_cut(res)
                    if res_list is not None and options_list is not None:
                        for _a in clean_res(res_list):
                            matched = False
                            for o in options_list:
                                if (
                                        is_subsequence(_a, o)  # 去掉各种符号和前面ABCD的答案应当是选项的子序列
                                ):
                                    answer += o[:1]
                                    matched = True
                                    break # 找到匹配项后立即停止，防止重复添加
                            if not matched:
                                best_letter = best_option_by_similarity(_a, options_list, threshold=0.8)
                                if best_letter:
                                    answer += best_letter
                        # 对答案进行排序, 否则会提交失败
                        answer = "".join(sorted(set(answer)))
                    # else 如果分割失败那么就直接到下面去随机选
                elif q["type"] == "single":
                    # 单选也进行切割，主要是防止返回的答案有异常字符
                    options_list = multi_cut(q["options"])
                    if options_list is not None:
                        t_res = clean_res(res)
                        for o in options_list:
                            if is_subsequence(t_res[0], o):
                                answer = o[:1]
                                break
                        if not answer and t_res:
                            answer = best_option_by_similarity(t_res[0], options_list, threshold=0.8)
                elif q["type"] == "judgement":
                    answer = "true" if self.tiku.judgement_select(res) else "false"
                elif q["type"] == "completion":
                    if isinstance(res, list):
                        answer = "".join(res)
                    elif isinstance(res, str):
                        answer = res
                else:
                    # 其他类型直接使用答案 （目前仅知有简答题，待补充处理）
                    answer = res

                if not answer:  # 检查 answer 是否为空
                    logger.warning(f"找到答案但答案未能匹配 -> {res}\t随机选择答案")
                    answer = random_answer(q["options"])  # 如果为空，则随机选择答案
                    q[f'answerSource{q["id"]}'] = "random"
                else:
                    logger.info(f"成功获取到答案：{answer}")
                    q[f'answerSource{q["id"]}'] = "cover"
                    found_answers += 1
            # 填充答案
            q["answerField"][f'answer{q["id"]}'] = answer
            logger.info(f'{q["title"]} 填写答案为 {answer}')
        cover_rate = (found_answers / total_questions) * 100
        logger.info(f"章节检测题库覆盖率： {cover_rate:.0f}%")
        # 提交模式  现在与题库绑定,留空直接提交, 1保存但不提交
        if self.tiku.get_submit_params() == "1":
            questions["pyFlag"] = "1"
        elif cover_rate >= self.tiku.COVER_RATE * 100 or self.rollback_times >= 1:
            questions["pyFlag"] = ""
        else:
            questions["pyFlag"] = "1"
            logger.info(f"章节检测题库覆盖率低于{self.tiku.COVER_RATE * 100:.0f}%，不予提交")
        # 组建提交表单
        if questions["pyFlag"] == "1":
            for q in questions["questions"]:
                questions.update(
                    {
                        f'answer{q["id"]}':
                            q["answerField"][f'answer{q["id"]}'] if q[f'answerSource{q["id"]}'] == "cover" else '',
                        f'answertype{q["id"]}': q["answerField"][f'answertype{q["id"]}'],
                    }
                )
        else:
            for q in questions["questions"]:
                questions.update(
                    {
                        f'answer{q["id"]}': q["answerField"][f'answer{q["id"]}'],
                        f'answertype{q["id"]}': q["answerField"][f'answertype{q["id"]}'],
                    }
                )

        del questions["questions"]

        res = _session.post(
            "https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew",
            data=questions,
            headers={
                "Host": "mooc1.chaoxing.com",
                "sec-ch-ua-platform": '"Windows"',
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "sec-ch-ua": '"Microsoft Edge";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "sec-ch-ua-mobile": "?0",
                "Origin": "https://mooc1.chaoxing.com",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
                # "Referer": "https://mooc1.chaoxing.com/mooc-ans/work/doHomeWorkNew?courseId=246831735&workAnswerId=52680423&workId=37778125&api=1&knowledgeid=913820156&classId=107515845&oldWorkId=07647c38d8de4c648a9277c5bed7075a&jobid=work-07647c38d8de4c648a9277c5bed7075a&type=&isphone=false&submit=false&enc=1d826aab06d44a1198fc983ed3d243b1&cpi=338350298&mooc2=1&skipHeader=true&originJobId=work-07647c38d8de4c648a9277c5bed7075a",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,ja;q=0.5",
            },
        )
        if res.status_code == 200:
            res_json = res.json()
            if res_json["status"]:
                logger.info(f'{"提交" if questions["pyFlag"] == "" else "保存"}答题成功 -> {res_json["msg"]}')
            else:
                logger.error(f'{"提交" if questions["pyFlag"] == "" else "保存"}答题失败 -> {res_json["msg"]}')
                return StudyResult.ERROR
        else:
            logger.error(f'{"提交" if questions["pyFlag"] == "" else "保存"}答题失败 -> {res.text}')
            return StudyResult.ERROR
        return StudyResult.SUCCESS

    def study_read(self, _course, _job, _job_info) -> StudyResult:
        """
        阅读任务学习, 仅完成任务点, 并不增长时长
        """
        _session = SessionManager.get_session()
        _resp = _session.get(
            url="https://mooc1.chaoxing.com/ananas/job/readv2",
            params={
                "jobid": _job["jobid"],
                "knowledgeid": _job_info["knowledgeid"],
                "jtoken": _job["jtoken"],
                "courseid": _course["courseId"],
                "clazzid": _course["clazzId"],
            },
        )
        if _resp.status_code != 200:
            logger.error(f"阅读任务学习失败 -> [{_resp.status_code}]{_resp.text}")
            return StudyResult.ERROR
        else:
            _resp_json = _resp.json()
            logger.info(f"阅读任务学习 -> {_resp_json['msg']}")
            return StudyResult.SUCCESS

    def study_emptypage(self, _course, point):
        _session = SessionManager.get_session()
        # &cpi=0&verificationcode=&mooc2=1&microTopicId=0&editorPreview=0
        _resp = _session.get(
            url="https://mooc1.chaoxing.com/mooc-ans/mycourse/studentstudyAjax",
            params={
                "courseId": _course["courseId"],
                "clazzid": _course["clazzId"],
                "chapterId": point["id"],
                "cpi": _course["cpi"],
                "verificationcode": "",
                "mooc2": 1,
                "microTopicId": 0,
                "editorPreview": 0,
            },
        )
        if _resp.status_code != 200:
            logger.error(f"空页面任务失败 -> [{_resp.status_code}]{point['title']}")
            return StudyResult.ERROR
        else:
            logger.info(f"空页面任务完成 -> {point['title']}")
            return StudyResult.SUCCESS
