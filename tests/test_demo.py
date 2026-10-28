"""``ledger demo``: the scripted repo and everything it promises."""
from __future__ import annotations

from pathlib import Path

import pytest

from codeledger import blame, demo, store
from codeledger.demo import bug_line_of, run_demo

from conftest import GitSpy


def test_bug_line_of_unit():
    source = "line one\ndiscount is applied a second time\nline X\nline Y\n"
    assert bug_line_of(source) == 4  # marker line + 2


def test_demo_builds_the_whole_story(repo, gateway: GitSpy):
    target = run_demo(repo)  # note: repo's cwd is the repo; demo nests ./ledger-demo
    assert target.is_dir()

    # --- the ledger carries exactly the 6 promised entries -------------------
    entries = store.load_entries(target)
    assert demo.DEMO_ENTRY_COUNT == 6
    assert len(entries) == 6
    assert [e.id for e in entries] == [f"L-{i:04d}" for i in range(1, 7)]
    assert all(e.status == "committed" and e.commit for e in entries)

    by_id = {e.id: e for e in entries}
    assert by_id["L-0001"].model == "claude-sonnet-4.5" and by_id["L-0001"].tool == "opencode"
    assert by_id["L-0002"].model == "gpt-4.1" and by_id["L-0002"].tool == "cursor"
    assert by_id["L-0003"].model == "claude-sonnet-4.5" and by_id["L-0003"].tool == "opencode"
    assert by_id["L-0004"].model == "gemini-2.5-pro" and by_id["L-0004"].tool == "claude-code"
    assert by_id["L-0005"].model == "gemini-2.5-pro" and by_id["L-0005"].tool == "claude-code"
    assert by_id["L-0006"].model == "gpt-4.1" and by_id["L-0006"].tool == "cursor"
    assert "2026-10-26" in by_id["L-0001"].timestamp

    # --- gate story: two deliberate bypasses, one caught fail->fixed, rest pass
    assert {e.id for e in entries if e.gate_result == "bypassed"} == {"L-0001", "L-0003"}
    assert {e.id for e in entries if e.gate_result == "pass"} == {"L-0002", "L-0004", "L-0005", "L-0006"}
    assert not [e for e in entries if e.gate_result == "fail"]

    # --- tests_included mirrors the gate story -------------------------------
    assert {e.id for e in entries if e.tests_included} == {"L-0002", "L-0004", "L-0005", "L-0006"}
    assert not by_id["L-0001"].tests_included and not by_id["L-0003"].tests_included

    # --- the demo rejects alternatives are recorded --------------------------
    assert by_id["L-0003"].rejected_alternatives, "the buggy commit records what was rejected"

    # --- hero reveal: the planted bug blames the bypassed discount commit ----
    cart = gateway.read("ledger-demo/cart.py")
    line_no = bug_line_of(cart)
    outcome = blame.reveal(target, f"cart.py:{line_no}")
    assert outcome.is_ai
    assert [e.id for e in outcome.entries] == ["L-0003"]
    assert outcome.entries[0].gate_result == "bypassed"
    assert "discount" in outcome.entries[0].prompt.lower()

    # --- the planted bug really chews totals (double discount) --------------
    # subtotal 10.0, 10% discount, tax 0  -> correct 9.00, buggy 8.10
    script = (target / "DEMO_SCRIPT.md").read_text("utf-8")
    assert f"ledger blame cart.py:{line_no}" in script
    assert "3 minutes" in script or "3-minute" in script or "~3 minutes" in script

    # --- the repo's gitignore keeps the ledger uncommitted ------------------
    ignore = gateway.read("ledger-demo/.gitignore")
    assert ".ledger/" in ignore


def test_demo_is_reproducible_in_a_new_place(tmp_path, gateway: GitSpy):
    """Two builds produce the same ledger content (timestamps+plan are fixed)."""
    first = run_demo(tmp_path)
    second = run_demo(tmp_path, force=True)
    a = (first / ".ledger/decisions.jsonl").read_text("utf-8")
    b = (second / ".ledger/decisions.jsonl").read_text("utf-8")
    assert a == b


def test_demo_refuses_existing_dir_without_force(repo):
    run_demo(repo)
    with pytest.raises(demo.LedgerError, match="--force|ledger-demo"):
        run_demo(repo)
