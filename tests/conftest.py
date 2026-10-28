"""Shared fixtures for the CodeLedger test suite.

Tests run against real, throwaway git repositories (the whole point of
CodeLedger is provenance in git), so the heavy lifting is a ``repo`` fixture
that creates a deterministic repo in a pytest temp dir, plus CLI helpers.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import pytest
from typer.testing import CliRunner

from codeledger import initcmd, store
from codeledger.gitutil import run_git
from codeledger.util import write_text_lf

__all__ = ["repo", "inited_repo", "cli", "gateway", "git", "HOOKS"]


@pytest.fixture(autouse=True)
def clean_gate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bypass flag must never leak between tests (or into the suite)."""
    monkeypatch.delenv("LEDGER_SKIP_GATE", raising=False)
    monkeypatch.delenv("GIT_PAGER", raising=False)


@pytest.fixture(autouse=True)
def git_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep git from discovering any repository above the test's temp tree.

    On this machine even the user's home directory may be a git repository,
    which would make every pytest temp dir 'inside a repo'; the ceiling pins
    discovery so an un-initialised directory stays genuinely un-initialised.
    """
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


@pytest.fixture
def cli() -> CliRunner:
    """Typer's CLI runner (captures output, gives exit codes)."""
    return CliRunner()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fresh git repository with one human commit; cwd IS the repo root."""
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, ["init", "--quiet", "-b", "main"])
    for key, value in (
        ("user.name", "Test User"),
        ("user.email", "test@example.com"),
        ("core.autocrlf", "false"),
        ("core.safecrlf", "false"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        _git(root, ["config", key, value])
    write_text_lf(root / "README.md", "human readme\n")
    _git(root, ["add", "README.md"])
    _git(root, ["commit", "--no-verify", "--quiet", "-m", "initial"])
    monkeypatch.chdir(root)
    return root


@pytest.fixture
def inited_repo(repo: Path) -> Path:
    """A repo that also ran ``ledger init`` (hooks installed, ledger empty)."""
    initcmd.run_init(repo)
    return repo


def _git(root: Path, args: List[str]) -> str:
    """Run git in ``root``; raise AssertionError on failure (test bug, not UX)."""
    proc = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}"
        )
    return proc.stdout


class GitSpy:
    """Small git/text helper bundle handed to tests as the ``gateway`` fixture."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # -- files --------------------------------------------------------

    def write(self, rel: str, text: str) -> None:
        """Write a repo file with LF endings (deterministic on every OS)."""
        write_text_lf(self.root / rel, text)

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    # -- git ----------------------------------------------------------

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Raw git; use ``--no-verify`` / hooks deliberately in each test."""
        proc = subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and proc.returncode != 0:
            raise AssertionError(f"git {args}: {proc.stdout}{proc.stderr}")
        return proc

    def stage(self, *rels: str) -> None:
        self.git("add", "--", *rels)

    def commit(self, message: str, *rels: str, verify: bool = False, quiet: bool = True):
        """Stage ``rels`` (if given) and commit; hooks run unless verify=False.

        Returncode is NOT checked (gate-blocked commits are expected); pick
        ``check=...`` via :meth:`git` for hard failures.
        """
        if rels:
            self.stage(*rels)
        args = ["commit", "-m", message]
        if not verify:
            args.append("--no-verify")
        if quiet:
            args.append("--quiet")
        return self.git(*args, check=False)

    def head(self, ref: str = "HEAD") -> str:
        return run_git(["rev-parse", ref], self.root).strip()

    def trailer(self, sha: str = "HEAD") -> str:
        """The ``Ledger-ID`` trailer value of a commit ('' when absent)."""
        out = run_git(
            ["log", "-1", "--format=%(trailers:key=Ledger-ID,valueonly,separator=%x3B)" , sha],
            self.root,
            check=False,
        ).strip()
        return out

    # -- ledger -------------------------------------------------------

    def entries(self) -> List[store.Entry]:
        return store.load_entries(self.root)

    def entry(self, entry_id: str) -> Optional[store.Entry]:
        for entry in self.entries():
            if entry.id == entry_id:
                return entry
        return None

    def entry_ids(self) -> List[str]:
        return [e.id for e in self.entries()]


@pytest.fixture
def gateway(repo: Path) -> GitSpy:
    """File/git/ledger helpers bound to the ``repo`` fixture."""
    return GitSpy(repo)


# Real hooks spawn ``ledger`` (or ``python -m codeledger``).  Skipping hook
# tests on machines without the package importable would hide regressions,
# so the suite always runs them through the *same* interpreter as pytest.
@pytest.fixture
def ledger_argv() -> List[str]:
    """argv prefix that runs the CodeLedger CLI under pytest's interpreter."""
    return [sys.executable, "-m", "codeledger"]
