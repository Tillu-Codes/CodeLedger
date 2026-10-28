"""CAPSULE.md generation: determinism, sections, caps and the CLI wiring."""
from __future__ import annotations

from pathlib import Path

from codeledger import capsule, store
from codeledger.cli import app
from codeledger.store import Entry, add_entry


def _entry(n, *, model="m", tool="t", status="committed", gate="pass", tests=True,
           decision="decide", files=None, rejected=None, ts="2026-01-01T00:00:00Z"):
    return Entry(
        id=f"L-{n:04d}",
        timestamp=ts,
        prompt="p",
        model=model,
        tool=tool,
        files=files if files is not None else ["a.py"],
        decision=decision,
        rejected_alternatives=rejected or [],
        status=status,
        commit=("c" * 40) if status == "committed" else None,
        tests_included=tests,
        gate_result=gate,
    )


def test_clip():
    assert capsule.clip("a  b") == "a b"
    assert capsule.clip("x" * 200, 20) == ("x" * 19) + "\u2026"
    assert capsule.clip("", 5) == ""


def test_generate_is_deterministic():
    entries = [_entry(1), _entry(2, decision="second decision")]
    a = capsule.generate_capsule(entries, Path("demo"), branch="main")
    b = capsule.generate_capsule(entries, Path("demo"), branch="main")
    assert a == b


def test_sections_and_ordering():
    entries = [
        _entry(1, decision="older", ts="2026-01-01T00:00:00Z"),
        _entry(2, decision="newer", ts="2026-02-02T00:00:00Z",
               rejected=[{"approach": "bad", "reason": "broke"}]),
    ]
    text = capsule.generate_capsule(entries, Path("demo"))
    assert text.startswith("# CodeLedger capsule - demo")
    # newest first: L-0002 before L-0001
    assert text.index("L-0002") < text.index("L-0001")
    assert "bad - reason: broke (rejected in L-0002)" in text
    assert "## Don't do this again" in text
    assert "## Project snapshot" in text
    assert "Entries: 2 total, 2 committed, 0 pending" in text


def test_empty_ledger_text():
    text = capsule.generate_capsule([], Path("p"))
    assert "No entries yet" in text
    assert "none yet" in text  # no decisions yet


def test_pending_open_items():
    entries = [_entry(1, status="pending", gate="n/a")]
    text = capsule.generate_capsule(entries, Path("p"))
    assert "L-0001 (unlinked commit)" in text


def test_recent_files_counter():
    entries = [
        _entry(1, files=["a.py", "b.py"]),
        _entry(2, files=["a.py"]),
        _entry(3, files=["c.py"]),
    ]
    text = capsule.generate_capsule(entries, Path("p"))
    i_a = text.index("a.py (2 entries)")
    i_b = text.index("b.py (1 entry)")
    i_c = text.index("c.py (1 entry)")
    assert i_a < i_b < i_c  # sorted by -count then name


def test_hard_cap_truncation(monkeypatch):
    entries = [
        _entry(
            i,
            decision="d" * 150,
            rejected=[{"approach": f"a{i}-{j}", "reason": f"r{i}-{j}"} for j in range(30)],
        )
        for i in range(1, 21)
    ]
    monkeypatch.setattr(capsule, "MAX_LINES", 60)
    text = capsule.generate_capsule(entries, Path("p"))
    assert len(text.splitlines()) <= 60
    assert "capsule truncated" in text


def test_write_capsule_writes_file(tmp_path):
    entries = [_entry(1)]
    text, path = capsule.write_capsule(entries, tmp_path, branch="main")
    assert path == tmp_path / ".ledger" / "CAPSULE.md"
    assert path.read_text("utf-8") == text


def test_cli_capsule_stdout_and_file(inited_repo, cli, gateway):
    """The capsule reflects committed entries and pending ones differently."""
    add_entry(inited_repo, prompt="p", model="m", tool="t", files=["a.py"], decision="d")
    result = cli.invoke(app, ["capsule", "--stdout"])
    assert result.exit_code == 0
    assert "L-0001 (unlinked commit)" in result.output
    result = cli.invoke(app, ["capsule"])
    assert result.exit_code == 0 and "CAPSULE.md written" in result.output
    written = inited_repo / ".ledger" / "CAPSULE.md"
    assert written.is_file()
    assert "L-0001 (unlinked commit)" in written.read_text("utf-8")


def test_log_refreshes_capsule(inited_repo, cli, gateway):
    """`ledger log` silently re-renders the capsule after recording an entry."""
    result = cli.invoke(
        app, ["log", "-p", "p", "-m", "m", "-t", "t", "-d", "d", "--files", "a.py"]
    )
    assert result.exit_code == 0, result.output
    assert "Recorded L-0001" in result.output  # raw console output unchanged
    capsule_path = inited_repo / ".ledger" / "CAPSULE.md"
    assert capsule_path.is_file()
    text = capsule_path.read_text("utf-8")
    assert "L-0001 (unlinked commit)" in text  # Open items show the new entry
    # deterministic render: the file equals a fresh render of the same ledger
    entries = store.load_entries(inited_repo)
    assert text == capsule.generate_capsule(entries, inited_repo, branch="main")


def test_refresh_capsule_skips_without_ledger(tmp_path):
    """No .ledger/ here: silent skip, nothing is conjured into existence."""
    assert capsule.refresh_capsule(tmp_path) is None
    assert not (tmp_path / ".ledger").exists()
