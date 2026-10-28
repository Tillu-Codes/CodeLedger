"""``ledger demo`` - build a scripted sample repo that proves the story.

``ledger demo`` creates ``./ledger-demo/``: a small Python shopping-cart app
with a realistic git history of six AI entries across three model/tool pairs.

The story arc:
  1. an AI scaffolds the cart (bypassed gate, no tests yet);
  2. a second tool adds tax handling *with* tests (gate passes);
  3. the first model adds a discount feature that plants a bug - the
     discount is applied twice - and ships via bypass;
  4. a third model adds receipts; the gate blocks the commit until tests
     are written, then it passes;
  5. an xfail test pins the double-discount bug;
  6. a "new tool, new session" change reads the capsule and avoids the
     rejected approaches - and adds a quantity rollup.

Afterwards ``ledger blame cart.py:<buggy line>`` reveals the prompt/model
behind the bug, and DEMO_SCRIPT.md contains a 3-minute presenter script.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from codeledger import blame as blame_mod
from codeledger import capsule as capsule_mod
from codeledger import initcmd, store
from codeledger.errors import LedgerError
from codeledger.gitutil import git_available, run_git
from codeledger.util import console, write_text_lf

__all__ = ["run_demo", "DEMO_DIR_NAME", "DEMO_ENTRY_COUNT", "bug_line_of"]

DEMO_DIR_NAME = "ledger-demo"
DEMO_ENTRY_COUNT = 6

_BUG_MARKER = "discount is applied a second time"

# ---------------------------------------------------------------------------
# Deterministic file contents
# ---------------------------------------------------------------------------

_DOC_V1 = '"""Shopping cart (v1): items and subtotals."""'
_DOC_V4 = '"""Shopping cart (v4): quantity rollup added from a capsule-restored session."""'

_CART_PREAMBLE = (
    '"""\n'  # placeholder docstring replaced below; real content assigned later
)
del _CART_PREAMBLE

_CART_SHARED_TOP = '''\
{docstring}

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


@dataclass
class LineItem:
    name: str
    unit_price: float  # stored in dollars (float is fine for the demo)
    quantity: int = 1


class Cart:
    """Tiny in-memory shopping cart."""

    def __init__(self) -> None:
        self._items: Dict[str, LineItem] = {}
{extra_state}
'''

_CART_V1_STATE = ""
_CART_V2_STATE = "        self._tax_rate: float = 0.0"
_CART_V3_STATE = "        self._tax_rate: float = 0.0\n        self._discount_pct: float = 0.0"

_CART_TAX_METHOD = '''\

    def set_tax_rate(self, rate: float) -> None:
        """Register a tax rate as a percentage (for example 8.25 for 8.25%)."""
        if rate < 0:
            raise ValueError("tax rate must be >= 0")
        self._tax_rate = rate
'''

_CART_CORE_METHODS = '''\

    def add_item(self, name: str, unit_price: float, quantity: int = 1) -> None:
        if not name:
            raise ValueError("item name must not be empty")
        if unit_price < 0:
            raise ValueError("unit_price must be >= 0")
        if quantity < 1:
            raise ValueError("quantity must be >= 1")
        if name in self._items:
            self._items[name].quantity += quantity
        else:
            self._items[name] = LineItem(name=name, unit_price=unit_price, quantity=quantity)

    def remove_item(self, name: str) -> bool:
        if name in self._items:
            del self._items[name]
            return True
        return False

    def items(self) -> "List[LineItem]":
        return list(self._items.values())

    def subtotal(self) -> float:
        return sum(i.unit_price * i.quantity for i in self._items.values())
'''

_GET_TOTAL_V2 = '''\

    def get_total(self) -> float:
        total = self.subtotal() * (1.0 + self._tax_rate / 100.0)
        return round(total, 2)
'''

_DISCOUNT_METHOD_AND_BUGGY_TOTAL = '''\

    def apply_discount(self, pct: float) -> None:
        """Record a sale discount as a percentage (10 means 10% off)."""
        if not 0.0 <= pct <= 100.0:
            raise ValueError("discount percentage must be between 0 and 100")
        self._discount_pct = pct

    def get_total(self) -> float:
        pre_discount = self.subtotal()
        after_discount = pre_discount - pre_discount * (self._discount_pct / 100.0)
        # BUG kept on purpose (demo): the discount is applied a second time,
        # so totals come out too low whenever a discount is set.
        total = after_discount - after_discount * (self._discount_pct / 100.0)
        grand_total = total * (1.0 + self._tax_rate / 100.0)
        return round(grand_total, 2)
'''

