"""``ledger ui`` - a local web dashboard served by the Python standard library.

Endpoints (all JSON, except the dashboard itself):
  * ``/``                 - the single-file dashboard (inline CSS/JS)
  * ``/api/health``       - repo/name/counts snapshot (always 200)
  * ``/api/entries``      - all ledger entries + aggregate counts
  * ``/api/entry/<id>``   - one entry
  * ``/api/blame``        - ``?file=&line=`` -> CLI blame JSON; without
    ``line`` -> per-line annotations for the code viewer
  * ``/api/capsule``      - capsule text (+ ``missing`` flag)
  * ``/api/files``        - blameable file list for the picker
  * ``/api/file``         - ``?path=`` -> file content (traversal-guarded)

Security notes: the server binds to 127.0.0.1 only; path parameters resolve
inside the repository only; ``.git`` and ``.ledger/redact_map.json`` are
never served.
"""
from __future__ import annotations

import json
import socket
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from codeledger import capsule as capsule_mod
from codeledger import gitutil, store
from codeledger.blame import reveal, reveal_to_dict
from codeledger.errors import LedgerError
from codeledger.util import console, read_json

__all__ = ["build_server", "run_ui", "ApiError", "api_health", "safe_file"]

INDEX_BYTES: bytes = b""
_MAX_FILE_BYTES = 512 * 1024

#: Largest request body ``do_POST`` drains before replying (loopback UI;
#: nobody is expected to POST at all, so this only guards TCP hygiene).
_MAX_BODY_DRAIN = 1024 * 1024


