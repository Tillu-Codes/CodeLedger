"""``ledger init``: bootstrap, idempotency and protecting existing hooks."""
from __future__ import annotations

import pytest

from codeledger import initcmd, store
from codeledger.errors import LedgerError
from codeledger.util import write_text_lf

HOOKS = ("prepare-commit-msg", "pre-commit", "post-commit")


def test_bootstrap_creates_everything(repo):
    report = initcmd.run_init(repo)
    assert report.created_ledger and report.created_jsonl
    assert report.gitignore_updated and report.agents_created
    assert all(report.hooks[h] == "created" for h in HOOKS)
    ledger_dir = repo / ".ledger"
    assert ledger_dir.is_dir()
    assert store.load_entries(repo) == []
    contents = (repo / ".gitignore").read_text("utf-8")
    assert initcmd.GITIGNORE_ENTRY in contents and initcmd.GITIGNORE_MARKER in contents
    agents = (repo / "AGENTS.md").read_text("utf-8")
    assert initcmd.AGENTS_BEGIN in agents and initcmd.AGENTS_END in agents
    for hook in HOOKS:
        text = (repo / ".git" / "hooks" / hook).read_text("utf-8")
        assert hook in text  # managed block prepared for this hook name
        assert text.startswith("#!/bin/sh\n")


def test_second_init_is_noop(repo):
    initcmd.run_init(repo)
    before = (repo / ".ledger/decisions.jsonl").read_bytes()
    report = initcmd.run_init(repo)
    assert not report.created_ledger and not report.created_jsonl
    assert not report.gitignore_updated and not report.agents_created
    assert all(report.hooks[h] == "skipped" for h in HOOKS)
    assert (repo / ".ledger/decisions.jsonl").read_bytes() == before


def test_existing_contents_are_kept(repo):
    write_text_lf(repo / ".gitignore", "my-garbage/\n")
    write_text_lf(repo / "AGENTS.md", "# Project rules\n\nBe nice.\n")
    initcmd.run_init(repo)
    ignore = (repo / ".gitignore").read_text("utf-8")
    assert ignore.startswith("my-garbage/\n")
    agents = (repo / "AGENTS.md").read_text("utf-8")
    assert agents.startswith("# Project rules\n\nBe nice.")
    assert initcmd.AGENTS_BEGIN in agents


def test_manager_section_is_replaced_not_duplicated(repo):
    initcmd.run_init(repo)
    agents = repo / "AGENTS.md"
    # Simulate a stale (older) managed section
    text = agents.read_text("utf-8")
    stale = text.replace("Be nice", "x").replace("4. Ship tests", "4. OLD RULE")
    agents.write_text(stale, encoding="utf-8")
    report = initcmd.run_init(repo)
    assert report.agents_updated
    final = agents.read_text("utf-8")
    assert final.count(initcmd.AGENTS_BEGIN) == 1
    assert initcmd.AGENTS_BEGIN in final


def test_existing_hook_is_appended_not_overwritten(repo):
    hook_path = repo / ".git" / "hooks" / "pre-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_lf(hook_path, "#!/bin/sh\necho custom-check\n")
    report = initcmd.run_init(repo)
    assert report.hooks["pre-commit"] == "appended"
    text = hook_path.read_text("utf-8")
    assert text.startswith("#!/bin/sh\necho custom-check")
    assert "BEGIN CODELEDGER" in text
    assert "ledger hook pre-commit" in text


def test_hook_without_shebang_is_refused(repo):
    hook_path = repo / ".git" / "hooks" / "post-commit"
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    write_text_lf(hook_path, "echo no shebang\n")
    with pytest.raises(LedgerError, match="shebang"):
        initcmd.run_init(repo)


def test_init_outside_git_repo_fails(tmp_path, monkeypatch):
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)
    with pytest.raises(LedgerError, match="git repository|git init"):
        initcmd.run_init(outside)


def test_gitignore_never_duplicates(repo):
    initcmd.run_init(repo)
    initcmd.run_init(repo)
    text = (repo / ".gitignore").read_text("utf-8")
    assert text.count(initcmd.GITIGNORE_ENTRY) == 1
