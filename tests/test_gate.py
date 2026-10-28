"""Gate classification + run_gate semantics (the policy engine)."""
from __future__ import annotations

import pytest

from codeledger import gate, store
from codeledger.errors import LedgerError

from conftest import GitSpy


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", [
    "tests/test_x.py", "test/test_x.py", "__tests__/base.jsx", "tests/x/conftest.py",
    "test_cart.py", "src/test_cart.py", "cart_test.py", "app.test.js", "app.test.ts",
    "comp.spec.jsx", "blame.spec.ts", "thing.spec", "cart.spec", "sub/tests/x.py",
])
def test_is_test_file_true(path):
    assert gate.is_test_file(path)


@pytest.mark.parametrize("path", [
    "test_cart.txt", "tests.py", "protest_x.py", "mytests/y.py", "specs/x.py",
    "src/cart.py", "cart.spec.txt", "cart_test_txt", "testlib.py", "contest.py",
])
def test_is_test_file_false(path):
    assert not gate.is_test_file(path)


@pytest.mark.parametrize("path", [
    "README.md", "docs/index.rst", "notes.txt", "guide.adoc", "pyproject.toml",
    "setup.cfg", "conf.ini", "deploy.yaml", "k8s.yml", "requirements.lock",
    "poetry.lock", "package-lock.json", "yarn.lock", ".gitignore", ".editorconfig",
    "package.json", "tsconfig.json", "docs/src/code.py", "LICENSE",
    "Dockerfile", "Makefile",
    ".ledger/decisions.jsonl",
    ".ledger/CAPSULE.md", ".ledger/redact_map.json", ".ledger",
])
def test_doc_or_config_never_blocked(path):
    assert gate.is_doc_or_config(path)
    assert gate.classify_path(path) in ("docs", "test")


@pytest.mark.parametrize("path", [
    "cart.py", "src/cart.py", "cli.ts", "userdata.bin", "fixpix", ".ledgerx/out.py",
])
def test_source_bucketers(path):
    assert gate.classify_path(path) == "source"
    assert not gate.is_doc_or_config(path)


def test_entry_has_tests():
    assert gate.entry_has_tests(["cart.py", "tests/test_cart.py"])
    assert gate.entry_has_tests(["test_cart.py"])
    assert not gate.entry_has_tests(["cart.py", "README.md"])


def test_test_fingerprint():
    assert gate.test_fingerprint(["cart.py", "src/util/receipt.py"]) == [
        "tests/test_cart.py", "tests/test_receipt.py"
    ]


# ---------------------------------------------------------------------------
# run_gate
# ---------------------------------------------------------------------------


def _pending(repo, files):
    e = store.add_entry(repo, prompt="p", model="m", tool="t", files=files, decision="d")
    return e


def test_gate_no_pending_is_clean_pass(inited_repo, gateway: GitSpy):
    gateway.write("cart.py", "class Cart:\n    pass\n")
    gateway.stage("cart.py")
    outcome = gate.run_gate(inited_repo, skip=False, record=False)
    assert not outcome.blocked and not outcome.bypassed
    assert "No pending" in outcome.reason


def test_gate_blocks_source_without_tests(repo, gateway: GitSpy):
    _pending(repo, ["cart.py"])
    gateway.write("cart.py", "class Cart:\n    pass\n")
    gateway.stage("cart.py")
    outcome = gate.run_gate(repo, skip=False, record=True)
    assert outcome.blocked and outcome.exit_code == 1
    assert outcome.sources == ["cart.py"]
    assert outcome.tests == []
    assert "cart.py" in outcome.test_prompt and "tests/test_cart.py" in outcome.test_prompt
    assert gateway.entry("L-0001").gate_result == "fail"
    assert gateway.entry("L-0001").status == "pending"  # still pending after a block


def test_gate_passes_when_tests_staged_with_source(repo, gateway: GitSpy):
    _pending(repo, ["cart.py"])
    gateway.write("cart.py", "class Cart:\n    pass\n")
    gateway.write("tests/test_cart.py", "def test_cart():\n    pass\n")
    gateway.stage("cart.py", "tests/test_cart.py")
    assert not gate.run_gate(repo, skip=False, record=True).blocked
    entry = gateway.entry("L-0001")
    assert entry.gate_result == "pass"
    assert entry.tests_included is True  # test file staged alongside source


def test_gate_records_pass_with_docs_only(repo, gateway: GitSpy):
    _pending(repo, ["NOTES.md"])
    gateway.write("NOTES.md", "# note\n")
    gateway.stage("NOTES.md")
    outcome = gate.run_gate(repo, skip=False, record=True)
    assert not outcome.blocked
    assert "docs/config" in outcome.reason
    assert gateway.entry("L-0001").gate_result == "pass"


def test_gate_unlisted_source_extra_but_claimed_still_blocks(inited_repo, gateway: GitSpy):
    """Only claimed files are gated, but if a claimed source is staged without
    tests, the gate blocks even when extra unlisted sources are also staged."""
    _pending(inited_repo, ["cart.py"])
    gateway.write("cart.py", "class Cart:\n    pass\n")
    gateway.write("other.py", "def o():\n    return 1\n")
    gateway.stage("cart.py", "other.py")
    assert gate.run_gate(inited_repo, skip=False, record=False).blocked


def test_gate_bypass_env(repo, gateway: GitSpy, monkeypatch):
    _pending(repo, ["cart.py"])
    gateway.write("cart.py", "class Cart:\n    pass\n")
    gateway.stage("cart.py")
    monkeypatch.setenv("LEDGER_SKIP_GATE", "1")
    outcome = gate.run_gate(repo, skip=gate.bypass_requested())
    assert outcome.bypassed and outcome.exit_code == 0
    assert gateway.entry("L-0001").gate_result == "bypassed"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on"])
def test_bypass_requested_values(value, monkeypatch):
    monkeypatch.setenv("LEDGER_SKIP_GATE", value)
    assert gate.bypass_requested()
    monkeypatch.setenv("LEDGER_SKIP_GATE", "0")
    assert not gate.bypass_requested()


def test_gate_nothing_staged(repo):
    _pending(repo, ["cart.py"])
    outcome = gate.run_gate(repo, skip=False, record=False)
    assert not outcome.blocked
    assert "Nothing staged" in outcome.reason


def test_gate_requires_ledger(repo, monkeypatch):
    with pytest.raises(LedgerError, match="ledger init"):
        gate.run_gate(repo, skip=False, record=False)
