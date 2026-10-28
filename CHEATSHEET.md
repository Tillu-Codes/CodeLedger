# CodeLedger — Cheat Sheet

> Run everything from inside your repo. Also works as `python -m codeledger <command>`.

## Commands

| Command | What it does | Usage |
|---|---|---|
| `init` | Set up: ledger + hooks + AGENTS.md section | `ledger init` |
| `log` | Record an AI change, prints new ID | `ledger log -p "prompt" -m model -t tool -f file.py -d "decision" -r "idea::reason"` |
| `blame` | Full story for ONE line (hero) | `ledger blame cart.py:65`  ·  `--json` for machine-readable |
| `gate` | Block AI source committed without tests | `ledger gate` (runs automatically on commit) |
| `capsule` | Write `CAPSULE.md` context snapshot | `ledger capsule`  ·  `--stdout` to print only |
| `redact` | Replace secrets with `<SECRET_1>` placeholders | `ledger redact file.txt`  ·  `--check` = report only  ·  `--out f2.txt` |
| `restore` | Swap placeholders back to real secrets | `ledger restore file.txt` |
| `status` | Quick totals: entries, gate stats | `ledger status` |
| `ui` | Local dashboard at http://127.0.0.1:8765 | `ledger ui --port 8123 --no-browser` |
| `demo` | Build demo repo + presenter script | `ledger demo`  ·  `--force` = rebuild |
| `hook` | Internal — called by git hooks, never run by hand | — |

## The flow you almost always use

```console
ledger log -p "prompt" -m model -t tool --files x.py -d "title" -r "idea::reason"
git add . && git commit        # hooks: gate check + Ledger-ID stamp + auto-link
ledger blame x.py:42           # the full receipt for that line
ledger capsule                 # paste into any NEW AI session
ledger ui                      # browser dashboard (localhost only)
```

## Flags of `log`

| Flag | Means |
|---|---|
| `-p` | The prompt you gave the AI (required) |
| `-m` | Model, e.g. `claude-sonnet-4.5` (required) |
| `-t` | Tool, e.g. `opencode`, `cursor` (required) |
| `-d` | One-line decision (required) |
| `-f` | Changed file — repeatable |
| `-r` | Rejected idea — `approach::reason` — repeatable |

## One rule to remember

| Setting it | Effect |
|---|---|
| `LEDGER_SKIP_GATE=1` before commit | Skips the test gate — recorded in the ledger as **bypassed**, never secret |
