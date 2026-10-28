"""``ledger blame <file:line>`` - the hero feature.

Given ``cart.py:24``, run ``git blame --porcelain`` for that line, follow the
commit, read its ``Ledger-ID`` trailer, and load the matching ledger entries
so we can show exactly which prompt/model/tool produced that line.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from codeledger import store
from codeledger.errors import LedgerError
from codeledger.gitutil import (
    BlameBlock,
    CommitInfo,
    GitError,
    blame_file,
    commit_info,
    ensure_git_repo,
)

__all__ = ["BlameReveal", "parse_ref", "reveal", "render_reveal", "reveal_to_dict"]


@dataclass
class BlameReveal:
    """The full story of one source line (AI-authored or human)."""

    file: str
    line: int
    text: str
    commit: Optional[CommitInfo]
    entries: List[store.Entry] = field(default_factory=list)
    uncommitted: bool = False
    human: bool = False

    @property
    def is_ai(self) -> bool:
        return bool(self.entries) and not self.uncommitted


def parse_ref(ref: str) -> "tuple[str, int]":
    """Split ``path/to/file.py:42`` into (path, line). Raises LedgerError.

    ```
    >>> parse_ref("cart.py:24")
    ('cart.py', 24)
    ```
    """
    if ":" not in ref:
        raise LedgerError(
            f"'{ref}' is not a file:line reference.\n"
            "Use the form  ledger blame cart.py:42",
            exit_code=2,
        )
    file, _, line_part = ref.rpartition(":")
    line_part = line_part.strip()
    if not file or not line_part.isdigit():
        raise LedgerError(
            f"'{ref}' is not a file:line reference.\n"
            "Use the form  ledger blame cart.py:42",
            exit_code=2,
        )
    line = int(line_part)
    if line < 1:
        raise LedgerError("Line numbers start at 1 (e.g. cart.py:1).", exit_code=2)
    return file.replace("\\", "/"), line


def reveal(cwd: Path, ref: str) -> BlameReveal:
    """Resolve a ``file:line`` reference to its full ledger story."""
    file, line = parse_ref(ref)
    root = ensure_git_repo(cwd)
    target = root / file
    if not target.is_file():
        raise LedgerError(
            f"'{file}' does not exist in this repository.\n"
            "Use a repo-relative path, e.g.  ledger blame src/cart.py:42",
            exit_code=1,
        )
    try:
        blocks = blame_file(root, file)
    except GitError as exc:
        raise LedgerError(
            f"git blame failed for '{file}': {exc}\n"
            "Hint: the file must be tracked in git to be blamed."
        ) from exc
    block: Optional[BlameBlock] = blocks.get(line)
    if block is None:
        line_count = max(blocks, default=0) or _count_lines(target)
        raise LedgerError(
            f"'{file}' has {line_count} line(s) - line {line} is out of range.",
            exit_code=1,
        )
    if block.uncommitted:
        return BlameReveal(
            file=file,
            line=line,
            text=block.text,
            commit=None,
            entries=[],
            uncommitted=True,
            human=False,
        )
    info = commit_info(root, block.sha)
    entry_ids = info.trailer_ids
    entries = store.find_entries(root, entry_ids) if entry_ids else []
    return BlameReveal(
        file=file,
        line=line,
        text=block.text,
        commit=info,
        entries=entries,
        uncommitted=False,
        human=not entries,
    )


def _count_lines(path: Path) -> int:
    """Number of text lines in a file (0 for an empty file)."""
    try:
        return sum(1 for _ in path.open("r", encoding="utf-8", errors="replace"))
    except OSError:
        return 0


def reveal_to_dict(r: BlameReveal) -> Dict[str, Any]:
    """JSON-friendly shape used by ``--json`` and the web API."""
    return {
        "file": r.file,
        "line": r.line,
        "text": r.text,
        "uncommitted": r.uncommitted,
        "human": r.human,
        "is_ai": r.is_ai,
        "commit": (
            {
                "sha": r.commit.sha,
                "short": r.commit.short,
                "author": r.commit.author,
                "author_email": r.commit.author_email,
                "date": r.commit.date,
                "message": r.commit.message,
            }
            if r.commit
            else None
        ),
        "entries": [e.to_dict() for e in r.entries],
    }


def json_of(reveal_result: BlameReveal) -> str:
    """Pretty JSON text for ``ledger blame --json``."""
    return json.dumps(reveal_to_dict(reveal_result), indent=2, ensure_ascii=False)


def render_reveal(console: Any, r: BlameReveal) -> None:
    """Print the hero panel for one line (rich), used by the CLI.

    Styles stay calm/paper-and-ink: no neon, no glow.
    """
    from rich.box import ROUNDED
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    title = Text(f"{r.file}:{r.line}", style="bold")
    if r.uncommitted:
        body = Text(
            "Uncommitted change - this line is not in any commit yet.\n"
            "Commit it, then 'ledger log' links the AI story to it.",
            style="yellow",
        )
        console.print(
            Panel(body, title=title, subtitle="uncommitted", box=ROUNDED, padding=(1, 2))
        )
        return

    grid = Table.grid(padding=(0, 2))
    if r.commit is not None:
        summary = r.commit.message.splitlines()[0] if r.commit.message else ""
        grid.add_row(Text("commit", style="bold slate_blue1"), Text(f"{r.commit.short}  {summary}"))
        grid.add_row(
            Text("author", style="bold slate_blue1"),
            Text(f"{r.commit.author} <{r.commit.author_email}>  {r.commit.date}"),
        )
    if r.is_ai:
        for entry in r.entries:
            grid.add_row(
                Text("entry", style="bold cyan"),
                Text(
                    f"{entry.id}  |  model {entry.model}  |  via {entry.tool}  |  "
                    f"{entry.status}  |  gate {entry.gate_result}  |  "
                    f"tests {'included' if entry.tests_included else 'missing'}"
                ),
            )
            grid.add_row("", Text(entry.decision, style="bold"))
            grid.add_row("", Text("prompt: ", style="dim").append_text(Text(entry.prompt, style="italic")))
            grid.add_row("", Text("files: ", style="dim").append_text(Text(", ".join(entry.files) or "-", style="")))
            for alt in entry.rejected_alternatives:
                approach = alt.get("approach", "") if isinstance(alt, dict) else str(alt)
                reason = alt.get("reason", "") if isinstance(alt, dict) else ""
                grid.add_row(
                    "",
                    Text("rejected: ", style="dim").append_text(
                        Text(f"{approach} - {reason}", style="dim")
                    ),
                )
    if r.human:
        grid.add_row(
            Text("note", style="bold"),
            Text("No AI record: human-written or unlogged commit.", style="bold"),
        )
    console.print(
        Panel(
            grid,
            title=title,
            subtitle="ai-written" if r.is_ai else "human",
            box=ROUNDED,
            padding=(1, 2),
        )
    )

