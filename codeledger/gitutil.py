"""Thin, defensive wrappers around the ``git`` CLI.

Every git call goes through :func:`run_git` which:
  * never pages (``GIT_PAGER=cat``),
  * decodes as UTF-8 with replacement (robust against odd bytes),
  * raises :class:`GitError` (a user-facing :class:`LedgerError`) with the
    stderr text when a command fails.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from codeledger.errors import LedgerError

__all__ = [
    "GitError",
    "TRAILER_PREFIX",
    "run_git",
    "git_available",
    "repo_root",
    "ensure_git_repo",
    "changed_files",
    "staged_files",
    "tracked_files",
    "commit_info",
    "CommitInfo",
    "head_commit",
    "trailer_ids",
    "BlameBlock",
    "blame_file",
    "append_ledger_trailer",
]

#: Commit-message trailer that links commits to ledger entries.
TRAILER_PREFIX = "Ledger-ID"

#: Zero sha used by git blame for "Not Committed Yet" working-tree lines.
_NOT_COMMITTED_SHA = "0" * 40


class GitError(LedgerError):
    """git invocation or repository-state problem (message is user-facing)."""


def git_env() -> Dict[str, str]:
    """Environment for git calls: never page, keep the terminal width sane."""
    env = dict(os.environ)
    env["GIT_PAGER"] = "cat"
    env["PAGER"] = "cat"
    return env


def git_available() -> bool:
    """True when the ``git`` executable is reachable on PATH."""
    try:
        run_git(["--version"], cwd=None)
        return True
    except (GitError, OSError):
        return False


def run_git(args: List[str], cwd: Optional[Path], check: bool = True) -> str:
    """Run git in ``cwd``, return stdout text. Raise :class:`GitError` if failing."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=None if cwd is None else str(cwd),
            env=git_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise GitError(
            f"could not run git ({exc}). Is git installed and on PATH?"
        ) from exc
    if check and proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout).strip()
        raise GitError(f"git {args[0]} failed: {stderr}")
    return proc.stdout


def repo_root(cwd: Path) -> Optional[Path]:
    """Absolute work-tree root containing ``cwd``, or ``None``."""
    try:
        out = run_git(["rev-parse", "--show-toplevel"], cwd=cwd, check=False)
    except GitError:
        return None
    out = out.strip()
    if not out:
        return None
    try:
        path = Path(out).resolve()
    except OSError:
        return None
    return path if path.is_dir() else None


def ensure_git_repo(cwd: Path) -> Path:
    """Return the repo root or raise a friendly :class:`LedgerError`."""
    root = repo_root(cwd)
    if root is None:
        raise LedgerError(
            "This directory is not inside a git repository.\n"
            "CodeLedger reads history from git, so run 'git init' first,\n"
            "then 'ledger init'."
        )
    return root


# ---------------------------------------------------------------------------
# Working-tree changes
# ---------------------------------------------------------------------------


def changed_files(cwd: Path) -> List[str]:
    """Staged + unstaged + untracked file paths (POSIX-style, deduped, sorted).

    Used as the default ``--files`` value of ``ledger log``. CodeLedger's own
    data (``.ledger/**``) is never listed: bookkeeping files are not the AI
    change being logged.
    """
    out = run_git(["status", "--porcelain=v1", "-z"], cwd=cwd, check=False)
    files: List[str] = []
    seen: Set[str] = set()
    tokens = out.split("\0")
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        i += 1
        if len(tok) < 4 or tok[2] != " ":
            continue
        path = tok[3:].replace("\\", "/")
        if path == ".ledger" or path.startswith(".ledger/"):
            continue  # CodeLedger's own bookkeeping, not an AI change
        if path and path not in seen:
            seen.add(path)
            files.append(path)
        if tok[0] in ("R", "C") or tok[1] in ("R", "C"):
            i += 1  # rename/copy records carry a second token: the origin path
    return sorted(files)


def staged_files(cwd: Path) -> List[str]:
    """Paths currently staged for commit."""
    out = run_git(["diff", "--cached", "--name-only", "-z"], cwd=cwd, check=False)
    return sorted({t.replace("\\", "/") for t in out.split("\0") if t})


def tracked_files(cwd: Path) -> List[str]:
    """All tracked file paths (used by the UI's blame file picker)."""
    out = run_git(["ls-files", "-z"], cwd=cwd, check=False)
    return sorted({t.replace("\\", "/") for t in out.split("\0") if t})


# ---------------------------------------------------------------------------
# Commits
# ---------------------------------------------------------------------------


