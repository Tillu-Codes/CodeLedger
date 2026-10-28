"""Full CLI journey through REAL subprocesses (the installed `ledger`/python -m).

This is the honest end-to-end: every command runs as its own process, git
hooks run for real, and the demo's UX promises (friendly errors, exit codes)
are asserted from the outside, like a user would see them.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import List, Optional

import pytest

from conftest import GitSpy


def ledger(*args: str, cwd=None, env=None, input: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run `python -m codeledger` (same interpreter that runs pytest)."""
    return subprocess.run(
        [sys.executable, "-m", "codeledger", *args],
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        input=input,
        env=env,
    )


# ---------------------------------------------------------------------------
# basics
# ---------------------------------------------------------------------------


def test_version_and_help(repo):
    version = ledger("--version", cwd=repo)
    assert version.returncode == 0
    assert "codeledger 1.0.0" in version.stdout
    help_out = ledger("--help", cwd=repo)
    assert help_out.returncode == 0
    for command in ("init", "log", "blame", "gate", "capsule", "redact", "restore", "status", "ui", "demo"):
        assert command in help_out.stdout


def test_commands_outside_git_repo(repo, monkeypatch, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    proc = ledger("status", cwd=outside)
    assert proc.returncode != 0
    assert "git repository" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_hook_command_refuses_unknown_name(inited_repo):
    proc = ledger("hook", "pre-push", cwd=inited_repo)
    assert proc.returncode == 2
    assert "unknown hook" in proc.stderr


# ---------------------------------------------------------------------------
# the money journey: init -> log -> gate block -> tests -> linked commit -> blame
# ---------------------------------------------------------------------------


def test_full_journey(inited_repo, gateway: GitSpy):
    root = inited_repo

    # 1. AI writes a helper and it gets logged as pending
    gateway.write("brew.py", "def brew(kind):\n    return 'hot ' + kind\n")
    res = ledger(
        "log",
        "--prompt", "add a tea brew function",
        "--model", "m1",
        "--tool", "t1",
        "--decision", "no framework, plain str",
        "--files", "brew.py",
        "--rejected", "subprocess brewing::too slow",
        cwd=root,
    )
    assert res.returncode == 0, res.stderr
    assert "L-0001" in res.stdout

    # 2. commit without tests: BLOCKED by the gate with a paste-ready prompt
    res = ledger("status", cwd=root)
    assert res.returncode == 0 and "1 pending" in res.stdout
    gateway.stage("brew.py")
    blocked = ledger("hook", "pre-commit", cwd=root)
    assert blocked.returncode == 1
    assert "FAILED" in blocked.stdout and "brew.py" in blocked.stdout
    assert "tests/test_brew.py" in blocked.stdout  # suggested path in the prompt
    entry = gateway.entry("L-0001")
    assert entry.gate_result == "fail"

    # 3. tests arrive; the commit rides through the REAL hooks
    gateway.write("tests/test_brew.py", "from brew import brew\n\ndef test_brew():\n    assert brew('chai') == 'hot chai'\n")
    proc = gateway.git("add", "brew.py", "tests/test_brew.py", check=True)
    committed = gateway.git("commit", "-m", "feat: brew", check=False)
    out = committed.stdout + committed.stderr
    assert committed.returncode == 0, out
    assert "Ledger-ID trailer added" in out or "trailers" in out
    assert "linked L-0001" in out

    message = gateway.git("log", "-1", "--format=%B", check=True).stdout
    assert "Ledger-ID: L-0001" in message

    stored = gateway.entry("L-0001")
    assert stored.status == "committed"
    assert stored.commit == gateway.head()
    assert stored.gate_result == "pass"
    assert stored.tests_included is True

    # 4. the hero: blame remembers which prompt wrote this line
    blame_json = ledger("blame", "brew.py:1", "--json", cwd=root)
    assert blame_json.returncode == 0, blame_json.stderr
    payload = json.loads(blame_json.stdout)
    assert payload["is_ai"] is True
    assert payload["entries"][0]["id"] == "L-0001"
    assert payload["entries"][0]["prompt"] == "add a tea brew function"

    blame_panel = ledger("blame", "brew.py:1", cwd=root)
    assert blame_panel.returncode == 0
    assert "L-0001" in blame_panel.stdout
    assert "m1" in blame_panel.stdout and "add a tea brew function" in blame_panel.stdout

    # 5. capsule + status reflect the committed state
    res = ledger("capsule", cwd=root)
    assert res.returncode == 0 and "CAPSULE.md written" in res.stdout
    capsule = gateway.read(".ledger/CAPSULE.md")
    assert "L-0001" in capsule and "subprocess brewing" in capsule

    res = ledger("status", cwd=root)
    assert res.returncode == 0 and "0 pending" in res.stdout

    # 6. amend keeps a single trailer and re-links
    gateway.write("brew.py", "def brew(kind):\n    return 'hot ' + kind  # note\n")
    gateway.write("tests/test_brew.py", "from brew import brew\n\ndef test_brew():\n    assert brew('chai') == 'hot chai'\ndef test_brew2():\n    assert brew('chai') == 'hot chai'\n")
    gateway.stage("brew.py", "tests/test_brew.py")
    amended = gateway.git("commit", "--amend", "--no-edit", check=False)
    assert amended.returncode == 0, amended.stdout + amended.stderr
    message = gateway.git("log", "-1", "--format=%B", check=True).stdout
    assert message.count("Ledger-ID:") == 1
    stored = gateway.entry("L-0001")
    # the post-commit hook re-links to the amended sha
    assert stored.commit == gateway.head()


def test_gate_skip_flag_in_real_commit(inited_repo, gateway: GitSpy):
    """LEDGER_SKIP_GATE=1 lets a testless AI commit through, marked bypassed."""
    from codeledger.store import add_entry

    add_entry(inited_repo, prompt="p", model="m", tool="t", files=["tea.py"], decision="d")
    gateway.write("tea.py", "def tea():\n    return 'ok'\n")
    env = dict(os.environ)
    env["LEDGER_SKIP_GATE"] = "1"
    gateway.stage("tea.py")
    committed = subprocess.run(
        ["git", "commit", "-m", "feat: tea"],
        cwd=str(inited_repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert committed.returncode == 0, committed.stderr
    out = committed.stdout + committed.stderr
    assert "bypassed" in out
    stored = gateway.entry("L-0001")
    assert stored.gate_result == "bypassed"
    assert stored.status == "committed"


def test_gate_docs_only_commit_never_blocked(inited_repo, gateway: GitSpy):
    from codeledger.store import add_entry

    add_entry(inited_repo, prompt="docs pass", model="m", tool="t", files=["NOTES.md"], decision="d")
    gateway.write("NOTES.md", "# just docs\n")
    gateway.stage("NOTES.md")
    proc = gateway.git("commit", "-m", "docs: note", check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert gateway.entry("L-0001").status == "committed"
