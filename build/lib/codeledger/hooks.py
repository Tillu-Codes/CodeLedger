"""``ledger hook <name>`` - the implementation behind the installed git hooks.

The three hooks installed by ``ledger init`` all delegate here:

* ``prepare-commit-msg <msgfile> <source> [sha]`` - append a
  ``Ledger-ID: L-0001, ...`` trailer when pending entries exist (never
  duplicated on amend);
* ``post-commit`` - link the new commit sha to the trailed entries and
  re-render the capsule so it reflects the committed state;
* ``pre-commit`` - run ``ledger gate`` and let a failure block the commit.

git runs hooks with the repository root as the working directory, so every
lookup here is repo-relative and safe. All user-visible output is kept plain
because it appears inside ``git commit``'s output.
"""
from __future__ import annotations

from pathlib import Path
from typing import List

from codeledger import gate, store
from codeledger.capsule import refresh_capsule
from codeledger.errors import LedgerError
from codeledger.gitutil import append_ledger_trailer, head_commit, repo_root
from codeledger.util import console, write_text_lf

__all__ = ["run_hook", "SUPPORTED_HOOKS"]

SUPPORTED_HOOKS = ("prepare-commit-msg", "pre-commit", "post-commit")

#: Commit-message sources where injecting trailers is wrong (git manages
#: those messages itself; entries stay pending and link on the next commit).
_SKIP_SOURCES = {"merge", "squash"}


def run_hook(name: str, args: List[str], cwd: Path) -> int:
    """Dispatch one hook invocation; returns the process exit code."""
    if name not in SUPPORTED_HOOKS:
        raise LedgerError(
            f"unknown hook '{name}' (expected one of: {', '.join(SUPPORTED_HOOKS)})",
            exit_code=2,
        )
    root: Path = repo_root(cwd) or cwd
    if name == "prepare-commit-msg":
        return _hook_prepare_commit_msg(root, args)
    if name == "post-commit":
        return _hook_post_commit(root)
    return _hook_pre_commit(root)


def _pending_ids(root: Path) -> List[str]:
    """Ids of all pending entries (the ones the next commit will link)."""
    return [e.id for e in store.pending_entries(store.load_entries(root))]


def _hook_prepare_commit_msg(root: Path, args: List[str]) -> int:
    if not args:
        return 0  # defensive: git always passes the message file
    msg_file = Path(args[0])
    if not msg_file.is_absolute():
        msg_file = root / msg_file
    source = args[1].strip().lower() if len(args) > 1 else ""
    if source in _SKIP_SOURCES:
        return 0
    pending = _pending_ids(root)
    if not pending:
        return 0
    try:
        text = msg_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    if any(line.lower().lstrip().startswith("ledger-id:") for line in text.splitlines()):
        return 0  # amend of a trailed commit, or an already-trailed message
    trailed = append_ledger_trailer(text, pending)
    if trailed == text:
        return 0
    try:
        write_text_lf(msg_file, trailed)
    except OSError:
        console.print("[red]codeledger:[/] could not append the Ledger-ID trailer")
        return 1
    console.print(
        f"[dim]codeledger:[/] marking commit for {', '.join(pending)} "
        f"(Ledger-ID trailer added)"
    )
    return 0


def _hook_post_commit(root: Path) -> int:
    info = head_commit(root)
    if info is None:
        return 0
    ids = info.trailer_ids
    if not ids:
        return 0
    linked = store.commit_entries(root, ids, info.sha)
    if not linked:
        return 0
    for entry in linked:
        console.print(f"[dim]codeledger:[/] linked {entry.id} to commit {info.short}")
    # Auto-refresh: the capsule must reflect the latest committed state
    # (silent, best-effort - a hiccup here must never fail the commit).
    refresh_capsule(root)
    return 0


def _hook_pre_commit(root: Path) -> int:
    outcome = gate.run_gate(root, skip=gate.bypass_requested())
    for line in gate.report_lines(outcome):
        console.print(line)
    return outcome.exit_code
