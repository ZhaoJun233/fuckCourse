"""Explicit application data contract shared by the spec and archive audit."""
from pathlib import Path, PurePosixPath

PLATFORM_SOURCES = (
    "chaoxing/main.py",
    "chaoxing/api/answer.py", "chaoxing/api/answer_check.py", "chaoxing/api/base.py",
    "chaoxing/api/cipher.py", "chaoxing/api/config.py", "chaoxing/api/cookies.py",
    "chaoxing/api/cxsecret_font.py", "chaoxing/api/decode.py", "chaoxing/api/exceptions.py",
    "chaoxing/api/font_decoder.py", "chaoxing/api/live.py", "chaoxing/api/live_process.py",
    "chaoxing/api/logger.py", "chaoxing/api/notification.py",
    "welearn/welearn_decompiled.py",
    "zhs/main.py", "zhs/fucker.py", "zhs/logger.py", "zhs/ObjDict.py",
    "zhs/push.py", "zhs/sign.py", "zhs/utils.py", "zhs/zd_utils.py",
    "yuketang/main.py", "yuketang/yuketang_login.py",
)
# The font loader uses _MEIPASS/resource in frozen mode.
RESOURCE_DESTINATIONS = (
    ("chaoxing/resource/font_map_table.json", "chaoxing/resource"),
    ("chaoxing/resource/font_map_table.json", "resource"),
)
PLATFORM_DIRS = {"chaoxing", "welearn", "zhs", "yuketang"}


def project_datas(root):
    root = Path(root).resolve()
    entries = [(name, str(PurePosixPath(name).parent)) for name in PLATFORM_SOURCES]
    entries.extend(RESOURCE_DESTINATIONS)
    result = []
    for source, destination in entries:
        path = root / source
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Missing or unsafe build input: {source}")
        result.append((str(path), destination))
    return result


def expected_project_entries():
    entries = set(PLATFORM_SOURCES)
    entries.update(f"{destination}/{PurePosixPath(source).name}" for source, destination in RESOURCE_DESTINATIONS)
    return entries


def validate_archive_names(names):
    normalized = {name.replace("\\", "/") for name in names}
    expected = expected_project_entries()
    forbidden = []
    for name in normalized:
        path = PurePosixPath(name)
        basename = path.name.lower()
        if path.parts and path.parts[0] in PLATFORM_DIRS and name not in expected:
            forbidden.append(name)
        elif basename in {"config.json", "cookies.json", "yuketang_config.json"} or basename.endswith((".log", ".bak")):
            forbidden.append(name)
    missing = expected - normalized
    if forbidden or missing:
        raise ValueError(f"Unsafe archive entries: {sorted(forbidden)}; missing application entries: {sorted(missing)}")
