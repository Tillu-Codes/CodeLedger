"""Small shared utilities: safe IO, dates and the shared rich console.

All persistence in CodeLedger goes through :func:`write_text_atomic` or the
store so a crash can never leave a half-written ledger behind.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from rich.console import Console

__all__ = [
    "console",
    "err_console",
    "now_iso",
    "write_text_lf",
    "write_bytes_atomic",
    "write_json_atomic",
    "read_json",
]

#: Primary console for normal output (respects NO_COLOR / non-TTY envs).
console: Console = Console()

#: Console for warnings/errors (stderr).
err_console: Console = Console(stderr=True, style="bold red")


def now_iso() -> str:
    """Current local time as an ISO 8601 UTC string with second precision."""
    stamp = _dt.datetime.now(_dt.timezone.utc)
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_text_lf(path: Path, text: str, *, atomic: bool = False) -> None:
    """Write UTF-8 text with LF endings, deterministically, on every OS.

    Line endings are normalised to ``\\n`` because git hooks are shell
    scripts: CRLF endings break shebangs on Windows/unix mixes.  With
    ``atomic=True`` the file is replaced via a temp file + rename so readers
    never observe a partial write.
    """
    data = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    if atomic:
        _atomic_replace(path, data)
    else:
        path.write_bytes(data)


def _atomic_replace(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically (temp file in same dir)."""
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # os.replace is atomic on POSIX and Windows (same volume/directory).
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Atomically replace ``path`` with ``data`` (UTF-8 agnostic)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_replace(path, data)


def write_text_atomic(path: Path, text: str) -> None:
    """Atomically write UTF-8/LF text."""
    write_bytes_atomic(path, text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8"))


def write_json_atomic(path: Path, obj: Any) -> None:
    """Atomically write pretty-printed JSON with LF endings."""
    text = json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    write_bytes_atomic(path, text.encode("utf-8"))


def read_json(path: Path) -> Any:
    """Read JSON from ``path`` or ``None`` if missing/corrupt."""
    try:
        return json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):
        return None
