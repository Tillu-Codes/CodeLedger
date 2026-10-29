"""Regenerate ``api/data/*.json`` for the Vercel deployment.

The hosted dashboard is read-only: it cannot run ``git blame`` on Vercel
(no ``.git`` inside the function bundle).  Instead we precompute every API
payload from the ``ledger-demo`` repository locally and ship the results as
JSON next to the serverless functions.

Run from the repo root:  python tools/gen_vercel_data.py
Uses the working copy of ledger-demo; commit the regenerated files.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from codeledger import webui  # noqa: E402
from codeledger.blame import reveal, reveal_to_dict  # noqa: E402
from codeledger.errors import LedgerError  # noqa: E402

REPO = ROOT / "ledger-demo"
OUT = ROOT / "api" / "data"


def dump(name: str, payload: Any) -> None:
    """One compact, stable-bytes JSON file."""
    path = OUT / name
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(f"  wrote {path.name} ({path.stat().st_size} bytes)")


def main() -> int:
    if not (REPO / ".ledger" / "decisions.jsonl").is_file():
        print(f"no demo ledger found at {REPO}", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    health = webui.api_health(REPO)
    entries = webui.api_entries(REPO)
    capsule = webui.api_capsule(REPO)
    files = webui.api_files(REPO)
    dump("health.json", health)
    dump("entries.json", entries)
    dump("capsule.json", capsule)
    dump("files.json", files)

    blame: Dict[str, Any] = {}
    for rel in files["files"]:
        try:
            full = webui.api_blame_full(REPO, rel)
        except webui.ApiError as exc:
            blame[rel] = {"error": exc.message}
            print(f"  {rel}: error ({exc.message})")
            continue
        lines: Dict[str, Any] = {}
        for line_no in range(1, len(full["lines"]) + 1):
            try:
                lines[str(line_no)] = reveal_to_dict(reveal(REPO, f"{rel}:{line_no}"))
            except LedgerError as exc:
                lines[str(line_no)] = {"error": str(exc)}
        blame[rel] = {"full": full, "lines": lines}
        print(f"  {rel}: {len(full['lines'])} lines revealed")
    dump("blame.json", blame)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