class ApiError(Exception):
    """A controlled endpoint failure carrying its HTTP status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _index_bytes() -> bytes:
    """Read the packaged dashboard HTML (single file, inline CSS/JS)."""
    global INDEX_BYTES
    if not INDEX_BYTES:
        INDEX_BYTES = resource_files("codeledger.web").joinpath("index.html").read_bytes()
    return INDEX_BYTES


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def safe_file(repo: Path, rel: str) -> Path:
    """Resolve ``rel`` inside ``repo``; raise ApiError on any escape attempt."""
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel or "\x00" in rel:
        raise ApiError(400, "bad path")
    candidate = Path(rel)
    if candidate.is_absolute() or candidate.drive:
        raise ApiError(400, "absolute paths are not allowed")
    parts = candidate.parts
    if ".." in parts:
        raise ApiError(400, "path traversal is not allowed")
    if ".git" in parts:
        raise ApiError(403, ".git is never served")
    if "/".join(parts) == f"{store.LEDGER_DIR}/{store.REDACT_MAP_FILE}":
        raise ApiError(403, "the redaction map is not served")
    try:
        resolved = (repo / candidate).resolve()
        repo_resolved = repo.resolve()
    except OSError as exc:
        raise ApiError(400, f"bad path: {exc}") from exc
    if resolved == repo_resolved or repo_resolved not in resolved.parents:
        raise ApiError(400, "path traversal is not allowed")
    if not resolved.is_file():
        raise ApiError(404, "file not found")
    return resolved


def _denied_path(rel: str) -> bool:
    """True for paths the UI API must never expose."""
    posix = rel.replace("\\", "/").lstrip("/").lower()
    if posix == f"{store.LEDGER_DIR}/{store.REDACT_MAP_FILE}":
        return True
    return ".git/" in posix + "/"


# ---------------------------------------------------------------------------
# Endpoint implementations (pure, testable)
# ---------------------------------------------------------------------------


def api_health(repo: Path) -> Dict[str, Any]:
    """Aggregate status used by the top bar and the Health screen."""
    try:
        branch = (
            gitutil.run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo, check=False).strip()
            or None
        )
    except LedgerError:
        branch = None
    entries = store.load_entries(repo)
    counts = store.counts(entries)
    models: Dict[str, int] = {}
    for entry in entries:
        models[entry.model] = models.get(entry.model, 0) + 1
    data = read_json(repo / store.LEDGER_DIR / store.REDACT_MAP_FILE)
    return {
        "ok": True,
        "repo": repo.name,
        "branch": branch,
        "initialized": store.discover_ledger_root(repo) is not None,
        "counts": counts,
        "models": models,
        "redaction_placeholders": len(data) if isinstance(data, dict) else 0,
    }


def api_entries(repo: Path) -> Dict[str, Any]:
    entries = store.load_entries(repo)
    return {
        "entries": [e.to_dict() for e in entries],
        "counts": store.counts(entries),
    }


def api_blame_line(repo: Path, file: str, line: int) -> Dict[str, Any]:
    """Same payload as ``ledger blame file:line --json``."""
    outcome = reveal(repo, f"{file}:{line}")
    return reveal_to_dict(outcome)


def api_blame_full(repo: Path, file: str) -> Dict[str, Any]:
    """Per-line annotations for the blame viewer (one git blame call)."""
    root = gitutil.ensure_git_repo(repo)
    rel = file.replace("\\", "/")
    if not (root / rel).is_file():
        raise ApiError(404, "file not found")
    try:
        blocks = gitutil.blame_file(root, rel)
    except gitutil.GitError as exc:
        raise ApiError(400, str(exc)) from exc
    commit_cache: Dict[str, Dict[str, Any]] = {}
    needed_ids: List[str] = []

    def commit_payload(sha: str) -> Dict[str, Any]:
        payload = commit_cache.get(sha)
        if payload is None:
            info = gitutil.commit_info(root, sha)
            payload = {
                "sha": info.sha,
                "short": info.short,
                "author": info.author,
                "author_email": info.author_email,
                "date": info.date,
                "message": info.message,
                "entry_ids": info.trailer_ids,
                "ai": bool(info.trailer_ids),
            }
            commit_cache[sha] = payload
            needed_ids.extend(info.trailer_ids)
        return payload

    lines: List[Dict[str, Any]] = []
    for line_no in sorted(blocks):
        block = blocks[line_no]
        if block.uncommitted:
            lines.append(
                {
                    "no": line_no,
                    "text": block.text,
                    "sha": "",
                    "uncommitted": True,
                    "entry_ids": [],
                    "ai": False,
                }
            )
            continue
        payload = commit_payload(block.sha)
        lines.append(
            {
                "no": line_no,
                "text": block.text,
                "sha": payload["short"],
                "uncommitted": False,
                "entry_ids": list(payload["entry_ids"]),
                "ai": bool(payload["entry_ids"]),
            }
        )

    deduped: List[str] = []
    for payload in commit_cache.values():
        for entry_id in payload["entry_ids"]:
            if entry_id not in deduped:
                deduped.append(entry_id)
    entries_index: Dict[str, Dict[str, Any]] = {}
    for entry in store.find_entries(root, deduped):
        entries_index[entry.id] = entry.to_dict()
    return {
        "file": rel,
        "lines": lines,
        "commits": commit_cache,
        "entries": entries_index,
    }


def api_capsule(repo: Path) -> Dict[str, Any]:
    """Existing capsule text, or a freshly generated preview + hint."""
    existing = repo / store.LEDGER_DIR / store.CAPSULE_FILE
    if existing.is_file():
        return {
            "missing": False,
            "text": existing.read_text(encoding="utf-8", errors="replace"),
        }
    entries = store.load_entries(repo)
    branch: Optional[str] = None
    try:
        branch = (
            gitutil.run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo, check=False).strip()
            or None
        )
    except LedgerError:
        branch = None
    text = capsule_mod.generate_capsule(entries, repo, branch)
    return {"missing": True, "text": text, "hint": "Run `ledger capsule` to write CAPSULE.md."}


def api_file(repo: Path, rel: str) -> Dict[str, Any]:
    """Serve one file's content (text only) with strict path checks."""
    resolved = safe_file(repo, rel)
    raw = resolved.read_bytes()
    if b"\x00" in raw[:4096]:
        return {"path": rel, "binary": True, "size": len(raw)}
    truncated = len(raw) > _MAX_FILE_BYTES
    text = raw[:_MAX_FILE_BYTES].decode("utf-8", errors="replace")
    return {
        "path": rel.replace("\\", "/"),
        "binary": False,
        "size": len(raw),
        "truncated": truncated,
        "content": text,
        "lines": text.splitlines(),
    }


