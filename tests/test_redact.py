"""``ledger redact`` / ``restore``: rules, false positives, map continuity, round trip."""
from __future__ import annotations

from pathlib import Path

import pytest

from codeledger import redact
from codeledger.store import add_entry
from codeledger.cli import app

from conftest import GitSpy

SECRETS = {
    "aws": "AKIAIOSFODNN7EXAMPLE",            # AKIA + 16 chars, Amazon's docs sample
    "github": "ghp_0123456789abcdef0123456789abcdef1234",
    "google": "AIza" + "A" * 35,
    "slack": "xoxb-1234567890AB-qrstuvwxyz12",
    "stripe": "sk_live_51H8xQabcDEFghiJKlmn",
    "jwt": "eyJhbGciOi.Jnsb2h.R3OUNzI2Vawk",
}


# ---------------------------------------------------------------------------
# detection battery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("secret", list(SECRETS.values()))
def test_token_rules(secret):
    assert redact.scan_counts(f"k = '{secret}'\n")["SECRET"] == 1


def test_pem_block():
    text = "\n".join([
        "cert = '''",
        "-----BEGIN RSA PRIVATE KEY-----",
        "MIIEowIBAAKCAQEA1234567890abcd",
        "-----END RSA PRIVATE KEY-----",
        "'''",
    ])
    assert redact.scan_counts(text)["SECRET"] == 1


def test_uri_password(tmp_path):
    line = "db = 'postgres://admin:hunter2super@db.example.com/prod'"
    assert redact.scan_counts(line)["SECRET"] == 1
    outcome = redact.redact_text(line, tmp_path)
    assert "hunter2super" not in outcome.text
    assert "<SECRET_" in outcome.text


def test_generic_assignment_variants():
    for line in (
        'password = "correct horse"',
        "auth_token: 'abcdef0123456789'",
        'client_secret="s3cr3tworth"',
        "PWD='notdummyvalue'",
        "consumer_secret = 'verysecretyeah'",
        "password: hunter2supersecret",     # YAML/dotenv unquoted style
        'token = "0xVmzCDDlonghexvalue00"',
    ):
        assert redact.scan_counts(line)["SECRET"] == 1, line


def test_bearer_token():
    line = "Authorization: Bearer yNzY4-MfF8EeJ.kQs9_O1aBCd2Ef3Gh4Ij5Kl6Mn7 ==\n"
    assert redact.scan_counts(line)["SECRET"] == 1


def test_emails_and_phones():
    text = "\n".join([
        "reach = 'dana.dee@corp-sub.krd'",
        "indian = '+91 9876543210'",
        "us = '(415) 555-0100'",
        "spaces = '98765 43210'",
    ])
    counts = redact.scan_counts(text)
    assert counts["EMAIL"] == 1
    assert counts["PHONE"] == 3


# ---------------------------------------------------------------------------
# false positives
# ---------------------------------------------------------------------------


def test_no_false_positives_battery():
    text = "\n".join([
        "if config.token == stored.token:",
        "    ssh = 'ssh -T git@github.com'",
        "call(price, 0.0825, '+91')",
        "contact = 'hello@example.com'",
        "greet(user='admin')",
        "fixpoint = 14255",
        "version = 2.0",
        "token = 'abc'",          # 3-char value: below the minimum length
        "date = '2026-10-28'",    # dates are not phones
        "ref = 'issue #12345'",
    ])
    counts = redact.scan_counts(text)
    assert sum(counts.values()) == 0, counts


def test_variable_comparison_fine_but_real_literal_caught():
    assert redact.scan_counts("x = a.token == b.token\n")["SECRET"] == 0
    assert redact.scan_counts("x = token == 'ghp_0123456789abcdef0123456789abcdef1234'\n")["SECRET"] == 1


# ---------------------------------------------------------------------------
# map continuity + overlap resolution
# ---------------------------------------------------------------------------


def test_numbering_continues_across_runs(tmp_path):
    map_path = tmp_path / "map.json"
    first = redact.redact_text("e = 'a.one@corp.io'\n", tmp_path, map_path=map_path)
    second = redact.redact_text("e = 'b.two@corp.io'\n", tmp_path, map_path=map_path)
    assert "<EMAIL_1>" in first.text
    assert "<EMAIL_2>" in second.text


def test_overlap_generic_assignment_beats_email(tmp_path):
    outcome = redact.redact_text("password = 'dana@corp.io'\n", tmp_path)
    assert outcome.counts["SECRET"] == 1
    assert outcome.counts["EMAIL"] == 0
    assert "dana@corp.io" not in outcome.text
    assert "<SECRET_1>" in outcome.text


def test_restore_round_trip(tmp_path):
    map_path = tmp_path / "map.json"
    original = "the token is 'ghp_0123456789abcdef0123456789abcdef1234', mail dana@corp.io\n"
    redacted = redact.redact_text(original, tmp_path, map_path=map_path).text
    assert original != redacted
    assert "ghp_0123456789" not in redacted
    restored, replaced, unknown = redact.restore_text(redacted, map_path)
    assert unknown == 0 and replaced == 2
    assert restored == original


def test_restore_unknown_placeholders_left(tmp_path):
    restored, replaced, unknown = redact.restore_text("x = <PHONE_77>\n", tmp_path / "map.json")
    assert replaced == 0 and unknown == 1
    assert "<PHONE_77>" in restored


def test_corrupt_map_tolerated(tmp_path):
    map_path = tmp_path / "map.json"
    map_path.write_bytes(b"{not json")
    outcome = redact.redact_text("k = 'ghp_0123456789abcdef0123456789abcdef1234'\n", tmp_path, map_path=map_path)
    assert "<SECRET_1>" in outcome.text  # numbering starts fresh after corruption


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def test_cli_redact_in_place_and_restore_round_trip(inited_repo, cli, gateway: GitSpy):
    gateway.write("secrets.py", "k = 'ghp_0123456789abcdef0123456789abcdef1234'\nmail = 'dana@corp.io'\n")
    add_entry(inited_repo, prompt="p", model="m", tool="t", files=["secrets.py"], decision="d")
    result = cli.invoke(app, ["redact", "secrets.py"])
    assert result.exit_code == 0, result.output
    redacted = gateway.read("secrets.py")
    assert "ghp_0123456789" not in redacted
    assert "<SECRET_1>" in redacted and "<EMAIL_1>" in redacted
    # the pending entry lists secrets.py -> redactions mirrored onto it
    assert gateway.entry("L-0001").redactions == 2
    assert (inited_repo / ".ledger" / "redact_map.json").is_file()

    result = cli.invoke(app, ["restore", "secrets.py"])
    assert result.exit_code == 0
    assert gateway.read("secrets.py") == "k = 'ghp_0123456789abcdef0123456789abcdef1234'\nmail = 'dana@corp.io'\n"


def test_cli_check_exit_codes(inited_repo, cli, gateway: GitSpy):
    gateway.write("clean.py", "total = 7 + 3\n")
    assert cli.invoke(app, ["redact", "--check", "clean.py"]).exit_code == 0
    gateway.write("dirty.py", "k = 'ghp_0123456789abcdef0123456789abcdef1234'\n")
    assert cli.invoke(app, ["redact", "--check", "dirty.py"]).exit_code == 1
    # --check never writes a map or rewrites the file
    assert not (inited_repo / ".ledger" / "redact_map.json").exists()
    assert "ghp_0123456789" in gateway.read("dirty.py")
