"""``ledger init`` - bootstrap CodeLedger inside a git repository.

Creates ``.ledger/`` (decisions.jsonl), adds a gitignore entry for the
redaction map, installs three git hooks (prepare-commit-msg, pre-commit,
post-commit) without clobbering existing hooks, and writes a marked
CodeLedger section into AGENTS.md.  Everything is idempotent.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from codeledger import store
from codeledger.errors import LedgerError
from codeledger.gitutil import GitError, ensure_git_repo, git_available
from codeledger.util import write_text_lf

__all__ = ["run_init", "InitReport", "agents_section"]

HOOK_NAMES = ("prepare-commit-msg", "pre-commit", "post-commit")

_HOOK_BLOCK_TEMPLATE = """\
# >>> BEGIN CODELEDGER (installed by `ledger init`; managed block) >>>
# CodeLedger: link commits to AI decisions. Prefer the `ledger` CLI,
# fall back to the equivalent `python -m codeledger` invocation.
if command -v ledger >/dev/null 2>&1; then
    exec ledger hook {hook} "$@"
elif command -v python >/dev/null 2>&1; then
    exec python -m codeledger hook {hook} "$@"
else
    echo "codeledger: neither 'ledger' nor 'python' found on PATH; skipping." >&2
    exit 0
fi
# <<< END CODELEDGER <<<
"""


def _hook_block(hook: str) -> str:
    """The managed block appended into (or starting) a hook script."""
    return _HOOK_BLOCK_TEMPLATE.format(hook=hook)


def _hook_script(hook: str) -> str:
    """Full contents of a fresh hook file (shebang + managed block)."""
    return "#!/bin/sh\n" + _hook_block(hook)


@dataclass
class InitReport:
    """What init created/updated/skipped, so the CLI can print a summary."""

    repo: str
    created_ledger: bool
    created_jsonl: bool
    gitignore_updated: bool
    hooks: dict = field(default_factory=dict)  # hook -> created|appended|skipped
    agents_updated: bool = False
    agents_created: bool = False


# ---------------------------------------------------------------------------
# .gitignore
# ---------------------------------------------------------------------------

GITIGNORE_MARKER = "# CodeLedger"
GITIGNORE_ENTRY = ".ledger/redact_map.json"


def _ensure_gitignore(repo: Path) -> bool:
    """Append the redact-map ignore entry; True when the file changed."""
    path = repo / ".gitignore"
    existing = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    if any(line.strip().startswith(GITIGNORE_ENTRY) for line in existing.splitlines()):
        return False
    lines = [line for line in existing.splitlines()]
    lines += [GITIGNORE_MARKER, GITIGNORE_ENTRY]
    write_text_lf(path, "\n".join(lines) + "\n")
    return True


# ---------------------------------------------------------------------------
# AGENTS.md
# ---------------------------------------------------------------------------

AGENTS_BEGIN = "<!-- codeledger:start -->"
AGENTS_END = "<!-- codeledger:end -->"

AGENTS_SECTION = f"""{AGENTS_BEGIN}
## CodeLedger - AI decision log (managed section)

This repository uses CodeLedger ("git blame for AI-written code").

1. At the **start of every session**, read `.ledger/CAPSULE.md` for context.
   If it does not exist, run `ledger capsule` to generate it.
2. After **every code change** you make, run `ledger log` and record the
   prompt/model/tool, the files touched, the decision, and every approach
   you considered and rejected. Non-interactive form:
   `ledger log --prompt "..." --model "..." --tool "..." --files a.py
   --decision "..." --rejected "approach::reason"`.
3. **Never repeat an approach listed under "Don't do this again"** in the
   capsule unless an entry explains why the situation has changed.
4. Ship tests together with source changes: the pre-commit hook runs
   `ledger gate` and blocks AI source changes that have no tests. Bypass
   only deliberately with `LEDGER_SKIP_GATE=1` (recorded as "bypassed").
