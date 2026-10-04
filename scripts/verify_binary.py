"""Audit and exercise a built binary without login or network operations."""
import argparse
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_support import validate_archive_names


def verify_binary(executable, timeout=90):
    from PyInstaller.archive.readers import CArchiveReader
    executable = Path(executable).resolve(strict=True)
    validate_archive_names(CArchiveReader(str(executable)).toc)
    print("ARCHIVE_ALLOWLIST_OK")
    result = subprocess.run([str(executable)], input="0\n", capture_output=True,
                            encoding="utf-8", errors="replace", timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"Menu smoke failed ({result.returncode}): {result.stdout}\n{result.stderr}")
    print("MENU_EXIT_OK")
    for platform in ("chaoxing", "welearn", "zhs", "yuketang"):
        result = subprocess.run([str(executable), "--offline-smoke", platform],
                                stdin=subprocess.DEVNULL, capture_output=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
        marker = f"SMOKE_IMPORT_OK {platform}"
        if result.returncode != 0 or marker not in result.stdout.splitlines():
            raise RuntimeError(f"{platform} import smoke failed ({result.returncode}): {result.stdout}\n{result.stderr}")
        print(marker)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable")
    parser.add_argument("--timeout", type=int, default=90)
    args = parser.parse_args()
    verify_binary(args.executable, args.timeout)
