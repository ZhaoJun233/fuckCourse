"""Replay progress comes from the server; future/ongoing lives remain pending."""
import time

from api.base import StudyResult, _draw_progress_bar, _wipe_bar
from api.live import Live, LiveError, LiveState, parse_live_status
from api.logger import logger


def _format_time(seconds):
    if seconds is None:
        return "未知"
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _show_status(live, snapshot, emit):
    percent = f"{snapshot.percent:.2f}%" if snapshot.percent is not None else "未知"
    message = (f"直播「{live.name}」[{snapshot.state.value}] | 平台进度 {percent} | "
               f"已观看 {_format_time(snapshot.watched_seconds)} / 总时长 {_format_time(snapshot.duration)}")
    if snapshot.state == LiveState.NOT_STARTED:
        message += f" | 预计开播 {snapshot.scheduled_at or '平台未提供'}"
    emit(message)
    logger.info(message)


class LiveProcessor:
    @staticmethod
    def run_live(live: Live, speed: float = 1.0, *, emit=print):
        """Only replay permitted by the platform; never infer completion from elapsed time."""
        prefix = f"直播回放 {live.name}"
        notice = ""
        try:
            snapshot = parse_live_status(live.get_status())
            _show_status(live, snapshot, emit)
            if snapshot.state in {LiveState.NOT_STARTED, LiveState.LIVING, LiveState.ENDED}:
                notice = "本轮不处理，保留待办；待平台开放回放后重新运行，不累计时长、不标记完成。"
                return StudyResult.DEFERRED
            if snapshot.state == LiveState.UNKNOWN:
                raise LiveError("无法确认直播是否允许回放，未累计时长。")
            if snapshot.duration is None:
                notice = "回放总时长尚未生成，保留待办；不使用默认30分钟代替。"
                return StudyResult.DEFERRED
            target = live.required_percent
            if snapshot.percent is None:
                raise LiveError("无法读取平台观看进度，未继续处理。")
            emit(f"回放完成目标 {target:g}%；显示的是平台确认进度，配置倍速不用于伪增直播累计时长。")
            stalled = 0
            while True:
                _draw_progress_bar(snapshot.duration * snapshot.percent / 100, snapshot.duration,
                                   prefix=prefix, suffix=f"平台 {snapshot.percent:.2f}% / 目标 {target:g}%")
                if snapshot.percent >= target:
                    notice = "平台确认已达到直播回放完成比例。"
                    return StudyResult.SUCCESS
                # Existing heartbeat protocol credits elapsed viewing time, not a speed multiplier.
                time.sleep(59)
                if not live.do_finish():
                    raise LiveError("直播时长请求未确认成功，已停止；未标记完成。")
                latest = parse_live_status(live.get_status())
                # Keep the console's reserved progress-bar rows stable. The bar
                # shows refreshed progress; detailed periodic snapshots go to the log.
                _show_status(live, latest, lambda message: None)
                if latest.state in {LiveState.NOT_STARTED, LiveState.LIVING, LiveState.ENDED}:
                    notice = f"平台直播状态改变：{latest.state.value}，停止处理并保留待办。"
                    return StudyResult.DEFERRED
                if latest.state != LiveState.REPLAY or latest.percent is None or latest.duration is None:
                    raise LiveError("无法确认更新后的回放进度，已停止；未标记完成。")
                stalled = stalled + 1 if latest.percent <= snapshot.percent else 0
                snapshot = latest
                if stalled >= 3:
                    raise LiveError("连续3次查询平台进度未增长，已停止；请在官方客户端核对，未标记完成。")
        except LiveError as error:
            notice = f"直播已停止：{error}"
            logger.warning(str(error))
            return StudyResult.ERROR
        finally:
            _wipe_bar(prefix)
            if notice:
                emit(notice)