_TOTAL_ITEMS_METHOD = '''\

    def total_items(self) -> int:
        """Number of physical items (rolled up for receipts)."""
        return sum(i.quantity for i in self._items.values())
'''


def _cart_source(docstring: str, state: str, *, tax: bool, discount: bool) -> str:
    """Assemble a cart.py snapshot; the buggy block only lands in v3/v4.

    ``.replace`` (not ``str.format``) because the cart source contains
    literal braces (``{}`` dict literal) that must survive untouched.
    """
    parts = [
        _CART_SHARED_TOP.replace("{docstring}", docstring).replace("{extra_state}", state)
    ]
    if tax:
        parts.append(_CART_TAX_METHOD)
    parts.append(_CART_CORE_METHODS)
    parts.append(_DISCOUNT_METHOD_AND_BUGGY_TOTAL if discount else _GET_TOTAL_V2)
    return "".join(parts)


def _cart_v1() -> str:
    return _cart_source(_DOC_V1, _CART_V1_STATE, tax=False, discount=False)


def _cart_v2() -> str:
    return _cart_source(_DOC_V1.replace("(v1)", "(v2)").replace("items and subtotals", "configurable sales tax"), _CART_V2_STATE, tax=True, discount=False)


def _cart_v3_buggy() -> str:
    return _cart_source(
        _DOC_V1.replace("(v1)", "(v3)").replace("items and subtotals", "percentage discount"),
        _CART_V3_STATE,
        tax=True,
        discount=True,
    )


def _cart_v4_final() -> str:
    """v4 = the buggy v3 body (unchanged bug lines) + a rollup method at EOF.

    Appending at the end keeps every line above identical, so the bug line's
    blame keeps pointing at the v3 commit - exactly the demo story needs.
    """

    base = _cart_v3_buggy().replace("(v3)", "(v4)").replace(
        "percentage discount", "quantity rollup added from a capsule-restored session", 1
    )
    return base + _TOTAL_ITEMS_METHOD


_TESTS_BASE = '''\
"""Tests for the shopping cart."""

import pytest

from cart import Cart


def test_add_and_subtotal() -> None:
    cart = Cart()
    cart.add_item("apple", 1.0, quantity=4)
    cart.add_item("pear", 2.0)
    assert cart.subtotal() == 6.0


def test_remove_item() -> None:
    cart = Cart()
    cart.add_item("apple", 1.0)
    assert cart.remove_item("apple") is True
    assert cart.remove_item("banana") is False


def test_tax_rate() -> None:
    cart = Cart()
    cart.add_item("apple", 1.0, quantity=10)
    cart.set_tax_rate(10.0)
    assert cart.get_total() == 11.0


def test_validation() -> None:
    cart = Cart()
    with pytest.raises(ValueError):
        cart.add_item("", 1.0)
    with pytest.raises(ValueError):
        cart.add_item("Pear", -1.0)
'''

_XFAIL_TEST = '''\

@pytest.mark.xfail(reason="double-discount bug pinned by CodeLedger entry L-0003")
def test_discount_is_applied_once() -> None:
    cart = Cart()
    cart.add_item("apple", 10.0)
    cart.apply_discount(10.0)
    assert cart.get_total() == 9.0  # 8.10 with the planted double-discount bug
'''

_TOTAL_ITEMS_TEST = '''\

def test_total_items() -> None:
    cart = Cart()
    cart.add_item("apple", 1.0, quantity=4)
    cart.add_item("pear", 2.0, quantity=2)
    assert cart.total_items() == 6
'''


def _tests_v5() -> str:
    """v5: base tests + the xfail test that pins the double-discount bug."""
    return _TESTS_BASE + _XFAIL_TEST


def _tests_v6() -> str:
    """v6: v5 + the quantity rollup test (file grew, nothing above moved)."""
    return _tests_v5() + _TOTAL_ITEMS_TEST


_RECEIPT = '''\
"""Checkout receipt rendering for the shopping cart."""

from __future__ import annotations

from cart import Cart


def format_receipt(cart: Cart) -> str:
    """Render a plain-text receipt (items + grand total)."""
    lines = ["RECEIPT", "-------"]
    for item in cart.items():
        subtotal = item.unit_price * item.quantity
        lines.append(
            f"{item.quantity:>3} x {item.name:<16} @ ${item.unit_price:>7.2f} = ${subtotal:>8.2f}"
        )
    lines.append(f"TOTAL: ${cart.get_total():.2f}")
    return "\n".join(lines)
'''

