"""The CodeLedger store: ``.ledger/decisions.jsonl`` append-only ledger.

One JSON object per line.  Writes are atomic (temp file + rename).  Reading
tolerates a missing file, an empty file and corrupt lines (skipped with a
warning, never a crash) so ordinary accidents cannot destroy the ledger.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from codeledger.errors import LedgerError
from codeledger.util import err_console, now_iso, write_text_atomic

__all__ = [
    "LEDGER_DIR",
    "ENTRIES_FILE",
    "CAPSULE_FILE",
    "REDACT_MAP_FILE",
    "ENTRY_RE",
    "Entry",
    "ledger_root",
    "discover_ledger_root",
    "load_entries",
    "save_entries",
    "add_entry",
    "commit_entries",
    "next_entry_id",
]

#: Folder where all ledger data lives, relative to the repo root.
LEDGER_DIR = ".ledger"
#: The decision log itself.
ENTRIES_FILE = "decisions.jsonl"
#: Generated context capsule.
CAPSULE_FILE = "CAPSULE.md"
#: Reverse map for redaction placeholders (must stay gitignored).
REDACT_MAP_FILE = "redact_map.json"

ENTRY_RE = re.compile(r"^L-(\d+)$")

#: Order of keys in a stored entry; keeps the JSONL file diff-friendly.
_ENTRY_KEY_ORDER = (
    "id",
    "timestamp",
    "prompt",
    "model",
    "tool",
    "files",
    "decision",
    "rejected_alternatives",
    "status",
    "commit",
    "tests_included",
    "gate_result",
    "redactions",
)


@dataclass
class Entry:
    """One recorded AI change (a single line of ``decisions.jsonl``)."""

    id: str
    timestamp: str
    prompt: str
    model: str
    tool: str
    files: List[str] = field(default_factory=list)
    decision: str = ""
    rejected_alternatives: List[Dict[str, str]] = field(default_factory=list)
    status: str = "pending"  # "pending" | "committed"
    commit: Optional[str] = None  # sha once committed, else None
    tests_included: bool = False
    gate_result: str = "n/a"  # "pass" | "fail" | "bypassed" | "n/a"
    redactions: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        """Ordered dict for JSONL storage (unknown keys preserved at end)."""
        out: Dict[str, Any] = {}
        for key in _ENTRY_KEY_ORDER:
            if key == "rejected_alternatives":
                rejected: List[Dict[str, str]] = []
                for item in self.rejected_alternatives:
                    if isinstance(item, dict):
                        rejected.append(
                            {
                                "approach": str(item.get("approach", "")),
                                "reason": str(item.get("reason", "")),
                            }
                        )
                out[key] = rejected
            else:
                out[key] = _clean(getattr(self, key))
        for key in sorted(self.extra):
            out[key] = self.extra[key]
        return out

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Entry":
        """Tolerant reader: unknown keys kept, wrong types coerced/sanitised."""
        kwargs: Dict[str, Any] = {}
        extra: Dict[str, Any] = {}
        for key, value in raw.items():
            if key in _ENTRY_KEY_ORDER:
                kwargs[key] = value
            else:
                extra[key] = value
        files = kwargs.get("files") or []
        if not isinstance(files, list):
            files = []
        rejected = kwargs.get("rejected_alternatives") or []
        if not isinstance(rejected, list):
            rejected = []
        commit = kwargs.get("commit")
        entry = cls(
            id=str(kwargs.get("id") or ""),
            timestamp=str(kwargs.get("timestamp") or ""),
            prompt=str(kwargs.get("prompt") or ""),
            model=str(kwargs.get("model") or "unknown"),
            tool=str(kwargs.get("tool") or "unknown"),
            files=[str(f) for f in files],
            decision=str(kwargs.get("decision") or ""),
            rejected_alternatives=[dict(r) for r in rejected if isinstance(r, dict)],
            status=str(kwargs.get("status") or "pending"),
            commit=str(commit) if commit else None,
            tests_included=bool(kwargs.get("tests_included", False)),
            gate_result=str(kwargs.get("gate_result") or "n/a"),
            redactions=int(kwargs.get("redactions") or 0),
            extra=extra,
        )
        if entry.status not in ("pending", "committed"):
            entry.status = "pending"
        if entry.gate_result not in ("pass", "fail", "bypassed", "n/a"):
            entry.gate_result = "n/a"
        return entry

    # -- helpers ----------------------------------------------------------

    @property
    def number(self) -> int:
        """Numeric id (e.g. 12 for ``L-0012``); 0 if malformed."""
        match = ENTRY_RE.match(self.id)
        return int(match.group(1)) if match else 0


def _clean(value: Any) -> Any:
    """Normalise a value for JSON (None-safe booleans/ints, plain lists)."""
    if value is None:
        return None
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return str(value)


# ---------------------------------------------------------------------------
# Path discovery
# ---------------------------------------------------------------------------


def ledger_root(repo: Path) -> Path:
    """Return ``<repo>/.ledger`` (the folder must exist for file helpers)."""
    return repo / LEDGER_DIR


def discover_ledger_root(start: Path) -> Optional[Path]:
    """Find ``.ledger`` at ``start`` (codes expect you to run inside the repo).

    We deliberately do not search parent directories: the ledger belongs to
    the repo you are standing in, and guessing would write data to the wrong
    project.
    """
    candidate = start / LEDGER_DIR
    if candidate.is_dir():
        return candidate
    return None


def repository_for(start: Path) -> Path:
    """The repo this command writes into (``start`` itself, verified later)."""
    return start


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------


def entries_path(repo: Path) -> Path:
    """Full path of ``decisions.jsonl`` inside ``repo``."""
    return ledger_root(repo) / ENTRIES_FILE


def _warn(msg: str) -> None:
    err_console.print(f"[yellow]codeledger warning:[/yellow] {msg}")


def load_entries(repo: Path) -> List[Entry]:
    """Load every entry in the ledger, skipping corrupt lines with a warning.

    Guaranteed never to raise on file problems: a missing/empty file yields
    an empty list, a corrupt line is skipped once with a warning.
    """
    path = entries_path(repo)
    if not path.is_file():
        return []
    entries: List[Entry] = []
    bad = 0
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, ValueError) as exc:  # unreadable -> treat as empty
        _warn(f"could not read {path}: {exc}")
        return []
    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip("\ufeff")
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            bad += 1
            continue
        if not isinstance(raw, dict) or not ENTRY_RE.match(str(raw.get("id", ""))):
            bad += 1
            continue
        entries.append(Entry.from_dict(raw))
    if bad:
        _warn(f"skipped {bad} corrupt line(s) in {ENTRIES_FILE}")
    return entries


def save_entries(repo: Path, entries: List[Entry]) -> None:
    """Atomically rewrite the whole ledger file, sorted by entry id."""
    path = entries_path(repo)
    ordered = sorted(entries, key=lambda e: e.number)
    lines = [json.dumps(e.to_dict(), ensure_ascii=False) for e in ordered]
    text = "\n".join(lines) + ("\n" if lines else "")
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, text)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def next_entry_id(entries: List[Entry]) -> str:
    """Next free id: max existing number + 1, formatted ``L-0001`` style."""
    highest = max((e.number for e in entries), default=0)
    return f"L-{highest + 1:04d}"


def add_entry(
    repo: Path,
    *,
    prompt: str,
    model: str,
    tool: str,
    files: List[str],
    decision: str,
    rejected_alternatives: Optional[List[Dict[str, str]]] = None,
    timestamp: Optional[str] = None,
    tests_included: Optional[bool] = None,
) -> Entry:
    """Create a pending entry with the next auto-incremented id.

    ``tests_included`` defaults to an automatic detection based on the entry's
    file list.
    """
    from codeledger.gate import entry_has_tests  # late import avoids a cycle

    entries = load_entries(repo)
    entry = Entry(
        id=next_entry_id(entries),
        timestamp=timestamp or now_iso(),
        prompt=prompt,
        model=model,
        tool=tool,
        files=files,
        decision=decision,
        rejected_alternatives=rejected_alternatives or [],
        status="pending",
        commit=None,
        tests_included=(
            entry_has_tests(files) if tests_included is None else bool(tests_included)
        ),
        gate_result="n/a",
    )
    entries.append(entry)
    save_entries(repo, entries)
    return entry


def update_entry(repo: Path, entry_id: str, **changes: Any) -> Optional[Entry]:
    """Patch one entry and atomically rewrite the ledger. Returns the entry."""
    entries = load_entries(repo)
    found: Optional[Entry] = None
    for entry in entries:
        if entry.id == entry_id:
            found = entry
            break
    if found is None:
        return None
    for key, value in changes.items():
        if hasattr(found, key):
            setattr(found, key, value)
        else:
            found.extra[key] = value
    save_entries(repo, entries)
    return found


def commit_entries(repo: Path, entry_ids: List[str], sha: str) -> List[Entry]:
    """Mark the given pending entries as committed with ``sha``.

    Idempotent: already-committed entries keep their original commit unless it
    differs (an amended commit updates the sha).
    """
    wanted = set(entry_ids)
    entries = load_entries(repo)
    changed: List[Entry] = []
    for entry in entries:
        if entry.id in wanted:
            if entry.commit != sha or entry.status != "committed":
                entry.status = "committed"
                entry.commit = sha
                changed.append(entry)
    if changed:
        save_entries(repo, entries)
    return changed


def find_entries(repo: Path, entry_ids: List[str]) -> List[Entry]:
    """Resolve entry ids to entries; missing ids are reported via stderr."""
    wanted = list(dict.fromkeys(entry_ids))
    entries = {e.id: e for e in load_entries(repo)}
    out: List[Entry] = []
    for entry_id in wanted:
        entry = entries.get(entry_id)
        if entry is None:
            _warn(f"ledger entry {entry_id} not found in {ENTRIES_FILE}")
        else:
            out.append(entry)
    return out


def pending_entries(entries: List[Entry]) -> List[Entry]:
    """All entries that have not been linked to a commit yet."""
    return [e for e in entries if e.status == "pending"]


#: Maps a stored ``gate_result`` to the aggregate counter it feeds.
GateMap = {
    "pass": "gate_pass",
    "fail": "gate_fail",
    "bypassed": "gate_bypassed",
    "n/a": "gate_na",
}


def counts(entries: List[Entry]) -> Dict[str, int]:
    """Aggregate statistics used by ``status`` and the Health screen."""
    result: Dict[str, int] = {
        "total": len(entries),
        "pending": 0,
        "committed": 0,
        "tests_included": 0,
        "gate_pass": 0,
        "gate_fail": 0,
        "gate_bypassed": 0,
        "gate_na": 0,
        "redactions": 0,
    }
    for entry in entries:
        if entry.status == "pending":
            result["pending"] += 1
        else:
            result["committed"] += 1
        if entry.tests_included:
            result["tests_included"] += 1
        key = GateMap.get(entry.gate_result)
        if key:
            result[key] += 1
        result["redactions"] += entry.redactions
    return result
