"""``ledger gate`` - block AI source changes that ship without tests.

The gate is the policy engine: if *pending* ledger entries exist and the
staged changes contain source files belonging to those entries but no test
files, the gate fails with a clear explanation plus a ready-to-paste prompt
for an AI to write tests.

Files that are clearly not code (docs, config, lockfiles) are never blocked.
``LEDGER_SKIP_GATE=1`` bypasses the check and records "bypassed" on the
pending entries.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from codeledger import store
from codeledger.errors import LedgerError
from codeledger.gitutil import ensure_git_repo, staged_files

__all__ = [
    "GateOutcome",
    "run_gate",
    "is_test_file",
    "is_doc_or_config",
    "classify_path",
    "entry_has_tests",
    "test_fingerprint",
    "BYPASS_ENV",
    "bypass_requested",
]

# ---------------------------------------------------------------------------
# File classification
# ---------------------------------------------------------------------------

#: Suffixes treated as docs/config/lockfiles - never blocked by the gate.
SKIPPED_SUFFIXES = {
    ".md", ".rst", ".txt", ".adoc", ".toml", ".cfg", ".ini",
    ".yaml", ".yml", ".lock", ".json",
}

#: Exact basenames never blocked by the gate (docs/config/lockfile staples).
SKIPPED_BASENAMES = {
    ".gitignore", ".gitattributes", ".editorconfig", ".flake8",
    ".pre-commit-config.yaml", ".env.example",
    "license", "notice", "dockerfile", "makefile",
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "requirements.txt",
}

#: Directory names treated as meta/docs directories.
SKIPPED_DIRS = {"docs", ".github", ".vscode", ".idea"}

#: Directory names that mark a path as a test file.
TEST_DIRS = {"tests", "test", "__tests__"}


def is_test_file(path: str) -> bool:
    """Detect the test-file naming conventions from the spec.

    ``tests/`` or ``test/`` folders, ``test_*.py``, ``*_test.py``,
    ``*.test.js/.ts/.jsx/.tsx``, ``*.spec.*``, ``__tests__/`` - plus
    ``conftest.py`` (pytest's own fixture module).
    """
    posix = path.replace("\\", "/").lower()
    parts = posix.split("/")
    name = parts[-1]
    if any(part in TEST_DIRS for part in parts[:-1]):
        return True
    stem, dot, ext = name.rpartition(".")
    if dot and "." + ext in SKIPPED_SUFFIXES:
        return False  # notes.spec.txt is a document, never a gateable test
    if name == "conftest.py":
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py"):
        return True
    for ext in (".test.js", ".test.ts", ".test.jsx", ".test.tsx"):
        if name.endswith(ext):
            return True
    if re.search(r"\.spec\.[a-z0-9]+$", name) or name.endswith(".spec"):
        return True
    return False


def is_doc_or_config(path: str) -> bool:
    """Docs, config, lockfiles and dotfiles: changes should never be blocked."""
    posix = path.replace("\\", "/")
    if posix == ".ledger" or posix.startswith(".ledger/"):
        return True  # CodeLedger's own bookkeeping: never "the AI source change"
    lowered = posix.lower()
    parts = lowered.split("/")
    name = parts[-1]
    stem, dot, ext = name.rpartition(".")
    if any(part in SKIPPED_DIRS for part in parts[:-1]):
        return True
    if dot and "." + ext in SKIPPED_SUFFIXES:
        return True
    if name in SKIPPED_BASENAMES or (dot and stem in SKIPPED_BASENAMES):
        return True
    return False


def classify_path(path: str) -> str:
    """Bucket a repo path: ``test`` | ``docs`` | ``source``."""
    if is_test_file(path):
        return "test"
    if is_doc_or_config(path):
        return "docs"
    return "source"


def entry_has_tests(files: List[str]) -> bool:
    """True when an entry's file list contains at least one test file."""
    return any(classify_path(f) == "test" for f in files)


def entry_files_of_kind(files: List[str]) -> Dict[str, List[str]]:
    """Split an entry's files into test / docs / source buckets."""
    buckets: Dict[str, List[str]] = {"test": [], "docs": [], "source": []}
    for f in files:
        buckets[classify_path(f)].append(f)
    return buckets


def test_fingerprint(sources: List[str]) -> List[str]:
    """Suggested pytest file names for given source files (stable order)."""
    out: List[str] = []
    for src in sources:
        name = Path(src.replace("\\", "/")).name
        stem = Path(name).stem or name
        out.append(f"tests/test_{stem}.py")
    return out


# ---------------------------------------------------------------------------
# Gate check
# ---------------------------------------------------------------------------

BYPASS_ENV = "LEDGER_SKIP_GATE"


def bypass_requested() -> bool:
    """True when ``LEDGER_SKIP_GATE=1`` (or true/yes/on, case-insensitive)."""
    return os.environ.get(BYPASS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class GateOutcome:
    """Result of one gate check (shared by CLI, hook and UI)."""

    blocked: bool
    bypassed: bool
    pending_ids: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)
    tests: List[str] = field(default_factory=list)
    reason: str = ""
    test_prompt: str = ""

    @property
    def exit_code(self) -> int:
        return 1 if self.blocked else 0


def _build_test_prompt(sources: List[str]) -> str:
    """The ready-to-paste prompt printed inside a failed gate."""
    listing = "\n".join(f"  - {s}" for s in sources)
    suggestions = ", ".join(test_fingerprint(sources))
    return (
        "Write pytest tests for the staged changes to these exact files:\n"
        f"{listing}\n"
        "Create the tests under the tests/ directory (for example: "
        f"{suggestions}), run pytest locally, and `git add` the new/updated "
        "test files together with the source changes. Change nothing else."
    )


def _mark_pending(root: Path, pending: List[store.Entry], gate_result: str) -> None:
    """Persist ``gate_result`` onto pending entries (committed ones untouched)."""
    ids = {e.id for e in pending}
    entries = store.load_entries(root)
    for entry in entries:
        if entry.id in ids:
            entry.gate_result = gate_result
    store.save_entries(root, entries)


def _bump_tests_flags(root: Path, pending: List[store.Entry], staged: List[str]) -> None:
    """When tests ride along with an entry's source files, flag tests_included."""
    ids = {e.id for e in pending}
    staged_tests = {f for f in staged if classify_path(f) == "test"}
    if not staged_tests:
        return
    entries = store.load_entries(root)
    for entry in entries:
        if entry.id not in ids:
            continue
        ships_source = any(classify_path(f) == "source" for f in entry.files)
        if ships_source and not entry.tests_included:
            entry.tests_included = True
    store.save_entries(root, entries)


def run_gate(repo: Path, *, skip: bool, record: bool = True) -> GateOutcome:
    """Run the gate. Mutates the ledger to record gate results unless disabled.

    Returns the outcome; the caller maps it to an exit code / hook behaviour.
    """
    root = ensure_git_repo(repo)
    ledger = store.discover_ledger_root(root)
    if ledger is None:
        raise LedgerError(
            "No CodeLedger repository found here.\n"
            "Run 'ledger init' to create the .ledger/ data directory."
        )
    entries = store.load_entries(root)
    pending = store.pending_entries(entries)
    outcome = GateOutcome(
        blocked=False, bypassed=False, pending_ids=[e.id for e in pending]
    )

    if not pending:
        outcome.reason = "No pending ledger entries - nothing to gate."
        return outcome

    staged = staged_files(root)
    pending_files = {f for e in pending for f in e.files}
    srcs = [f for f in staged if classify_path(f) == "source" and f in pending_files]
    # Test files count even when the pending entry didn't list them: the
    # assistant typically creates NEW test files it never listed at log time.
    tests = [f for f in staged if classify_path(f) == "test"]
    outcome.sources = sorted(srcs)
    outcome.tests = sorted(tests)

    if skip:
        outcome.bypassed = True
        outcome.reason = "LEDGER_SKIP_GATE is set: gate bypassed, entries recorded as 'bypassed'."
        if record:
            _mark_pending(root, pending, gate_result="bypassed")
        return outcome

    if not staged:
        outcome.reason = "Nothing staged yet - the gate inspects the changes about to be committed."
        return outcome

    if srcs and not tests:
        outcome.blocked = True
        outcome.reason = (
            f"{len(srcs)} source file(s) touched by pending AI entries are staged, "
            "but no test files. The gate blocks AI source changes without tests."
        )
        outcome.test_prompt = _build_test_prompt(outcome.sources)
        if record:
            _mark_pending(root, pending, gate_result="fail")
        return outcome

    # Gate passes: flag tests_included when tests ride along with source.
    if record:
        _bump_tests_flags(root, pending, staged=staged)
        _mark_pending(root, pending, gate_result="pass")
    if srcs:
        outcome.reason = (
            f"Gate passed: {len(srcs)} source file(s) staged with "
            f"{len(tests)} test file(s)."
        )
    else:
        outcome.reason = (
            "Gate passed: staged changes are docs/config/test-only (not blocked)."
        )
    return outcome


# ---------------------------------------------------------------------------
# Shared reporting (CLI `gate` command and the pre-commit hook print the same)
# ---------------------------------------------------------------------------


def report_lines(outcome: GateOutcome) -> List[str]:
    """Human-readable, plain-text lines describing the gate outcome.

    Used verbatim by the pre-commit hook (inside `git commit` output) and by
    the `ledger gate` command, so both surfaces ever tell one story.
    """
    lines: List[str] = []
    if outcome.blocked:
        lines.append("ledger gate: FAILED (commit blocked)")
        lines.append("")
        lines.append(outcome.reason)
        lines.append("")
        lines.append("Paste this prompt into your AI assistant to fix it now:")
        lines.append("")
        for number, p_line in enumerate(outcome.test_prompt.splitlines(), start=1):
            lines.append(f"  {p_line}")
        lines.append("")
        lines.append(
            "Then commit again (or run `ledger gate` to re-check). Bypass "
            "deliberately with LEDGER_SKIP_GATE=1 (recorded as 'bypassed')."
        )
        return lines
    if outcome.bypassed:
        return [f"ledger gate: bypassed - {outcome.reason}"]
    return [f"ledger gate: passed - {outcome.reason}"]