_RECEIPT_TESTS = '''\
"""Tests for receipt rendering (written after the gate blocked the commit)."""

from receipt import format_receipt

from cart import Cart


def test_receipt_contents() -> None:
    cart = Cart()
    cart.add_item("apple", 1.25, quantity=3)
    lines = format_receipt(cart).splitlines()
    assert lines[0] == "RECEIPT"
    assert "3 x apple" in lines[2]
    assert "TOTAL: $3.75" in lines[-1]
'''


def bug_line_of(final_cart_source: str) -> int:
    """Line number of the double-discount bug inside the final cart.py."""
    marker_lines = [
        index
        for index, line in enumerate(final_cart_source.splitlines(), start=1)
        if _BUG_MARKER in line
    ]
    if not marker_lines:
        raise LedgerError("internal demo error: bug marker not found in cart source")
    return marker_lines[0] + 2  # bug is two lines under the marker comment


# ---------------------------------------------------------------------------
# The fixed history: entries then commits, step by step
# ---------------------------------------------------------------------------

_AUTHOR_EMAIL = {
    "Ada Chen": "ada@codeledger.demo",
    "Ben Park": "ben@codeledger.demo",
    "Chi Wu": "chi@codeledger.demo",
}

_REJECTED_TAX = {
    "approach": "Hardcode the tax rate as a module constant",
    "reason": "Rates differ per store; a settable field keeps the module reusable",
}
_REJECTED_SPLIT_TOTAL = {
    "approach": "Return (subtotal, discount) and let the UI compute totals",
    "reason": "Pushes arithmetic to every caller; totals become inconsistent across views",
}
_REJECTED_HTML = {
    "approach": "Render the receipt as HTML directly",
    "reason": "The demo is terminal-only; HTML invites injection issues",
}
_REJECTED_LIST_ITEMS = {
    "approach": "Track items as a list and scan per add",
    "reason": "Name-keyed dict keeps merges O(1); list scanning resurfaces per line",
}


def _entry_specs() -> List[Dict[str, object]]:
    """The six demo entries, in commit order."""
    return [
        {
            "timestamp": "2026-10-26T09:12:00+00:00",
            "prompt": "Create a shopping cart module with add/remove and subtotals",
            "model": "claude-sonnet-4.5",
            "tool": "opencode",
            "files": ["cart.py"],
            "decision": "Scaffold Cart with validation; float money kept for demo simplicity",
            "rejected_alternatives": [],
        },
        {
            "timestamp": "2026-10-26T09:26:00+00:00",
            "prompt": "Add a configurable sales tax and make the grand total explicit",
            "model": "gpt-4.1",
            "tool": "cursor",
            "files": ["cart.py", "tests/test_cart.py"],
            "decision": "Tax lives on the cart as a percentage; get_total applies it once - tests added in the same change",
            "rejected_alternatives": [_REJECTED_TAX],
        },
        {
            "timestamp": "2026-10-26T10:03:00+00:00",
            "prompt": "Add a holiday discount: apply_discount(pct) should reduce the grand total",
            "model": "claude-sonnet-4.5",
            "tool": "opencode",
            "files": ["cart.py"],
            "decision": "Store the discount on the cart and apply it inside get_total (bug: applied twice)",
            "rejected_alternatives": [_REJECTED_SPLIT_TOTAL],
        },
        {
            "timestamp": "2026-10-26T10:41:00+00:00",
            "prompt": "Add a receipt formatter printing items and the grand total",
            "model": "gemini-2.5-pro",
            "tool": "claude-code",
            "files": ["receipt.py"],
            "decision": "Render from cart.items() plus get_total; no new cart state",
            "rejected_alternatives": [_REJECTED_HTML],
        },
        {
            "timestamp": "2026-10-26T11:07:00+00:00",
            "prompt": "Pin the double-discount bug with an xfail test so it cannot silently regress",
            "model": "gemini-2.5-pro",
            "tool": "claude-code",
            "files": ["tests/test_cart.py"],
            "decision": "xFail test documents the known bug tracked in L-0003",
            "rejected_alternatives": [],
        },
        {
            "timestamp": "2026-10-26T11:39:00+00:00",
            "prompt": "New session in a new tool: read the capsule first, then add quantity rollups for receipts",
            "model": "gpt-4.1",
            "tool": "cursor",
            "files": ["cart.py", "tests/test_cart.py"],
            "decision": "Added total_items(); the capsule confirmed rejected approaches; the name-keyed dict stays",
            "rejected_alternatives": [_REJECTED_LIST_ITEMS],
        },
    ]