def api_files(repo: Path) -> Dict[str, Any]:
    """Blameable file list for the picker (tracked, hygiene-filtered)."""
    try:
        tracked = gitutil.tracked_files(repo)
    except gitutil.GitError as exc:
        raise ApiError(400, str(exc)) from exc
    keep: List[str] = []
    for path in tracked:
        posix = path.replace("\\", "/")
        if posix.endswith(f"/{store.REDACT_MAP_FILE}") and posix.startswith(
            f"{store.LEDGER_DIR}/"
        ):
            continue
        keep.append(posix)
    return {"files": keep}


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    """The HTTP handler; ``repo`` is attached per-server by build_server."""

    server_version = "CodeLedger"
    sys_version = ""
    repo: Path = Path.cwd()

    def log_message(self, *args: Any) -> None:
        """Keep the host console calm (no per-request lines)."""

    def _query(self) -> Dict[str, str]:
        parsed = urlparse(self.path)
        out: Dict[str, str] = {}
        for key, values in parse_qs(parsed.query, keep_blank_values=True).items():
            out[key.lower()] = values[0]
        return out

    def _split(self) -> Tuple[str, Dict[str, str]]:
        parsed = urlparse(self.path)
        return (parsed.path.rstrip("/") or "/", self._query())

    def _send(
        self,
        payload: Dict[str, Any],
        status: int = 200,
        content_type: str = "application/json; charset=utf-8",
        body: Optional[bytes] = None,
    ) -> None:
        data = body if body is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def do_GET(self) -> None:
        try:
            path_name, query = self._split()
            if path_name in ("/", "/index.html"):
                self._send({}, 200, "text/html; charset=utf-8", _index_bytes())
                return
            if not path_name.startswith("/api/"):
                raise ApiError(404, "not found")
            if path_name == "/api/health":
                self._send(api_health(self.repo))
            elif path_name == "/api/entries":
                self._send(api_entries(self.repo))
            elif path_name.startswith("/api/entry/"):
                self._send(self._entry_by_id(path_name[len("/api/entry/") :]).to_dict())
            elif path_name == "/api/blame":
                self._blame(query)
            elif path_name == "/api/capsule":
                self._send(api_capsule(self.repo))
            elif path_name == "/api/files":
                self._send(api_files(self.repo))
            elif path_name == "/api/file":
                rel = query.get("path", "").strip()
                if not rel:
                    raise ApiError(400, "missing ?path= parameter")
                self._send(api_file(self.repo, rel))
            else:
                raise ApiError(404, "not found")
        except ApiError as exc:
            self._send({"error": exc.message}, exc.status)
        except LedgerError as exc:
            self._send({"error": str(exc)}, 400)
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            self._send({"error": f"internal error: {exc}"}, 500)

    def do_POST(self) -> None:
        # Read-only UI: no endpoint accepts POSTs. But before replying we
        # must drain the request body: this handler stays on HTTP/1.0
        # (the connection is closed after every request), and closing a
        # socket that still holds unread received bytes emits an RST that
        # can wipe the buffered 405 response before the client reads it -
        # surfaced as ConnectionAbortedError [WinError 10053] under load.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length > 0:
            self.rfile.read(min(length, _MAX_BODY_DRAIN))
        self._send({"error": "the CodeLedger UI is read-only"}, 405)

    def do_HEAD(self) -> None:
        self._send({}, 200, "text/plain; charset=utf-8", b"")

    def _blame(self, query: Dict[str, str]) -> None:
        file = query.get("file", "").strip()
        if not file:
            raise ApiError(400, "missing ?file= parameter")
        line = query.get("line", "").strip()
        if line:
            if not line.isdigit() or int(line) < 1:
                raise ApiError(400, "?line= must be a positive integer")
            self._send(api_blame_line(self.repo, file, int(line)))
        else:
            self._send(api_blame_full(self.repo, file))

    def _entry_by_id(self, entry_id: str) -> store.Entry:
        entries = {e.id: e for e in store.load_entries(self.repo)}
        found = entries.get(entry_id)
        if found is None:
            raise ApiError(404, f"no such entry: {entry_id}")
        return found


class CodeLedgerServer(ThreadingHTTPServer):
    """HTTP server bound to loopback, with quiet daemon threads."""

    daemon_threads = True
    allow_reuse_address = True


def build_server(repo: Path, port: int) -> CodeLedgerServer:
    """Bind to 127.0.0.1:$port for ``repo`` (port 0 = pick any free port)."""
    handler_class = type("LedgerRequestHandler", (_Handler,), {"repo": repo, "__module__": __name__})
    return CodeLedgerServer(("127.0.0.1", port), handler_class)


def _free_port(preferred: int, attempts: int = 50) -> int:
    """First free loopback port at/after ``preferred``."""
    for candidate in range(preferred, preferred + attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", candidate))
                sock.close()
                return candidate
            except OSError:
                continue
    raise LedgerError(f"no free port found from {preferred} (tried {attempts}).")


def run_ui(cwd: Path, *, port: int, open_browser: bool) -> None:
    """Serve the dashboard; Ctrl+C shuts down cleanly."""
    try:
        root = gitutil.ensure_git_repo(cwd)
    except LedgerError:
        root = cwd  # un-initialised repos get the friendly empty state UI
    try:
        server = build_server(root, port)
    except OSError:
        # Preferred port busy: try the next few ports, then give up.
        chosen = _free_port(port + 1)
        server = build_server(root, chosen)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    console.print(f"CodeLedger UI running at [bold]{url}[/bold] - press Ctrl+C to stop.")

    def open_later() -> None:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - the browser is optional
            pass

    if open_browser:
        threading.Timer(0.4, open_later).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        console.print("UI shut down.")