5. The raw log is `.ledger/decisions.jsonl` - one JSON object per line.
{AGENTS_END}"""


def _update_agents_md(repo: Path) -> bool:
    """Insert/replace the marked CodeLedger section; True when changed."""
    path = repo / "AGENTS.md"
    existed = path.is_file()
    existing = path.read_text(encoding="utf-8", errors="replace") if existed else ""
    begin_idx = existing.find(AGENTS_BEGIN)
    end_idx = existing.find(AGENTS_END)
    if begin_idx != -1 and end_idx != -1 and end_idx > begin_idx:
        new_text = (
            existing[:begin_idx]
            + AGENTS_SECTION
            + existing[end_idx + len(AGENTS_END):]
        )
    else:
        # Dangling or missing markers: rebuild cleanly from the prose part.
        base = existing
        if end_idx != -1 and begin_idx == -1:
            base = existing[end_idx + len(AGENTS_END):]
        if begin_idx != -1:
            base = existing[:begin_idx]
        if base.strip():
            new_text = base.rstrip("\n") + "\n\n" + AGENTS_SECTION + "\n"
        else:
            new_text = AGENTS_SECTION + "\n"
    if new_text == existing:
        return False
    write_text_lf(path, new_text)
    return True


def agents_section() -> str:
    """Expose the managed AGENTS.md section (used by tests)."""
    return AGENTS_SECTION


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


def _install_hook(repo: Path, hook: str) -> str:
    """Install ``hook``; returns created|appended|skipped.

    Never overwrites an existing hook: when a hook already exists, the
    managed block is appended between BEGIN/END markers.  Calling init twice
    is a no-op (markers already present).
    """
    hooks_dir = repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    path = hooks_dir / hook
    if path.is_file():
        content = path.read_text(encoding="utf-8", errors="replace")
        if "BEGIN CODELEDGER" in content:
            return "skipped"
        if not content.lstrip().startswith("#!"):
            raise LedgerError(
                f"hooks/{hook} exists but has no '#!/bin/sh'-style shebang, so\n"
                "CodeLedger cannot safely append its managed block. Add a\n"
                "shebang line or remove the hook, then re-run 'ledger init'."
            )
        sep = "" if content.endswith("\n") else "\n"
        write_text_lf(path, content + sep + "\n" + _hook_block(hook))
        action = "appended"
    else:
        write_text_lf(path, _hook_script(hook))
        action = "created"
    _make_executable(path)
    return action


def _make_executable(path: Path) -> None:
    """Best-effort chmod +x (a no-op effect on Windows, respected on unix)."""
    try:
        path.chmod(0o755)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_init(cwd: Path) -> InitReport:
    """Bootstrap everything. Idempotent: re-running changes nothing."""
    if not git_available():
        raise LedgerError(
            "git is not installed or not on PATH.\n"
            "CodeLedger is built on git blame, so git is required."
        )
    try:
        repo = ensure_git_repo(cwd)
    except GitError as exc:  # git present but repo broken
        raise LedgerError(str(exc)) from exc

    ledger_dir = repo / store.LEDGER_DIR
    created_ledger = not ledger_dir.is_dir()
    ledger_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = ledger_dir / store.ENTRIES_FILE
    created_jsonl = not jsonl_path.exists()
    if created_jsonl:
        write_text_lf(jsonl_path, "")

    gitignore_updated = _ensure_gitignore(repo)

    hooks: dict = {}
    for hook in HOOK_NAMES:
        hooks[hook] = _install_hook(repo, hook)

    agents_path = repo / "AGENTS.md"
    agents_created = not agents_path.is_file()
    agents_updated = _update_agents_md(repo)

    return InitReport(
        repo=str(repo),
        created_ledger=created_ledger,
        created_jsonl=created_jsonl,
        gitignore_updated=gitignore_updated,
        hooks=hooks,
        agents_updated=agents_updated,
        agents_created=agents_created,
    )
