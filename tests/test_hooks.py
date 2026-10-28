"""Hook behaviour: trailers, linking, gate-on-commit, and the real git round trip.

The hooks are exercised directly through ``codeledger.hooks.run_hook`` (the
same entry point the installed hook scripts call into the CLI), plus one
end-to-end test that runs a real ``git commit`` so the installed hook files
actually spawn the ledger CLI.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from codeledger import hooks, store
from codeledger.errors import LedgerError
from codeledger.store import add_entry

from conftest import GitSpy


# ---------------------------------------------------------------------------
# prepare-commit-msg: Ledger-ID trailer
# ---------------------------------------------------------------------------


def test_trailer_appended_for_pending_entry(inited_repo, gateway: GitSpy):
    add_entry(inited_repo, prompt="p", model="m", tool="t", files=["a.py"], decision="d")
    msg = inited_repo / ".git" / "COMMIT_EDITMSG"
    gateway.write(".git/COMMIT_EDITMSG", "feat: thing\n")
    code = hooks.run_hook("prepare-commit-msg", [str(msg), "commit"], inited_repo)
    assert code == 0
    assert msg.read_text("utf-8") == "feat: thing\n\nLedger-ID: L-0001\n"


def test_trailer_lists_all_pending_entries(inited_repo, gateway: GitSpy):
    add_entry(inited_repo, prompt="p", model="m", tool="t", files=["a.py"], decision="d")
    add_entry(inited_repo, prompt="p2", model="m", tool="t", files=["b.py"], decision="d2")
    msg = inited_repo / ".git" / "COMMIT_EDITMSG"
    gateway.write(".git/COMMIT_EDITMSG", "feat: thing\n")
    hooks.run_hook("prepare-commit-msg", [str(msg), "commit"], inited_repo)
    assert "Ledger-ID: L-0001, L-0002" in msg.read_text("utf-8")


def test_trailer_never_duplicates(inited_repo, gateway: GitSpy):
    """Amending (or re-running) must not add a second trailer."""
    add_entry(inited_repo, prompt="p", model="m", tool="t", files=["a.py"], decision="d")
    msg = inited_repo / ".git" / "COMMIT_EDITMSG"
    gateway.write(".git/COMMIT_EDITMSG", "feat: thing\n\nLedger-ID: L-0001\n")
    hooks.run_hook("prepare-commit-msg", [str(msg), "commit"], inited_repo)
    assert msg.read_text("utf-8").count("Ledger-ID:") == 1


def test_trailer_respects_instructions_block(inited_repo, gateway: GitSpy):
    """git appends '#' comment lines; they must stay last and untouched."""
    add_entry(inited_repo, prompt="p", model="m", tool="t", files=["a.py"], decision="d")
    msg = inited_repo / ".git" / "COMMIT_EDITMSG"
    gateway.write(
        ".git/COMMIT_EDITMSG",
        "feat: thing\n# Please enter the commit message...\n# more help\n",
    )
    hooks.run_hook("prepare-commit-msg", [str(msg), "commit"], inited_repo)
    text = msg.read_text("utf-8")
    marker = "Ledger-ID: L-0001"
    body, _, tail = text.partition(marker)
    assert body == "feat: thing\n\n"
    assert tail.splitlines() == ["", "# Please enter the commit message...", "# more help"]


def test_trailer_skipped_for_merge_and_squash(inited_repo, gateway: GitSpy):
    add_entry(inited_repo, prompt="p", model="m", tool="t", files=["a.py"], decision="d")
    msg = inited_repo / ".git" / "COMMIT_EDITMSG"
    gateway.write(".git/COMMIT_EDITMSG", "Merge branch 'x'\n")
    for source in ("merge", "squash"):
        hooks.run_hook("prepare-commit-msg", [str(msg), source], inited_repo)
    assert "Ledger-ID" not in msg.read_text("utf-8")


def test_trailer_noop_without_pending_entries(inited_repo, gateway: GitSpy):
    msg = inited_repo / ".git" / "COMMIT_EDITMSG"
    gateway.write(".git/COMMIT_EDITMSG", "plain human commit\n")
    assert hooks.run_hook("prepare-commit-msg", [str(msg), "commit"], inited_repo) == 0
    assert msg.read_text("utf-8") == "plain human commit\n"


def test_unknown_hook_name_rejected(inited_repo):
    with pytest.raises(LedgerError, match="unknown hook"):
        hooks.run_hook("pre-push", [], inited_repo)


# ---------------------------------------------------------------------------
# post-commit: linking the sha into the ledger
# ---------------------------------------------------------------------------


def test_post_commit_links_trailed_commit(inited_repo, gateway: GitSpy):
    entry = add_entry(inited_repo, prompt="p", model="m", tool="t", files=["a.py"], decision="d")
    gateway.write("a.py", "def a():\n    return 1\n")
    gateway.commit("feat: anything\n\nLedger-ID: L-0001\n", "a.py")
    assert hooks.run_hook("post-commit", [], inited_repo) == 0
    stored = gateway.entry(entry.id)
    assert stored.status == "committed"
    assert stored.commit == gateway.head()


def test_post_commit_noop_for_untrailed_commit(inited_repo):
    add_entry(inited_repo, prompt="p", model="m", tool="t", files=["a.py"], decision="d")
    assert hooks.run_hook("post-commit", [], inited_repo) == 0
    assert [e for e in store.load_entries(inited_repo) if e.status == "committed"] == []


# ---------------------------------------------------------------------------
# pre-commit: the gate inside git commit
# ---------------------------------------------------------------------------


def test_pre_commit_blocks_source_without_tests(inited_repo, gateway: GitSpy):
    add_entry(inited_repo, prompt="p", model="m", tool="t", files=["cart.py"], decision="d")
    gateway.write("cart.py", "class Cart:\n    pass\n")
    gateway.stage("cart.py")
    code = hooks.run_hook("pre-commit", [], inited_repo)
    assert code == 1
    stored = gateway.entry("L-0001")
    assert stored.gate_result == "fail"


def test_pre_commit_passes_with_tests(inited_repo, gateway: GitSpy):
    add_entry(inited_repo, prompt="p", model="m", tool="t", files=["cart.py"], decision="d")
    gateway.write("cart.py", "class Cart:\n    pass\n")
    gateway.write("tests/test_cart.py", "def test_cart():\n    pass\n")
    gateway.stage("cart.py", "tests/test_cart.py")
    assert hooks.run_hook("pre-commit", [], inited_repo) == 0
    stored = gateway.entry("L-0001")
    # A test file rides along even though the entry only listed the source
    assert stored.gate_result == "pass"
    assert stored.tests_included is True


def test_pre_commit_bypass_records_bypassed(inited_repo, gateway: GitSpy, monkeypatch):
    add_entry(inited_repo, prompt="p", model="m", tool="t", files=["cart.py"], decision="d")
    gateway.write("cart.py", "class Cart:\n    pass\n")
    gateway.stage("cart.py")
    monkeypatch.setenv("LEDGER_SKIP_GATE", "1")
    assert hooks.run_hook("pre-commit", [], inited_repo) == 0
    assert gateway.entry("L-0001").gate_result == "bypassed"


def test_pre_commit_allows_docs_only(inited_repo, gateway: GitSpy):
    add_entry(inited_repo, prompt="p", model="m", tool="t", files=["NOTES.md"], decision="d")
    gateway.write("NOTES.md", "# note\n")
    gateway.stage("NOTES.md")
    assert hooks.run_hook("pre-commit", [], inited_repo) == 0


def test_pre_commit_noop_without_pending(inited_repo, gateway: GitSpy):
    gateway.write("cart.py", "class Cart:\n    pass\n")
    gateway.stage("cart.py")
    assert hooks.run_hook("pre-commit", [], inited_repo) == 0


# ---------------------------------------------------------------------------
# The REAL installed hook files, through a real git commit
# ---------------------------------------------------------------------------


def test_real_git_commit_links_entry(inited_repo, gateway: GitSpy):
    """The installed hook scripts must work end-to-end: git -> sh -> ledger."""
    add_entry(inited_repo, prompt="p", model="m", tool="t", files=["a.py"], decision="d")
    gateway.write("a.py", "def a():\n    return 1\n")
    gateway.write("tests/test_a.py", "def test_a():\n    assert True\n")
    env = dict(os.environ)
    env["LEDGER_SKIP_GATE"] = ""  # present-but-empty: no bypass requested
    proc = subprocess.run(
        ["git", "add", "a.py", "tests/test_a.py"],
        cwd=str(inited_repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    proc = subprocess.run(
        ["git", "commit", "-m", "feat: a without verify"],
        cwd=str(inited_repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout + proc.stderr
    assert "Ledger-ID trailer added" in out          # prepare-commit-msg ran (python fallback)
    assert "linked L-0001" in out                     # post-commit ran
    stored = gateway.entry("L-0001")
    assert stored.status == "committed"
    assert stored.commit == gateway.head()[:40]
    assert gateway.trailer() == "L-0001"


def test_real_git_commit_refreshes_capsule(inited_repo, gateway: GitSpy):
    """After the post-commit hook links the entry, CAPSULE.md is already fresh."""
    entry = add_entry(inited_repo, prompt="p", model="m", tool="t", files=["a.py"], decision="d")
    gateway.write("a.py", "def a():\n    return 1\n")
    gateway.write("tests/test_a.py", "def test_a():\n    assert True\n")
    proc = subprocess.run(
        ["git", "add", "a.py", "tests/test_a.py"],
        cwd=str(inited_repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(os.environ),  # no LEDGER_SKIP_GATE: the gate must pass for real
    )
    assert proc.returncode == 0, proc.stderr
    proc = subprocess.run(
        ["git", "commit", "-m", "feat: a with a fresh capsule"],
        cwd=str(inited_repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(os.environ),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "linked L-0001" in proc.stdout + proc.stderr  # post-commit ran

    from codeledger import capsule

    text = gateway.read(".ledger/CAPSULE.md")
    assert "every recorded change is linked to a commit" in text  # Open items emptied
    assert f"{entry.id} (unlinked commit)" not in text  # no longer pending
    assert entry.id in text  # the decision is listed as committed
    # deterministic render: matches the committed ledger state exactly
    entries = store.load_entries(inited_repo)
    assert text == capsule.generate_capsule(entries, inited_repo, branch="main")