def _commit_plan() -> List[Tuple[Tuple[str, ...], str, str, str, bool]]:
    """(files, message, date, author, skip_gate) per commit, in story order."""
    return [
        (("cart.py",), "Add shopping cart scaffold", "2026-10-26T09:15:00+00:00", "Ada Chen", True),
        (("cart.py", "tests/test_cart.py"), "Add sales tax handling with tests", "2026-10-26T09:30:00+00:00", "Ben Park", False),
        (("cart.py",), "Introduce holiday discount (bug: double application)", "2026-10-26T10:06:00+00:00", "Ada Chen", True),
        (("receipt.py",), "Add checkout receipt formatter", "2026-10-26T10:45:00+00:00", "Chi Wu", False),
        (("tests/test_cart.py",), "Pin double-discount bug behind xfail test", "2026-10-26T11:10:00+00:00", "Chi Wu", False),
        (("cart.py", "tests/test_cart.py"), "Add quantity rollup from a capsule-restored session", "2026-10-26T11:43:00+00:00", "Ben Park", False),
    ]


# ---------------------------------------------------------------------------
# git helpers (the demo builds a REAL git history through the REAL hooks)
# ---------------------------------------------------------------------------


def _git(repo: Path, args: List[str]) -> str:
    """Run a git command in the demo repo; raise LedgerError when it fails."""
    try:
        return run_git(args, cwd=repo)
    except LedgerError as exc:
        raise LedgerError(f"git {' '.join(args)} failed: {exc}") from exc


def _head_sha(repo: Path) -> Optional[str]:
    try:
        return run_git(["rev-parse", "HEAD"], cwd=repo, check=False).strip() or None
    except LedgerError:
        return None


def _commit(
    repo: Path, message: str, date: str, author: str, *, skip_gate: bool
) -> subprocess.CompletedProcess:
    """Commit staged changes with a fixed identity/dates (+optional bypass)."""
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": author,
            "GIT_AUTHOR_EMAIL": _AUTHOR_EMAIL[author],
            "GIT_COMMITTER_NAME": author,
            "GIT_COMMITTER_EMAIL": _AUTHOR_EMAIL[author],
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        }
    )
    if skip_gate:
        env["LEDGER_SKIP_GATE"] = "1"
    try:
        return subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise LedgerError(f"could not run git commit: {exc}") from exc


def _write_files(repo: Path, mapping: List[Tuple[str, str]]) -> List[str]:
    """Write repo files deterministically (LF endings); return written names."""
    written: List[str] = []
    for rel, content in mapping:
        write_text_lf(repo / rel, content)
        written.append(rel)
    return written


# ---------------------------------------------------------------------------
# DEMO_SCRIPT.md - generated with the real numbers
# ---------------------------------------------------------------------------


