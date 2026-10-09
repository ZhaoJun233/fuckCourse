"""Local warning evidence, never a replacement for current server completion."""
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile


class ProgressError(ValueError):
    pass


class ProgressStore:
    def __init__(self, path=None):
        cookies = Path(os.environ.get("FUCKCOURSE_COOKIES") or "cookies.json").resolve()
        self.path = Path(path) if path is not None else cookies.parent / "progress_state.json"

    @staticmethod
    def _key(parts):
        return sha256(json.dumps(parts, ensure_ascii=False).encode("utf-8")).hexdigest()

    def check_and_record(self, uid, course, points):
        """Stop on regression/unreadable evidence; keep the previous snapshot intact."""
        context = [str(uid or "")] + [str(course.get(k) or "") for k in ("courseId", "clazzId", "cpi")]
        if not all(context) or not isinstance(points, list) or not points:
            raise ProgressError("无法读取有效账号、课程或章节进度，未开始处理。")
        all_points, completed = [], []
        for point in points:
            if (not isinstance(point, dict) or not point.get("id")
                    or type(point.get("has_finished")) is not bool):
                raise ProgressError("章节进度格式异常，未开始处理。")
            key = self._key([str(point["id"])])
            all_points.append(key)
            if point["has_finished"]:
                completed.append(key)
        if len(set(all_points)) != len(all_points):
            raise ProgressError("章节重复，无法可靠比较进度，未开始处理。")
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            data = {"version": 1, "courses": {}}
        except (ValueError, OSError):
            raise ProgressError("本地进度检查记录不可读取，已保留原文件；请先核查，不自动覆盖。") from None
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("courses"), dict):
            raise ProgressError("本地进度检查记录格式异常，已保留原文件。")
        course_key = self._key(context)
        previous = data["courses"].get(course_key)
        if previous is not None:
            if (not isinstance(previous, dict)
                    or not all(isinstance(previous.get(k), list)
                               and all(isinstance(x, str) for x in previous[k]) for k in ("points", "completed"))
                    or not set(previous["completed"]).issubset(previous["points"])):
                raise ProgressError("本地进度检查记录格式异常，已保留原文件。")
            missing = set(previous["points"]) - set(all_points)
            regressed = set(previous["completed"]) - set(completed)
            if missing or regressed:
                raise ProgressError(
                    f"检测到平台进度回退/章节变更：{len(regressed)} 个已完成章节失去完成标记，"
                    f"{len(missing)} 个章节无法读取。已停止，不自动重刷；请在官方平台核查异常学习记录，"
                    "或联系教师/平台确认。此前本地记录保留，不能用于恢复远端进度。")
        data["courses"][course_key] = {
            "points": all_points, "completed": completed,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=self.path.name + ".", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump(data, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise ProgressError("无法安全保存进度检查记录，未开始处理；旧记录未主动覆盖。") from None
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
