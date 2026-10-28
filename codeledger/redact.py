"""``ledger redact`` / ``ledger restore`` - strip secrets before logging code.

Detected patterns (with numbered placeholders, e.g. ``<SECRET_1>``,
``<EMAIL_1>``, ``<PHONE_1>``):

* AWS access keys, GitHub tokens, Google API keys, Slack tokens,
  Stripe live/test keys,
* JWTs, PEM private-key blocks, bearer tokens,
  passwords inside ``scheme://user:pass@host`` URLs,
* generic ``api_key | secret | password | token = "..."`` assignments,
  including unquoted YAML/dotenv style values (``password: ...``),
* emails and phone numbers (incl. ``+91`` Indian formats).

The reverse map is stored in ``.ledger/redact_map.json`` (gitignored by
``ledger init``) so ``ledger restore <file>`` can swap placeholders back.
``--check`` mode only reports counts and exits 1 if anything was found.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from codeledger import store
from codeledger.errors import LedgerError
from codeledger.util import read_json, write_json_atomic

__all__ = [
    "RedactOutcome",
    "redact_text",
    "restore_text",
    "scan_counts",
    "PLACEHOLDER_RE",
    "FAMILIES",
]

PLACEHOLDER_RE = re.compile(r"<(SECRET|EMAIL|PHONE)_(\d+)>")

FAMILIES: Tuple[str, ...] = ("SECRET", "EMAIL", "PHONE")


# ---------------------------------------------------------------------------
# Detection rules
# ---------------------------------------------------------------------------


@dataclass
class Rule:
    """One detection pattern with its placeholder family."""

    name: str
    family: str
    priority: int  # lower wins overlaps -> deterministic resolution
    regex: re.Pattern
    value_group: Optional[str] = None  # span to replace when set, else full match


_RULES: List[Rule] = [
    Rule(
        "pem",
        "SECRET",
        0,
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
            r"[\s\S]*?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
        ),
    ),
    Rule(
        "jwt",
        "SECRET",
        1,
        re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    ),
    Rule("aws-key", "SECRET", 2, re.compile(r"\bA[SK]IA[0-9A-Z]{16}\b")),
    Rule("google-api", "SECRET", 3, re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    Rule("github-token", "SECRET", 4, re.compile(r"\bgh[pousr]_[A-Za-z0-9]{25,}\b")),
    Rule("stripe-key", "SECRET", 5, re.compile(r"\b(?:sk|rk)_(?:live|test)_[0-9a-zA-Z]{16,}\b")),
    Rule("slack-token", "SECRET", 6, re.compile(r"\bxox[baposr]-[A-Za-z0-9-]{10,}\b")),
    Rule(
        "uri-password",
        "SECRET",
        7,
        re.compile(
            r"(?i)\b[a-z][a-z0-9+.-]*://[^\s:/@]+:"
            r"(?P<value>[^\s/@]+)@[^\s/]*"
        ),
        value_group="value",
    ),
    Rule(
        "generic-assignment",
        "SECRET",
        8,
        re.compile(
            r"(?i)(?<![A-Za-z0-9_])"
            r"(?:api[_-]?key|apikey|secret|consumer[_-]?secret|client[_-]?secret|"
            r"access[_-]?token|auth[_-]?token|password|passwd|pwd|token)"
            # separators up to ':'/'=', all optional, never '=='
            r"\b[\"']?\]?[\"']?\s*[:=](?<!==)\s*"
            r"(?P<value>[\"'][^\"'\n]{4,}[\"'])"
        ),
        value_group="value",
    ),
    Rule(
        "generic-unquoted",
        "SECRET",
        9,
        re.compile(
            r"(?i)(?<![A-Za-z0-9_])"
            r"(?:api[_-]?key|apikey|secret|consumer[_-]?secret|client[_-]?secret|"
            r"access[_-]?token|auth[_-]?token|password|passwd|pwd|token)"
            r"\b[\"']?\]?[\"']?\s*[:=][ \t]+(?<!==)"
            r"(?P<value>[A-Za-z0-9._~+/=\-]{16,})(?![A-Za-z0-9._~+/=\-])"
        ),
        value_group="value",
    ),
    Rule(
        "bearer",
        "SECRET",
        10,
        re.compile(r"(?i)\bbearer[ \t]+(?P<value>[A-Za-z0-9._~+/\-=]{16,})"),
        value_group="value",
    ),
    Rule(
        "email",
        "EMAIL",
        11,
        re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}"),
    ),
    Rule(
        "phone-plus91",
        "PHONE",
        12,
        re.compile(r"\+91[-\s]?(?:[6-9]\d{9}|[6-9]\d{5}[-\s]\d{5})(?!\d)"),
    ),
    Rule(
        "phone-blocks",
        "PHONE",
        13,
        # US-style groups: (415) 555-0100, 415-555-0100, 415 555 0100
        re.compile(r"(?<![\w-])(?:\(\d{3}\)?[-\s]|\d{3}[-\s])\d{3}[-\s]?\d{4}(?!\d)"),
    ),
    Rule(
        "phone-91-bare",
        "PHONE",
        14,
        re.compile(r"(?<![\w-])[6-9]\d{4}[-\s]\d{5}(?!\d)"),
    ),
]

#: Local parts / domains that look like documentation placeholders, not real
#: people - filtered to avoid false positives on ordinary prose.
_NOISE_LOCALS = {
    "user", "test", "testuser", "name", "email", "example", "foo", "mail",
    "contact", "admin", "username", "yourname", "you", "someone", "hello",
    "info", "noreply",
}
_NOISE_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "yourdomain.com",
    "mail.com", "email.com", "foo.com", "test.com", "mail.example",
    # remote/bot-style addresses that carry no credentials
    "github.com", "gitlab.com", "bitbucket.org",
}


@dataclass
class _Candidate:
    """One raw regex hit, before overlap resolution."""

    priority: int
    start: int
    end: int
    rule: str
    family: str
    original: str

    def overlaps(self, other: "_Candidate") -> bool:
        return not (self.end <= other.start or self.start >= other.end)


def _is_doc_example_email(value: str) -> bool:
    """Filter documentation-style email addresses (placeholder mail)."""
    value_l = value.lower()
    local, sep, domain = value_l.rpartition("@")
    if not sep:
        return False
    if domain in _NOISE_DOMAINS or domain.endswith(".example"):
        return True
    if local in _NOISE_LOCALS:
        return True
    return False


def _candidates(text: str) -> List[_Candidate]:
    """Every raw hit from every rule (possibly overlapping)."""
    out: List[_Candidate] = []
    for rule in _RULES:
        for match in rule.regex.finditer(text):
            if rule.family == "EMAIL" and _is_doc_example_email(match.group(0)):
                continue
            span = match.span(rule.value_group) if rule.value_group else match.span()
            if span[1] <= span[0]:
                continue
            out.append(
                _Candidate(
                    priority=rule.priority,
                    start=span[0],
                    end=span[1],
                    rule=rule.name,
                    family=rule.family,
                    original=text[span[0] : span[1]],
                )
            )
    return out


def _resolve_overlaps(cands: List[_Candidate]) -> List[_Candidate]:
    """Deterministic overlap resolution.

    Candidates are considered in (priority, position, longest-first) order;
    a candidate that overlaps an already-kept span is skipped, so the
    higher-priority (more specific) pattern wins.
    """
    ordered = sorted(
        cands, key=lambda c: (c.priority, c.start, -(c.end - c.start), c.rule)
    )
    kept: List[_Candidate] = []
    for cand in ordered:
        if any(keep.overlaps(cand) for keep in kept):
            continue
        kept.append(cand)
    kept.sort(key=lambda c: (c.start, -c.end, c.priority))
    return kept


# ---------------------------------------------------------------------------
# Redaction pass
# ---------------------------------------------------------------------------


@dataclass
class RedactOutcome:
    """Result of one redaction pass."""

    text: str
    counts: Dict[str, int]
    total: int
    map_added: int  # placeholders persisted into redact_map.json


def _load_map(path: Path) -> Dict[str, str]:
    """Read the reverse map; missing/corrupt files yield an empty dict."""
    data = read_json(path)
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _persist_map(path: Path, mapping: Dict[str, str]) -> None:
    """Write the reverse map atomically; create folders as needed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(path, mapping)
    except OSError as exc:
        raise LedgerError(f"could not write redaction map: {exc}") from exc