def _demo_script(bug_line: int) -> str:
    """The 3-minute presenter script with exact commands (deterministic)."""
    return f"""\
# CodeLedger demo script (~3 minutes)

> "git blame tells you who wrote a line. CodeLedger tells you why the AI wrote it."

One-time setup, from the CodeLedger repo root:

    pip install -e .
    ledger demo
    cd ledger-demo

---

## [0:00-0:25] The hook - the bug hunt

Say: "This repo was built by three AI assistants over one morning. Receipts
are wrong whenever there's a discount. Classic AI code: nobody knows why."

Run:

    ledger blame cart.py:{bug_line}

Say: "One command. The buggy line was written by claude-sonnet-4.5 through
opencode from THIS prompt, and the team bypassed the test gate for that
commit. This is the prompt that caused the bug."

## [0:25-0:55] How that is possible - the log

Run:

    ledger status

Say: "Every AI change is a ledger entry: prompt, model, tool, files,
decision, rejected alternatives, tests included, gate result. Plain files -
no database, no cloud - one JSON object per line in .ledger/decisions.jsonl."

## [0:55-1:35] The gate - live

Say: "Receipt commit four was BLOCKED the first time. Let me replay the gate.
This command creates its own scratch edit - safe on the demo repo."

Run:

    echo "# gate replay" >> cart.py
    ledger log --prompt "gate replay" --model "gpt-4.1" --tool cursor --files cart.py --decision "Replaying the gate for the demo"
    git add cart.py
    git commit -m "Gate replay attempt"

Say: "The pre-commit hook runs the gate: AI source changes need tests. It
names exactly which files need coverage and prints a ready-to-paste prompt
for the AI. This is the enforcement story - the gate blocked commit four
until the assistant's tests arrived."

Run:

    git restore --staged cart.py
    git checkout -- cart.py

Say: "I deliberately keep that scratch entry pending - watch it appear in
the capsule's Open items in a second."

## [1:35-2:15] The capsule - memory that survives tool switches

Run:

    ledger capsule
    cat .ledger/CAPSULE.md

Say: "New AI sessions start from zero. The capsule fixes that: newest
decisions, rejected approaches under 'Don't do this again', recently
changed files, open items. AGENTS.md literally tells every AI assistant to
read this file first - that is how a NEW tool inherits the whole story."

## [2:15-2:45] The UI

Run:

    ledger ui

Say: "Timeline with the whole story; blame with per-line provenance and the
prompt that made each line; the capsule with one-click copy for any AI
tool; health charts for tests, gate and models. Pure standard library -
no CDN, no cloud, no database."

## [2:45-3:00] Close

Say: "git blame tells you WHO. CodeLedger tells you WHY. Decisions on plain
files, tests enforced at commit time, context restored in one capsule -
fully offline, fully deterministic. That's CodeLedger."
"""


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _step_files(step: int) -> List[Tuple[str, str]]:
    """Files to write for story step ``step`` (tests for step 4 come later)."""
    if step == 1:
        return [("cart.py", _cart_v1())]
    if step == 2:
        return [("cart.py", _cart_v2()), ("tests/test_cart.py", _TESTS_BASE)]
    if step == 3:
        return [("cart.py", _cart_v3_buggy())]
    if step == 4:
        return [("receipt.py", _RECEIPT)]
    if step == 5:
        return [("tests/test_cart.py", _tests_v5())]
    if step == 6:
        return [("cart.py", _cart_v4_final()), ("tests/test_cart.py", _tests_v6())]
    raise LedgerError(f"unknown demo step {step}")


def _verify_final_state(repo: Path) -> int:
    """Sanity-check the story the demo promised; returns the buggy line no."""
    entries = store.load_entries(repo)
    if len(entries) != DEMO_ENTRY_COUNT:
        raise LedgerError(
            f"demo self-check failed: expected {DEMO_ENTRY_COUNT} entries, got {len(entries)}"
        )
    for entry in entries:
        if entry.status != "committed" or not entry.commit:
            raise LedgerError(f"demo self-check failed: {entry.id} is not committed")
    broken = [e for e in entries if e.gate_result == "fail"]
    if broken:
        raise LedgerError(
            f"demo self-check failed: {', '.join(e.id for e in broken)} left in 'fail' state"
        )
    bypassed = [e.id for e in entries if e.gate_result == "bypassed"]
    if sorted(bypassed) != ["L-0001", "L-0003"]:
        raise LedgerError(f"demo self-check failed: bypassed entries {bypassed}")
    tests_included = [e.id for e in entries if e.tests_included]
    if sorted(tests_included) != ["L-0002", "L-0004", "L-0005", "L-0006"]:
        raise LedgerError(f"demo self-check failed: tests_included {tests_included}")
    # The hero proof: the buggy line must reveal L-0003.
    final_cart = (repo / "cart.py").read_text(encoding="utf-8")
    line_no = bug_line_of(final_cart)
    outcome = blame_mod.reveal(repo, f"cart.py:{line_no}")
    if not outcome.is_ai or not any(e.id == "L-0003" for e in outcome.entries):
        raise LedgerError(
            f"demo self-check failed: blame of cart.py:{line_no} did not reveal L-0003"
        )
    return line_no


def _rmtree(path: Path) -> None:
    """Remove a repo tree, coping with git's read-only object files (Windows)."""

    def onerror(func, touched, exc_info) -> None:  # noqa: ANN001 - shutil signature
        try:
            os.chmod(touched, stat.S_IWRITE)
            func(touched)
        except OSError:
            pass  # keep going; --force teardown must not half-fail loudly

    shutil.rmtree(path, onerror=onerror)


