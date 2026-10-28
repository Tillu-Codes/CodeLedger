"""Store layer tests: the JSONL ledger's correctness and tolerance."""
from __future__ import annotations

import json

import pytest

from codeledger import store
from codeledger.util import write_text_lf


def make_entry(repo, **kw) -> store.Entry:
    payload = dict(
        prompt="p", model="m1", tool="t1", files=["a.py"], decision="d"
    )
    payload.update(kw)
    return store.add_entry(repo, **payload)


# ---------------------------------------------------------------------------
# ids, persistence, ordering
# ---------------------------------------------------------------------------


def test_add_entry_ids_increment(repo):
    e1 = make_entry(repo)
    e2 = make_entry(repo, files=["b.py"])
    assert e1.id == "L-0001"
    assert e2.id == "L-0002"
    assert e1.status == "pending" and e1.commit is None
    assert e1.gate_result == "n/a" and e1.tests_included is False


def test_ids_follow_max_after_manual_gap(repo):
    """A hand-crafted high id (e.g. after a manual edit) is respected."""
    store.save_entries(repo, [store.Entry(id="L-0042", timestamp="t", prompt="", model="", tool="")])
    nxt = store.add_entry(repo, prompt="p", model="m", tool="t", files=[], decision="")
    assert nxt.id == "L-0043"


def test_roundtrip_preserves_fields(repo):
    entry = store.add_entry(
        repo,
        prompt="the prompt",
        model="m",
        tool="t",
        files=["x.py", "sub/../y.py"],
        decision="decide",
        rejected_alternatives=[{"approach": "a", "reason": "b"}],
        tests_included=True,
    )
    loaded = store.load_entries(repo)
    assert loaded == [entry]
    raw = json.loads((repo / ".ledger/decisions.jsonl").read_text("utf-8"))
    assert raw["files"] == ["x.py", "sub/../y.py"]
    assert raw["tests_included"] is True
    assert "extra" not in raw  # only real fields are stored


def test_save_sorts_by_number(repo):
    sa = store.Entry(id="L-0002", timestamp="t", prompt="", model="", tool="")
    sb = store.Entry(id="L-0001", timestamp="t", prompt="", model="", tool="")
    store.save_entries(repo, [sa, sb])
    ids = [e.id for e in store.load_entries(repo)]
    assert ids == ["L-0001", "L-0002"]


def test_extra_keys_survive_roundtrip(repo):
    store.add_entry(repo, prompt="p", model="m", tool="t", files=[], decision="d")
    loaded = store.load_entries(repo)[0]
    loaded.extra["note"] = "hello"
    store.save_entries(repo, [loaded])
    again = store.load_entries(repo)[0]
    assert again.extra["note"] == "hello"
    raw = json.loads((repo / ".ledger/decisions.jsonl").read_text("utf-8"))
    assert raw["note"] == "hello"


# ---------------------------------------------------------------------------
# tolerance: missing / empty / corrupt
# ---------------------------------------------------------------------------


def test_missing_and_empty_ledger_are_empty(repo):
    assert store.load_entries(repo) == []
    (repo / ".ledger").mkdir()
    (repo / ".ledger/decisions.jsonl").write_bytes(b"")
    assert store.load_entries(repo) == []


def test_corrupt_lines_skipped_not_crash(repo):
    path = repo / ".ledger/decisions.jsonl"
    good = store.Entry(id="L-0001", timestamp="t", prompt="", model="", tool="").to_dict()
    write_text_lf(
        path,
        json.dumps(good) + "\n" + "{not json" + "\n" + "\n" + "[1,2]\n" + json.dumps(good | {"id": "L-0002"}) + "\n",
    )
    entries = store.load_entries(repo)
    assert [e.id for e in entries] == ["L-0001", "L-0002"]


def test_utf8_bom_on_first_line_tolerated(repo):
    path = repo / ".ledger/decisions.jsonl"
    good = store.Entry(id="L-0001", timestamp="t", prompt="", model="", tool="").to_dict()
    write_text_lf(path, "")  # create folder
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(good).encode("utf-8") + b"\n")
    assert [e.id for e in store.load_entries(repo)] == ["L-0001"]


def test_jsonl_without_id_or_bad_id_is_corrupt(repo):
    path = repo / ".ledger/decisions.jsonl"
    write_text_lf(path, '{"prompt": "no id"}\n{"id": "X-9"}\n')
    assert store.load_entries(repo) == []


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------


def test_commit_entries_marks_and_is_idempotent(repo):
    entry = make_entry(repo)
    sha = "f" * 40
    changed = store.commit_entries(repo, [entry.id], sha)
    assert [e.id for e in changed] == ["L-0001"]
    again = store.commit_entries(repo, [entry.id], sha)
    assert again == []  # second call with same sha: nothing changed
    stored = store.load_entries(repo)[0]
    assert stored.status == "committed" and stored.commit == sha
    # amend with a new sha updates
    changed = store.commit_entries(repo, [entry.id], "a" * 40)
    assert [e.id for e in changed] == ["L-0001"]
    assert store.load_entries(repo)[0].commit == "a" * 40


def test_update_entry_patches_and_returns(repo):
    entry = make_entry(repo)
    assert store.update_entry(repo, "L-9999", model="x") is None
    updated = store.update_entry(repo, entry.id, model="new-model")
    assert store.load_entries(repo)[0].model == "new-model"
    assert updated.id == "L-0001"


def test_pending_and_counts(repo):
    a = make_entry(repo)
    make_entry(repo, files=["tests/test_x.py"])
    store.commit_entries(repo, [a.id], "b" * 40)
    store.update_entry(repo, a.id, gate_result="pass", redactions=2)
    store.update_entry(repo, "L-0002", gate_result="bypassed")
    counts = store.counts(store.load_entries(repo))
    assert counts["total"] == 2
    assert counts["committed"] == 1 and counts["pending"] == 1
    assert counts["tests_included"] == 1
    assert counts["gate_pass"] == 1 and counts["gate_bypassed"] == 1
    assert counts["redactions"] == 2
