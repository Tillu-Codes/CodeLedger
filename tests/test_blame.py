"""``ledger blame`` - parse_ref and the hero reveal over real git history."""
from __future__ import annotations

import pytest

from codeledger import blame, hooks, store
from codeledger.blame import parse_ref, reveal, reveal_to_dict
from codeledger.errors import LedgerError
from codeledger.store import add_entry

from conftest import GitSpy


# ---------------------------------------------------------------------------
# parse_ref
# ---------------------------------------------------------------------------


def test_parse_ref_valid():
    assert parse_ref("cart.py:24") == ("cart.py", 24)
    assert parse_ref("src/cart.py:1") == ("src/cart.py", 1)
    assert parse_ref("src\\cart.py:7") == ("src/cart.py", 7)
    assert parse_ref("a:b.py:3") == ("a:b.py", 3)


@pytest.mark.parametrize("ref", ["cart.py", "cart.py:", "cart.py:x", "cart.py:0", "cart.py:-4", ""])
def test_parse_ref_rejects_bad(ref):
    with pytest.raises(LedgerError):
        parse_ref(ref)


def test_parse_ref_zero_line_message():
    with pytest.raises(LedgerError, match="start at 1"):
        parse_ref("cart.py:0")


# ---------------------------------------------------------------------------
# reveal over a real AI-authored history
# ---------------------------------------------------------------------------


@pytest.fixture
def raid_repo(inited_repo, gateway: GitSpy):
    """One AI entry, committed trailer-style, over a two-line module."""
    add_entry(
        inited_repo,
        prompt="make adder",
        model="m1",
        tool="t1",
        files=["adder.py"],
        decision="plain function",
        rejected_alternatives=[{"approach": "X", "reason": "why not"}],
    )
    gateway.write(
        "adder.py",
        "def add(a, b):\n    return a + b\n\ndef broken():\n    return 1 / 0  # BUG\n",
    )
    gateway.commit("feat: adder\n\nLedger-ID: L-0001\n", "adder.py")
    return inited_repo


def test_reveal_ai_line(raid_repo, gateway: GitSpy):
    outcome = reveal(raid_repo, "adder.py:2")
    assert outcome.is_ai and not outcome.human
    assert outcome.entries and outcome.entries[0].id == "L-0001"
    assert outcome.entries[0].model == "m1" and outcome.entries[0].tool == "t1"
    assert outcome.commit.message.splitlines()[0] == "feat: adder"
    assert outcome.text == "    return a + b"


def test_reveal_json_shape(raid_repo):
    payload = reveal_to_dict(reveal(raid_repo, "adder.py:2"))
    assert payload["is_ai"] is True and payload["human"] is False
    assert payload["entries"][0]["id"] == "L-0001"
    assert payload["commit"]["short"]
    assert payload["file"] == "adder.py" and payload["line"] == 2


def test_reveal_human_line(raid_repo):
    outcome = reveal(raid_repo, "README.md:1")
    assert outcome.human and outcome.entries == []
    assert outcome.commit.message.splitlines()[0] == "initial"


def test_reveal_uncommitted_line(raid_repo, gateway: GitSpy):
    gateway.write("adder.py", "def add(a, b):\n    return a + b + 0  # uncommitted\n")
    outcome = reveal(raid_repo, "adder.py:2")
    assert outcome.uncommitted and outcome.entries == []
    assert "uncommitted" in outcome.text or "tweak" in outcome.text or "+ 0" in outcome.text


def test_reveal_out_of_range(raid_repo):
    with pytest.raises(LedgerError, match="out of range"):
        reveal(raid_repo, "adder.py:999")


def test_reveal_unknown_file(raid_repo):
    with pytest.raises(LedgerError, match="does not exist"):
        reveal(raid_repo, "nope.py:1")


def test_reveal_untracked_file(raid_repo, gateway: GitSpy):
    gateway.write("untracked.py", "print(1)\n")
    with pytest.raises(LedgerError, match="git blame failed|tracked"):
        reveal(raid_repo, "untracked.py:1")


def test_reveal_multiple_entries_on_one_commit(raid_repo, gateway: GitSpy):
    """A commit carrying two Ledger-IDs reveals both."""
    add_entry(
        raid_repo, prompt="p2", model="m2", tool="t2", files=["adder.py"], decision="d2"
    )
    gateway.write("adder.py", "def add(a, b):\n    return a + b  # refined\n")
    gateway.commit("fix: refine\n\nLedger-ID: L-0001, L-0002\n", "adder.py")
    got = reveal(raid_repo, "adder.py:2")
    assert [e.id for e in got.entries] == ["L-0001", "L-0002"]


def test_reveal_bug_line_shape(raid_repo):
    """The planted-BUG line (unlinked judgement aside) resolves through the same commit."""
    payload = reveal_to_dict(reveal(raid_repo, "adder.py:5"))
    assert payload["line"] == 5