def run_demo(cwd: Path, *, force: bool = False) -> Path:
    """Create ``<cwd>/ledger-demo`` through the real hooks; returns its path."""
    if not git_available():
        raise LedgerError(
            "git is not installed or not on PATH; the demo builds a real repo."
        )
    target = cwd / DEMO_DIR_NAME
    if target.exists():
        if not force:
            raise LedgerError(
                f"'{DEMO_DIR_NAME}' already exists here. Use --force to rebuild."
            )
        _rmtree(target)
    target.mkdir(parents=True)

    console.print(f"Building demo repo at [bold]{target}[/bold]")
    _git(target, ["init", "--quiet", "-b", "main"])
    for key, value in (
        ("core.autocrlf", "false"),
        ("core.safecrlf", "false"),
        ("commit.gpgsign", "false"),
    ):
        _git(target, ["config", key, value])

    report = initcmd.run_init(target)
    hooks = report.hooks
    console.print(
        f"  hooks: prepare-commit-msg={hooks.get('prepare-commit-msg')}, "
        f"pre-commit={hooks.get('pre-commit')}, post-commit={hooks.get('post-commit')}"
    )
    # Keep the demo's `git status` spotless: the ledger stays a local folder.
    write_text_lf(target / ".gitignore", "# demo keeps the ledger local\n.ledger/\n")

    plan = _commit_plan()
    specs = _entry_specs()
    entries: List[Dict[str, object]] = []

    for step, (files, message, date, author, skip_gate) in enumerate(plan, start=1):
        spec = specs[step - 1]
        entry = store.add_entry(
            target,
            prompt=str(spec["prompt"]),
            model=str(spec["model"]),
            tool=str(spec["tool"]),
            files=[str(f) for f in spec["files"]],
            decision=str(spec["decision"]),
            rejected_alternatives=[dict(a) for a in spec["rejected_alternatives"]],  # type: ignore[arg-type]
            timestamp=str(spec["timestamp"]),
        )
        entries.append(entry.to_dict())
        written = _write_files(target, _step_files(step))
        console.print(f"  {entry.id} staged {', '.join(written)}")
        _git(target, ["add", "--", *files])

        if step == 4:
            # Story beat: the receipt commit gets blocked until tests arrive.
            before = _head_sha(target)
            attempt = _commit(target, message, date, author, skip_gate=False)
            if attempt.returncode == 0:
                raise LedgerError(
                    "demo self-check failed: the receipt commit should have been "
                    "blocked by the gate but it went through"
                )
            console.print("  gate BLOCKED the receipt commit (no tests) - writing tests...")
            write_text_lf(target / "tests/test_receipt.py", _RECEIPT_TESTS)
            _git(target, ["add", "--", "tests/test_receipt.py"])
            after = _head_sha(target)
            if after != before:
                raise LedgerError("demo self-check failed: a commit slipped in unexpectedly")
            proc = _commit(target, message, date, author, skip_gate=False)
            if proc.returncode != 0 or _head_sha(target) == before:
                raise LedgerError(
                    "demo self-check failed: the receipt commit with tests failed"
                )
        else:
            proc = _commit(target, message, date, author, skip_gate=skip_gate)
            if proc.returncode != 0:
                output = (proc.stdout or "") + (proc.stderr or "")
                raise LedgerError(f"git commit failed during demo step {step}:\n{output}")

    # Self-check the promised story, then produce the capsule + script.
    line_no = _verify_final_state(target)

    entries_final = store.load_entries(target)
    text, capsule_path = capsule_mod.write_capsule(entries_final, target, branch="main")
    del capsule_path
    script_path = target / "DEMO_SCRIPT.md"
    write_text_lf(script_path, _demo_script(line_no))

    console.print()
    console.print(
        f"Demo complete: {DEMO_ENTRY_COUNT} entries, {len(entries_final)} committed, "
        f"capsule {len(text.splitlines())} lines, script at DEMO_SCRIPT.md"
    )
    console.print()
    console.print("[bold]The hero reveal (what the presenter runs next):[/bold]")
    outcome = blame_mod.reveal(target, f"cart.py:{line_no}")
    blame_mod.render_reveal(console, outcome)
    console.print()
    console.print("Explore it yourself:")
    console.print("  cd ledger-demo")
    blame_cmd = f"  ledger blame cart.py:{line_no}".ljust(41)
    console.print(f"{blame_cmd}# the prompt behind the bug")
    console.print("  ledger status                          # 6 entries, gate stats")
    console.print("  ledger capsule                         # context for any AI tool")
    console.print("  ledger ui                              # local dashboard at /127.0.0.1")
    return target
