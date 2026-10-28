"""CodeLedger command line interface (``ledger``, or ``python -m codeledger``).

Commands: init, log, blame, gate, capsule, redact, restore, status, ui,
demo - plus a hidden ``hook`` command used by the installed git hooks.
All output is friendly, coloured via rich, and free of debug traces.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

import typer
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from codeledger import capsule as capsule_mod
from codeledger import demo as demo_mod
from codeledger import gate as gate_mod
from codeledger import gitutil
from codeledger import hooks as hooks_mod
from codeledger import initcmd
from codeledger import redact as redact_mod
from codeledger import store
from codeledger import webui as webui_mod
from codeledger.blame import BlameReveal, render_reveal, reveal, reveal_to_dict
from codeledger.errors import LedgerError
from codeledger.util import console, err_console, write_text_lf

from codeledger import __version__

app = typer.Typer(
    name="ledger",
    help=(
        "CodeLedger - git blame for AI-written code.\n\n"
        "Records the prompt, model and tool behind every AI change in plain\n"
        "files inside .ledger/, so context outlives the chat window."
    ),
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)


def main() -> None:
    """Entry point (console-script and ``python -m codeledger``)."""
    try:
        app()
    except LedgerError as exc:
        err_console.print(f"codeledger: {exc}")
        # SystemExit (not typer.Exit): outside typer's dispatch nothing catches
        # typer.Exit, so it would print a raw traceback. SystemExit is clean.
        raise SystemExit(exc.exit_code)


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _require_repo(cwd: Path) -> Path:
    """Resolve the ledger repo root; friendly error when uninitialised."""
    root = gitutil.repo_root(cwd)
    if root is None:
        raise LedgerError(
            "This directory is not inside a git repository.\n"
            "CodeLedger needs git history: run 'git init', then 'ledger init'."
        )
    ledger = store.discover_ledger_root(root)
    if ledger is None:
        raise LedgerError(
            "No CodeLedger repository found here.\n"
            "Run 'ledger init' to create the .ledger/ data directory."
        )
    return root


def parse_rejected(items: List[str]) -> List[Dict[str, str]]:
    """Parse ``--rejected 'approach::reason'`` repeats into dicts."""
    out: List[Dict[str, str]] = []
    for item in items:
        if "::" not in item:
            raise LedgerError(
                f"bad --rejected value {item!r}: expected 'approach::reason'.",
                exit_code=2,
            )
        approach, _, reason = item.partition("::")
        out.append({"approach": approach.strip(), "reason": reason.strip()})
    return out


def _interactive() -> bool:
    """True when we may ask the user questions at the prompt."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except ValueError:  # closed streams in test runners
        return False


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"codeledger {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """CodeLedger keeps the 'why' behind AI-written code, in your repo."""


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@app.command("init")
def init_cmd() -> None:
    """Bootstrap CodeLedger: .ledger/, git hooks, .gitignore, AGENTS.md."""
    cwd = Path.cwd()
    try:
        report = initcmd.run_init(cwd)
    except LedgerError as exc:
        err_console.print(f"codeledger init: {exc}")
        raise typer.Exit(exc.exit_code)

    table = Table.grid(padding=(0, 2))
    table.add_column(style="dim")
    table.add_row(
        "repo",
        Text(
            report.repo,
            style="",
        ),
    )
    table.add_row(
        ".ledger/decisions.jsonl", Text("created" if report.created_jsonl else "kept")
    )
    table.add_row(
        ".gitignore",
        Text(
            "updated (redact_map.json ignored)"
            if report.gitignore_updated
            else "already covers .ledger/redact_map.json"
        ),
    )
    for hook_name in ("prepare-commit-msg", "pre-commit", "post-commit"):
        table.add_row(
            f".git/hooks/{hook_name}", Text(report.hooks.get(hook_name, "skipped"))
        )
    table.add_row(
        "AGENTS.md",
        Text(
            "section added"
            if report.agents_created
            else ("section refreshed" if report.agents_updated else "kept (already marked)")
        ),
    )
    console.print(Panel(table, title="CodeLedger initialised", padding=(1, 2)))
    console.print(
        "Next: after your next AI change, run [bold]ledger log[/bold]; then "
        "[bold]ledger capsule[/bold] to generate the context snapshot."
    )


# ---------------------------------------------------------------------------
# log
# ---------------------------------------------------------------------------

INTERACTIVE_ORDER = ("prompt", "model", "tool", "decision")


def _fill_missing_log_fields(fields: Dict[str, Optional[str]]) -> None:
    """Ask for log fields at the terminal when options were omitted."""
    if not _interactive():
        return
    if not fields.get("prompt"):
        fields["prompt"] = typer.prompt("Prompt (what did you ask the AI?)", default="")
    if not fields.get("model"):
        fields["model"] = typer.prompt("Model (e.g. claude-sonnet-4.5)", default="unknown")
    if not fields.get("tool"):
        fields["tool"] = typer.prompt("Tool (e.g. opencode, claude-code, cursor)", default="unknown")
    if not fields.get("decision"):
        fields["decision"] = typer.prompt("Decision in one line", default="")


@app.command("log")
def log_cmd(
    prompt: Optional[str] = typer.Option(None, "--prompt", "-p", help="The prompt/instruction that produced the change."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Model id, e.g. 'claude-sonnet-4.5'."),
    tool: Optional[str] = typer.Option(None, "--tool", "-t", help="Assistant/tool, e.g. opencode, claude-code, cursor."),
    files: Optional[List[str]] = typer.Option(None, "--files", "-f", metavar="FILE", help="Repo-relative file(s) changed (repeatable; default: files changed in git)."),
    decision: Optional[str] = typer.Option(None, "--decision", "-d", help="One-line summary of what was decided and why."),
    rejected: Optional[List[str]] = typer.Option(None, "--rejected", "-r", metavar="APPROACH::REASON", help="Rejected alternative 'approach::reason' (repeatable)."),
) -> None:
    """Record an AI change as a pending ledger entry (prints its new ID)."""
    repo = _require_repo(Path.cwd())
    fields: Dict[str, Optional[str]] = {
        "prompt": prompt,
        "model": model,
        "tool": tool,
        "decision": decision,
    }
    _fill_missing_log_fields(fields)
    missing = [k for k in ("prompt", "decision") if not (fields.get(k) or "").strip()]
    if missing:
        raise LedgerError(
            "ledger log needs --prompt/--model/--tool/--decision\n"
            "(or run it in an interactive terminal to be asked). "
            "Missing: " + ", ".join(f"--{m}" for m in missing),
            exit_code=2,
        )
    if rejected:
        rejected_items = parse_rejected(list(rejected))
    else:
        rejected_items = []
    if files:
        entry_files = [f.replace("\\", "/") for f in files]
    else:
        entry_files = gitutil.changed_files(repo)
        if not entry_files:
            console.print("[dim]codeledger:[/] no changed files found in git; recording an entry with an empty file list.")

    entry = store.add_entry(
        repo,
        prompt=fields["prompt"] or "",
        model=fields["model"] or "unknown",
        tool=fields["tool"] or "unknown",
        files=entry_files,
        decision=fields["decision"] or "",
        rejected_alternatives=rejected_items,
    )
    console.print(
        f"Recorded [bold]{entry.id}[/bold] ({entry.status}) - "
        "it links to your next commit via the Ledger-ID trailer."
    )
    # Auto-refresh: the capsule's Open items must show this entry immediately,
    # so the next AI session never reads a stale snapshot (silent, best-effort).
    capsule_mod.refresh_capsule(repo)
    if rejected_items:
        for item in rejected_items:
            console.print(
                f"  rejected: {item['approach']} - {item['reason']}", markup=False, style="dim"
            )


# ---------------------------------------------------------------------------
# hook (hidden; used by the git hooks installed by init)
# ---------------------------------------------------------------------------


@app.command("hook", hidden=True)
def hook_cmd(
    hook_name: str = typer.Argument(..., help="prepare-commit-msg | pre-commit | post-commit"),
    hook_args: Optional[List[str]] = typer.Argument(None),
) -> None:
    """(internal) Entry point used by the git hooks installed by `ledger init`."""
    code = hooks_mod.run_hook(hook_name, list(hook_args or []), Path.cwd())
    raise typer.Exit(code)


# ---------------------------------------------------------------------------
# blame
# ---------------------------------------------------------------------------


@app.command("blame")
def blame_cmd(
    ref: str = typer.Argument(..., metavar="FILE:LINE", help="Repo-relative path and 1-based line, e.g. cart.py:42."),
    json_out: bool = typer.Option(False, "--json", help="Print the result as JSON instead of a panel."),
) -> None:
    """Show which prompt/model/decision produced a given line (hero feature)."""
    outcome = reveal(Path.cwd(), ref)
    _blame_exit(outcome, json_out)


def _blame_exit(outcome: BlameReveal, json_out: bool) -> None:
    if json_out:
        sys.stdout.write(json.dumps(reveal_to_dict(outcome), indent=2, ensure_ascii=False) + "\n")
        return
    render_reveal(console, outcome)


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------


@app.command("gate")
def gate_cmd() -> None:
    """Check that staged AI changes include tests; explains how to fix it."""
    repo = _require_repo(Path.cwd())
    outcome = gate_mod.run_gate(repo, skip=gate_mod.bypass_requested())
    lines = gate_mod.report_lines(outcome)
    if outcome.blocked:
        console.print(Panel("\n".join(lines), title="ledger gate", border_style="red"))
    elif outcome.bypassed:
        console.print(Panel("\n".join(lines), title="ledger gate", border_style="yellow"))
    else:
        console.print(Panel("\n".join(lines), title="ledger gate", border_style="green"))
    raise typer.Exit(outcome.exit_code)


# ---------------------------------------------------------------------------
# capsule
# ---------------------------------------------------------------------------


@app.command("capsule")
def capsule_cmd(
    stdout: bool = typer.Option(False, "--stdout", help="Print the capsule instead of writing .ledger/CAPSULE.md."),
) -> None:
    """Generate .ledger/CAPSULE.md: the context snapshot for any AI tool."""
    repo = _require_repo(Path.cwd())
    entries = store.load_entries(repo)
    branch: Optional[str] = None
    try:
        branch = gitutil.run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo, check=False).strip()
        if branch in ("", "HEAD"):
            branch = None
    except LedgerError:
        branch = None
    text, path = capsule_mod.write_capsule(entries, repo, branch=branch)
    if stdout:
        sys.stdout.write(text)
        return
    line_count = len(text.splitlines())
    console.print(
        f"CAPSULE.md written ({line_count} lines) - [dim]{escape(str(path.relative_to(repo)))}[/dim]"
    )


# ---------------------------------------------------------------------------
# redact / restore
# ---------------------------------------------------------------------------


def _redact_ledger_dir(cwd: Path) -> Path:
    """Where the redaction map lives (repo root's .ledger, else cwd/.ledger)."""
    root = gitutil.repo_root(cwd)
    return (root or cwd) / ".ledger"


def _read_input_text(path: Optional[Path]) -> str:
    """Read redact input from a file or stdin (UTF-8, strict)."""
    if path is not None:
        try:
            # utf-8-sig: tolerate BOMs, which common Windows editors add.
            return path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            raise LedgerError(f"cannot read '{path}': {exc}") from exc
        except UnicodeDecodeError:
            raise LedgerError(f"'{path}' is not valid UTF-8 text; redact works on text files only.") from exc
    if sys.stdin is None or not hasattr(sys.stdin, "read"):
        raise LedgerError("no input: pass a file path or pipe text into ledger redact.")
    try:
        return sys.stdin.read()
    except UnicodeDecodeError as exc:
        raise LedgerError(f"stdin is not valid UTF-8 text: {exc}") from exc


def _write_stdout_safe(text: str) -> None:
    """Write text to stdout even when the console encoding is not UTF-8.

    Windows consoles default to cp1252; secret material may contain
    characters it cannot encode. Bytes matter, not display, so unencodable
    characters are replaced rather than crashing mid-output.
    """
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
        return
    except UnicodeEncodeError:
        pass
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    data = text.encode(enc, errors="replace")
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        try:
            buffer.write(data)
            buffer.flush()
            return
        except (OSError, ValueError):
            pass
    sys.stderr.write(f"codeledger: could not write redacted output ({enc})\n")


@app.command("redact")
def redact_cmd(
    path: Optional[Path] = typer.Argument(None, help="File to redact; omit to read stdin."),
    out: Optional[Path] = typer.Option(None, "--out", metavar="FILE", help="Write the redacted text here instead of stdout."),
    check: bool = typer.Option(False, "--check", help="Only report counts; exit 1 if anything would be redacted."),
) -> None:
    """Replace secrets with numbered placeholders; keeps a reverse map.

    With a FILE: the file is rewritten in place (round-trips with
    ``ledger restore``) and the redacted text is printed. With stdin: the
    redacted text goes to stdout (or ``--out``).
    """
    text = _read_input_text(path)
    if check:
        counts = redact_mod.scan_counts(text)
        _print_redact_counts(counts)
        if sum(counts.values()) > 0:
            raise typer.Exit(1)
        return
    ledger_dir = _redact_ledger_dir(Path.cwd())
    outcome = redact_mod.redact_text(text, ledger_dir)
    target_written = False
    if path is not None:
        try:
            write_text_lf(path, outcome.text, atomic=True)
        except OSError as exc:
            raise LedgerError(f"cannot write '{path}': {exc}") from exc
        target_written = True
    elif out is not None:
        try:
            write_text_lf(out, outcome.text)
        except OSError as exc:
            raise LedgerError(f"cannot write '{out}': {exc}") from exc
    else:
        _write_stdout_safe(outcome.text)
    _print_redact_counts(outcome.counts)
    console.print(f"[dim]codeledger:[/] reverse map updated ({outcome.map_added} new placeholder(s))")
    if target_written:
        console.print(f"[dim]codeledger:[/] redacted in place: {path} (restore with 'ledger restore')")
    _maybe_bump_redactions(outcome, path)


def _print_redact_counts(counts: Dict[str, int]) -> None:
    total = sum(counts.values())
    if total == 0:
        console.print("[green]No secrets detected.[/green]")
        return
    parts = ", ".join(f"{v} {k}" for k, v in counts.items() if v)
    console.print(f"Detected {total} item(s): {parts}")


def _maybe_bump_redactions(
    outcome: "redact_mod.RedactOutcome", path: Optional[Path]
) -> None:
    """Mirror the redaction count onto a pending ledger entry (nice touch)."""
    if outcome.total == 0 or path is None:
        return
    repo = gitutil.repo_root(Path.cwd())
    if repo is None:
        return
    try:
        rel = path.resolve().relative_to(repo).as_posix()
    except ValueError:
        return
    entries = store.load_entries(repo)
    selected = None
    for entry in reversed(store.pending_entries(entries)):
        if rel in entry.files:
            selected = entry
            break
    if selected is None:
        return
    store.update_entry(repo, selected.id, redactions=selected.redactions + outcome.total)
    console.print(f"[dim]codeledger:[/] recorded {outcome.total} redaction(s) on {selected.id}")


@app.command("restore")
def restore_cmd(
    path: Path = typer.Argument(..., metavar="FILE", help="File whose placeholders should be swapped back."),
) -> None:
    """Restore placeholders in a file using .ledger/redact_map.json."""
    if not path.is_file():
        raise LedgerError(f"file not found: {path}")
    ledger_dir = _redact_ledger_dir(Path.cwd())
    map_path = ledger_dir / store.REDACT_MAP_FILE
    text = path.read_text(encoding="utf-8-sig")
    new_text, replaced, unknown = redact_mod.restore_text(text, map_path)
    if replaced == 0 and unknown == 0:
        console.print("Nothing to restore - no placeholders found in this file.")
        return
    try:
        write_text_lf(path, new_text)
    except OSError as exc:
        raise LedgerError(f"cannot write '{path}': {exc}") from exc
    line = f"restored {replaced} placeholder(s) in {path}"
    if unknown:
        line += f" ({unknown} unknown placeholder(s) left untouched)"
    console.print(line)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@app.command("status")
def status_cmd() -> None:
    """Summary of the ledger: totals, pending entries and gate stats."""
    repo = _require_repo(Path.cwd())
    entries = store.load_entries(repo)
    counts = store.counts(entries)
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="dim")
    summary.add_column()
    summary.add_row("entries", f"{counts['total']} total, {counts['committed']} committed, {counts['pending']} pending")
    summary.add_row("tests", f"{counts['tests_included']}/{counts['total']} entries include tests")
    summary.add_row(
        "gate",
        f"pass {counts['gate_pass']} / fail {counts['gate_fail']} / "
        f"bypassed {counts['gate_bypassed']} / n/a {counts['gate_na']}",
    )
    summary.add_row("redactions", str(counts["redactions"]))
    console.print(Panel(summary, title="CodeLedger status", padding=(1, 2)))

    if not entries:
        console.print("No entries yet. Record your first AI change with [bold]ledger log[/bold].")
        return

    table = Table(box=None, header_style="dim")
    table.add_column("id", no_wrap=True)
    table.add_column("time", no_wrap=True)
    table.add_column("model", no_wrap=True)
    table.add_column("tool", no_wrap=True)
    table.add_column("status", no_wrap=True)
    table.add_column("gate", no_wrap=True)
    table.add_column("tests", no_wrap=True)
    table.add_column("decision")
    for entry in entries:
        table.add_row(
            entry.id,
            entry.timestamp[:16].replace("T", " "),
            entry.model,
            entry.tool,
            entry.status,
            entry.gate_result,
            "yes" if entry.tests_included else "no",
            capsule_mod.clip(entry.decision, 44),
        )
    console.print(table)
    if counts["pending"]:
        console.print("[yellow]Pending entries:[/yellow] commit with the Ledger-ID trailer (hooks do it automatically).")


# ---------------------------------------------------------------------------
# ui
# ---------------------------------------------------------------------------


@app.command("ui")
def ui_cmd(
    port: int = typer.Option(8765, "--port", min=1024, max=65535, help="Preferred port; busy ports fall through to the next."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser window."),
) -> None:
    """Start the local CodeLedger dashboard (bound to 127.0.0.1 only)."""
    webui_mod.run_ui(Path.cwd(), port=port, open_browser=not no_browser)


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------


@app.command("demo")
def demo_cmd(
    force: bool = typer.Option(False, "--force", help="Delete an existing ./ledger-demo and rebuild it."),
) -> None:
    """Create ./ledger-demo: a scripted sample repo proving the whole story."""
    demo_mod.run_demo(Path.cwd(), force=force)
