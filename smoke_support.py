"""Offline platform-import probe, invoked explicitly by the launcher."""
import ast
import logging
import os
from pathlib import Path
import sys
import tempfile

PLATFORMS = {
    "chaoxing": "main.py",
    "welearn": "welearn_decompiled.py",
    "zhs": "main.py",
    "yuketang": "main.py",
}


def _forbid_network(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.sendto"}:
        raise RuntimeError("Network is forbidden during offline smoke")


def smoke_import(app_dir, platform):
    if platform not in PLATFORMS:
        raise ValueError(f"Unknown smoke platform: {platform}")
    sys.addaudithook(_forbid_network)
    with tempfile.TemporaryDirectory(prefix="fuckcourse-smoke-") as data:
        log_dir = Path(data) / "logs"
        log_dir.mkdir()
        os.environ.update({
            "FUCKCOURSE_CONFIG": str(Path(data) / "config.json"),
            "FUCKCOURSE_COOKIES": str(Path(data) / "cookies.json"),
            "FUCKCOURSE_LOG_DIR": str(log_dir),
        })
        platform_dir = Path(app_dir) / platform
        os.chdir(platform_dir)
        sys.path.insert(0, str(platform_dir))
        try:
            # Some entry scripts log in at top level, so execute imports only.
            entry = platform_dir / PLATFORMS[platform]
            tree = ast.parse(entry.read_text(encoding="utf-8-sig"), filename=str(entry))
            imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
            module = ast.Module(body=imports, type_ignores=[])
            exec(compile(module, str(entry), "exec"), {"__name__": "__fuckcourse_smoke__", "__file__": str(entry)})
            if platform == "chaoxing":
                from api.cxsecret_font import FontHashDAO
                FontHashDAO()
            print(f"SMOKE_IMPORT_OK {platform}")
        finally:
            logging.shutdown()
            if "loguru" in sys.modules:
                sys.modules["loguru"].logger.remove()