def _next_family_index(existing: Dict[str, str], family: str) -> int:
    """One past the biggest index already used for ``family`` in the map."""
    highest = 0
    for key in existing:
        match = PLACEHOLDER_RE.fullmatch(key)
        if match and match.group(1) == family:
            highest = max(highest, int(match.group(2)))
    return highest + 1


@dataclass
class _Resolved:
    """A kept match together with its assigned placeholder."""

    start: int
    end: int
    family: str
    placeholder: str
    original: str


def redact_text(
    text: str,
    ledger_dir: Path,
    map_path: Optional[Path] = None,
) -> RedactOutcome:
    """Replace secret spans with numbered placeholders and persist the map.

    ``ledger_dir`` is the repo's ``.ledger`` folder; ``map_path`` overrides
    where the reverse map is written (tests and stdin mode use scratch
    locations). Numbering continues from the map's existing placeholders, so
    repeated runs can never collide.
    """
    if map_path is None:
        map_path = ledger_dir / store.REDACT_MAP_FILE
    existing = _load_map(map_path)

    kept = _resolve_overlaps(_candidates(text))
    next_index = {fam: _next_family_index(existing, fam) for fam in FAMILIES}
    resolved: List[_Resolved] = []
    for cand in kept:  # kept is in document order
        placeholder = f"<{cand.family}_{next_index[cand.family]}>"
        next_index[cand.family] += 1
        resolved.append(
            _Resolved(
                start=cand.start,
                end=cand.end,
                family=cand.family,
                placeholder=placeholder,
                original=cand.original,
            )
        )

    out = text
    for res in sorted(resolved, key=lambda r: r.start, reverse=True):
        out = out[: res.start] + res.placeholder + out[res.end :]

    counts: Dict[str, int] = {fam: 0 for fam in FAMILIES}
    for res in resolved:
        counts[res.family] += 1

    merged = dict(existing)
    for res in resolved:
        merged[res.placeholder] = res.original
    _persist_map(map_path, merged)
    return RedactOutcome(text=out, counts=counts, total=len(resolved), map_added=len(resolved))


def scan_counts(text: str) -> Dict[str, int]:
    """Counts ``--check`` would report, without writing anything."""
    kept = _resolve_overlaps(_candidates(text))
    counts: Dict[str, int] = {fam: 0 for fam in FAMILIES}
    for cand in kept:
        counts[cand.family] += 1
    return counts


def restore_text(text: str, map_path: Path) -> Tuple[str, int, int]:
    """Swap ``<FAMILY_n>`` placeholders back using the reverse map.

    Returns ``(new_text, replaced, unknown)``; unknown placeholders are left
    untouched and counted, never destroyed.
    """
    mapping = _load_map(map_path)
    replaced = 0
    unknown = 0

    def _swap(match: "re.Match[str]") -> str:
        nonlocal replaced, unknown
        token = match.group(0)
        original = mapping.get(token)
        if original is None:
            unknown += 1
            return token
        replaced += 1
        return original

    new_text = PLACEHOLDER_RE.sub(_swap, text)
    return new_text, replaced, unknown
