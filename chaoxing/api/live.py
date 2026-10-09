# -*- coding: utf-8 -*-
from dataclasses import dataclass
from enum import Enum
import math
import re
import time

import requests

from api.logger import logger

from api.base import SessionManager
from api.config import GlobalConst as gc


class LiveError(RuntimeError):
    """Fixed user-facing live messages, without request parameters or cookies."""


class LiveState(Enum):
    NOT_STARTED = "未开始"
    LIVING = "正在直播"
    REPLAY = "已结束可回放"
    ENDED = "已结束，不允许回放"
    UNKNOWN = "状态未知"


@dataclass(frozen=True)
class LiveSnapshot:
    state: LiveState
    duration: float | None
    watched_seconds: float | None
    percent: float | None
    scheduled_at: str


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def parse_live_status(payload):
    """Official live card: liveStatus 0/1/4; ifReview=0 allows replay, 1 denies it."""
    if not isinstance(payload, dict) or payload.get("status") is not True:
        raise LiveError("平台未返回有效的直播状态，未继续处理。")
    temp = payload.get("temp")
    data = temp.get("data") if isinstance(temp, dict) else None
    if not isinstance(data, dict):
        raise LiveError("直播状态结构发生变化，未继续处理。")
    code, review = _number(data.get("liveStatus")), _number(data.get("ifReview"))
    state = LiveState.UNKNOWN
    if code == 0:
        state = LiveState.NOT_STARTED
    elif code == 1:
        state = LiveState.LIVING
    elif code == 4 and review in (0, 1):
        state = LiveState.REPLAY if review == 0 else LiveState.ENDED
    duration = _number(data.get("duration")) or None
    milliseconds = _number(data.get("timeLong"))
    minutes = _number(data.get("timeLongValue"))
    watched = milliseconds / 1000 if milliseconds is not None else (minutes * 60 if minutes is not None else None)
    percent = _number(data.get("percentValue"))
    if percent is not None and percent > 100:
        percent = None
    scheduled = data.get("ygdate", "")
    if not isinstance(scheduled, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?", scheduled):
        scheduled = ""
    return LiveSnapshot(state, duration, watched, percent, scheduled)


class Live:
    def __init__(self, attachment: dict, defaults: dict, course_id: str):
        self.attachment = attachment
        self.defaults = defaults  # 包含用户ID、课程ID等信息
        self.course_id = course_id  # 课程ID
        self.name = self.attachment.get("property", {}).get("title", "未知直播")  # 直播名称
        self.headers = gc.HEADERS.copy()
        self.headers.update({
            "Referer": "https://mooc1.chaoxing.com/ananas/modules/live/index.html?v=2022-1214-1139"
        })

    @property
    def required_percent(self):
        # The official live card defaults to rt=0.9 when no ratio is supplied.
        ratio = _number(self.attachment.get("property", {}).get("rt", 0.9))
        if ratio is None or not 0 < ratio <= 1:
            raise LiveError("直播任务的完成比例无法识别，未继续处理。")
        return ratio * 100

    def do_finish(self):
        """提交直播观看时长（核心方法）"""
        # 从直播信息中提取关键参数
        stream_name = self.attachment.get("property", {}).get("streamName")
        vdoid = self.attachment.get("property", {}).get("vdoid")
        user_id = self.defaults.get("userid")
        
        if not all([stream_name, vdoid, user_id, self.course_id]):
            logger.error("缺少直播必要参数，无法提交时长")
            return False
        
        # 构造时长记录请求URL（超星直播时长记录接口）
        url = "https://zhibo.chaoxing.com/saveTimePc"
        params = {"streamName": stream_name, "vdoid": vdoid, "userId": user_id,
                  "isStart": 0, "t": int(time.time() * 1000), "courseId": self.course_id}
        
        # 发送请求记录时长
        session = SessionManager.get_session()
        try:
            response = session.get(url, params=params, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.text.strip() == "@success"  # 响应为@success表示提交成功
        except requests.RequestException:
            logger.error("直播时长请求失败，未确认累计结果；请在官方客户端核对。")
            return False

    def get_status(self) -> dict|None:
        """获取直播状态（总时长等信息）"""
        prop = self.attachment.get("property", {})
        live_id = prop.get("liveId")
        if not live_id and str(prop.get("_jobid", "")).startswith("live-"):
            live_id = str(prop["_jobid"])[len("live-"):]
        user_id = self.defaults.get("userid")
        clazz_id = self.defaults.get("clazzId")
        knowledge_id = self.defaults.get("knowledgeid")
        
        if not all([live_id, user_id, clazz_id, knowledge_id, self.course_id]):
            logger.error("缺少直播状态查询必要参数")
            return None
        
        # 构造直播状态请求URL
        status_url = "https://mooc1.chaoxing.com/ananas/live/liveinfo"
        params = {"liveid": live_id, "userid": user_id, "clazzid": clazz_id,
                  "knowledgeid": knowledge_id, "courseid": self.course_id,
                  "jobid": prop.get("jobid") or self.attachment.get("jobid") or prop.get("_jobid", ""), "ut": "s"}
        
        # 发送请求并解析状态（包含总时长）
        session = SessionManager.get_session()
        try:
            response = session.get(status_url, params=params, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError):
            logger.error("直播状态请求失败或返回格式异常，请在官方客户端检查。")
            return None