@dataclass
class CommitInfo:
    """Everything the blame screens need about one commit."""

    sha: str
    short: str
    author: str
    author_email: str
    date: str
    message: str
    trailer_ids: List[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.trailer_ids is None:
            self.trailer_ids = []


def trailer_ids(message: str) -> List[str]:
    """Extract ``Ledger-ID:`` trailer entry ids from a commit message."""
    prefix = TRAILER_PREFIX.lower() + ":"
    ids: List[str] = []
    for line in message.splitlines():
        if not line.lower().startswith(prefix):
            continue
        for part in line.split(":", 1)[1].replace(";", ",").split(","):
            part = part.strip()
            if re.fullmatch(r"L-\d{3,}", part) and part not in ids:
                ids.append(part)
    return ids

_FORMAT = "\x01".join(["%H", "%h", "%an", "%ae", "%aI", "%B"])


def commit_info(cwd: Path, sha: str) -> CommitInfo:
    """Author/date/message for ``sha`` plus its ledger trailer ids."""
    out = run_git(["show", "-s", f"--format={_FORMAT}", sha], cwd=cwd)
    parts = out.split("\x01", 5)
    if len(parts) < 6:
        raise GitError(f"could not parse commit information for {sha[:10]}.")
    full, short, author, email, date, message = parts[:6]
    return CommitInfo(
        sha=full,
        short=short,
        author=author,
        author_email=email,
        date=date,
        message=message.rstrip("\n"),
        trailer_ids=trailer_ids(message),
    )


def head_commit(cwd: Path) -> Optional[CommitInfo]:
    """Current HEAD commit, or ``None`` when the repo has no commits yet."""
    try:
        return commit_info(cwd, "HEAD")
    except LedgerError:
        return None


# ---------------------------------------------------------------------------
# commit-message trailer injection (used by the prepare-commit-msg hook)
# ---------------------------------------------------------------------------


def append_ledger_trailer(text: str, entry_ids: List[str]) -> str:
    """Append ``Ledger-ID:`` trailer lines to a commit message being composed.

    Handles the git editor layout: the instruction comment block that git
    appends to the message file is left untouched (the trailer is inserted
    before it) and exactly one blank line separates body and trailers.
    """
    lines = text.rstrip("\n").split("\n")
    # The trailing block of standalone '#' comment lines is editorial only.
    end_of_body = len(lines)
    while end_of_body > 0:
        s = lines[end_of_body - 1].strip()
        if s.startswith("#") or s == "":
            end_of_body -= 1
        else:
            break
    body = lines[:end_of_body]
    tail = lines[end_of_body:]
    trailer = f"{TRAILER_PREFIX}: " + ", ".join(entry_ids)
    # Strip a pre-existing blank line before inserting our own separator.
    while body and body[-1] == "":
        body.pop()
    if body:
        body.append("")
    return "\n".join([*body, trailer, *tail]).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Blame
# ---------------------------------------------------------------------------


@dataclass
class BlameBlock:
    """One ``git blame --porcelain`` result for a single line."""

    sha: str
    orig_line: int
    final_line: int
    author: str = ""
    author_mail: str = ""
    summary: str = ""
    text: str = ""
    boundary: bool = False

    @property
    def uncommitted(self) -> bool:
        """True for changes not part of any commit ('Not Committed Yet')."""
        return not self.sha or set(self.sha) == {"0"}


_HEADER_RE = re.compile(
    r"^(?P<meta>[\^])?(?P<sha>[0-9a-fA-F]{4,40}) (?P<orig>\d+) (?P<final>\d+)"
    r"(?: (?P<count>\d+))?$"
)

_META_KEYS = ("author-mail", "author-time", "author-tz", "author", "summary", "filename")


def blame_file(cwd: Path, path: str) -> Dict[int, BlameBlock]:
    """Run ``git blame --porcelain`` over a file; ``{line -> block}`` (1-based).

    Raises :class:`GitError` when git rejects the path (untracked/missing), so
    callers can translate the failure into a friendly message.
    """
    out = run_git(["blame", "--porcelain", "--", path], cwd=cwd)
    blocks: Dict[int, BlameBlock] = {}
    meta_by_sha: Dict[str, Dict[str, str]] = {}
    current: Optional[Tuple[str, int, int, bool]] = None  # (sha, orig, final, boundary)
    remaining = 0
    consumed = 0

    def parse_raw(raw: str) -> None:
        nonlocal current, remaining, consumed
        if raw.startswith("\t"):
            if current is None:
                return
            sha, orig, final, boundary = current
            line_no = final + consumed
            consumed += 1
            remaining -= 1
            meta = meta_by_sha.get(sha, {})
            blocks[line_no] = BlameBlock(
                sha=sha,
                orig_line=orig + consumed - 1,
                final_line=line_no,
                author=meta.get("author", ""),
                author_mail=meta.get("author-mail", ""),
                summary=meta.get("summary", ""),
                text=text,
                boundary=boundary,
            )
            if remaining <= 0:
                current = None
                consumed = 0
            return
        match = _HEADER_RE.match(raw)
        if match:
            sha = match.group("sha").lower()
            orig = int(match.group("orig"))
            final = int(match.group("final"))
            count = int(match.group("count") or 1)
            current = (sha, orig, final, bool(match.group("meta")))
            remaining = count
            consumed = 0
            meta_by_sha.setdefault(sha, {})
            return
        if current is None:
            return
        # Metadata line: "author X", "author-mail <x>", "summary X", ...
        meta = meta_by_sha[current[0]]
        for key in _META_KEYS:
            if raw == key:
                meta[key] = ""
                break
            if raw.startswith(key + " "):
                meta[key] = raw[len(key) + 1 :].strip()
                break

    for raw in out.split("\n"):
        text = raw[1:] if raw.startswith("\t") else ""
        parse_raw(raw)

    return blocks
