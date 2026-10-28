"""``ledger log`` CLI behaviour (via Typer's CliRunner)."""
from __future__ import annotations

import json

import pytest

from codeledger import store
from codeledger.cli import app
from codeledger.errors import LedgerError

from conftest import GitSpy


def test_log_records_pending_entry(inited_repo, cli, gateway: GitSpy):
    gateway.write("feature.py", "def f():\n    return 1\n")
    result = cli.invoke(
        app,
        [
            "log",
            "--prompt", "add feature",
            "--model", "m1",
            "--tool", "t1",
            "--files", "feature.py",
            "--decision", "plain function",
            "--rejected", "class Feature::too heavy",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "L-0001" in result.output and "pending" in result.output
    entry = gateway.entry("L-0001")
    assert entry.prompt == "add feature"
    assert entry.model == "m1" and entry.tool == "t1"
    assert entry.files == ["feature.py"]
    assert entry.rejected_alternatives == [{"approach": "class Feature", "reason": "too heavy"}]


def test_log_rejected_requires_double_colon(inited_repo, cli):
    result = cli.invoke(
        app,
        ["log", "-p", "p", "-m", "m", "-t", "t", "-d", "d", "-r", "missing-reason"],
    )
    assert result.exit_code != 0
    assert isinstance(result.exception, LedgerError)
    assert "approach::reason" in str(result.exception)


def test_log_missing_required_fields_errors_non_interactive(inited_repo, cli):
    """Without --prompt/--decision, non-interactive runs fail cleanly."""
    result = cli.invoke(app, ["log", "--model", "m", "--tool", "t"])
    assert result.exit_code != 0
    assert isinstance(result.exception, LedgerError)
    message = str(result.exception)
    assert "--prompt" in message and "--decision" in message


def test_log_default_files_exclude_ledger_data(inited_repo, cli, gateway: GitSpy):
    """CodeLedger's own bookkeeping is never recorded as the AI change."""
    gateway.write("code.py", "print(1)\n")
    result = cli.invoke(app, ["log", "-p", "p", "-m", "m", "-t", "t", "-d", "d"])
    assert result.exit_code == 0, result.output
    entry = gateway.entry("L-0001")
    assert "code.py" in entry.files
    assert not any(f == ".ledger" or f.startswith(".ledger/") for f in entry.files)


def test_log_tests_flag_auto_detection(inited_repo, cli, gateway: GitSpy):
    gateway.write("tests/test_a.py", "def test_a():\n    pass\n")
    result = cli.invoke(
        app,
        ["log", "-p", "p", "-m", "m", "-t", "t", "-d", "d", "--files", "tests/test_a.py"],
    )
    assert result.exit_code == 0
    assert gateway.entry("L-0001").tests_included is True


def test_log_writes_one_jsonl_line_per_entry(inited_repo, cli):
    for i, name in enumerate("ab", start=1):
        cli.invoke(app, ["log", "-p", f"p{i}", "-m", "m", "-t", "t", "-d", f"d{i}", "--files", f"{name}.py"])
    lines = (inited_repo / ".ledger/decisions.jsonl").read_text("utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["id"] for line in lines] == ["L-0001", "L-0002"]


def test_log_before_init_fails_friendly(repo, cli):
    result = cli.invoke(app, ["log", "-p", "p", "-m", "m", "-t", "t", "-d", "d"])
    assert result.exit_code != 0
    assert isinstance(result.exception, LedgerError)
    assert "ledger init" in str(result.exception)


def test_log_outside_git_repo_fails(tmp_path, monkeypatch, cli):
    outside = tmp_path / "nowhere"
    outside.mkdir()
    monkeypatch.chdir(outside)
    result = cli.invoke(app, ["log", "-p", "p", "-m", "m", "-t", "t", "-d", "d"])
    assert result.exit_code != 0
    assert "git repository" in str(result.exception)
