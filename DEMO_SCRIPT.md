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

    ledger blame cart.py:65

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
