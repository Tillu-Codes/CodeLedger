"""The local web dashboard: endpoints, payloads and traversal guards."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from codeledger import hooks
from codeledger.store import add_entry
from codeledger.webui import build_server, safe_file, ApiError

from conftest import GitSpy


@pytest.fixture
def port(inited_repo: Path, gateway: GitSpy):
    """A repo with one committed AI entry; the server bound to a free port."""
    add_entry(inited_repo, prompt="prompt-one", model="m1", tool="t1",
              files=["adder.py"], decision="plain adder",
              rejected_alternatives=[{"approach": "class X", "reason": "why"}])
    gateway.write("adder.py", "def add(a, b):\n    return a + b  # line two\n")
    gateway.commit("feat: adder\n\nLedger-ID: L-0001\n", "adder.py")
    hooks.run_hook("post-commit", [], inited_repo)
    srv = build_server(inited_repo, 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def get(path: str, port: int):
    return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)


def body(response) -> dict:
    return json.loads(response.read().decode("utf-8"))


def plain(response) -> str:
    return response.read().decode("utf-8")


# ---------------------------------------------------------------------------
# happy endpoints
# ---------------------------------------------------------------------------


def test_index_served(port):
    response = get("/", port)
    assert response.status == 200
    text = plain(response)
    assert "CodeLedger" in text and "Timeline" in text


def test_health_shape(port):
    data = body(get("/api/health", port))
    assert data["ok"] is True and data["initialized"] is True
    assert data["counts"]["total"] == 1
    assert "branch" in data and "models" in data


def test_entries_and_single_entry(port):
    data = body(get("/api/entries", port))
    assert data["counts"]["committed"] == 1
    assert data["entries"][0]["id"] == "L-0001"
    one = body(get("/api/entry/L-0001", port))
    assert one["prompt"] == "prompt-one"


def test_entry_404_is_json(port):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get("/api/entry/L-9999", port)
    assert exc.value.code == 404
    assert json.loads(exc.value.read().decode("utf-8"))["error"]


def test_blame_line_and_full(port):
    one = body(get("/api/blame?file=adder.py&line=2", port))
    assert one["is_ai"] is True
    assert one["entries"][0]["id"] == "L-0001"
    full = body(get("/api/blame?file=adder.py", port))
    assert full["file"] == "adder.py"
    assert [l["no"] for l in full["lines"]] == [1, 2]
    assert full["lines"][1]["ai"] is True
    assert "L-0001" in full["entries"]


def test_blame_bad_line(port):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get("/api/blame?file=adder.py&line=0", port)
    assert exc.value.code == 400


def test_capsule_endpoint(port, inited_repo: Path):
    """Capsule is present now (post-commit auto-refresh) or previewed if absent."""
    data = body(get("/api/capsule", port))
    assert data["missing"] is False and "L-0001" in data["text"]
    # Without a capsule file the endpoint still previews + hints (unchanged).
    (inited_repo / ".ledger" / "CAPSULE.md").unlink()
    data = body(get("/api/capsule", port))
    assert data["missing"] is True and "hint" in data


def test_files_endpoint(port):
    data = body(get("/api/files", port))
    assert "adder.py" in data["files"] and "README.md" in data["files"]


def test_file_content_endpoint(port):
    data = body(get("/api/file?path=adder.py", port))
    assert data["binary"] is False
    assert "# line two" in data["content"]


# ---------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------


def test_bad_paths_rejected(port):
    cases = {
        ".git/config": 403,
        ".git": 403,
        ".ledger/redact_map.json": 403,
        "../outside.txt": 400,
        "sub/../../.git/config": 400,
        "C:\\proj\\sneaky": 400,
        "/etc/passwd": 404,             # leading slash disarmed -> treated as relative
        "%2e%2e%2fout.txt": 400,        # '..' percent-decoded to traversal
        "missing-file.py": 404,
    }
    for rel, expected in cases.items():
        try:
            get(f"/api/file?path={rel}", port)
            status = 200
        except urllib.error.HTTPError as exc:
            status = exc.code
        assert status == expected, f"{rel!r}: got {status}, expected {expected}"


def test_post_is_read_only(port):
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/entries", data=b"{}", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(request, timeout=5)
    assert exc.value.code == 405


def test_unknown_api_404(port):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get("/api/nope", port)
    assert exc.value.code == 404


def test_blame_requires_file(port):
    with pytest.raises(urllib.error.HTTPError) as exc:
        get("/api/blame", port)
    assert exc.value.code == 400


def test_redact_map_never_served_even_when_present(inited_repo: Path):
    (inited_repo / ".ledger" / "redact_map.json").write_bytes(b"{}")
    srv = build_server(inited_repo, 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            get("/api/file?path=.ledger/redact_map.json", srv.server_address[1])
        assert exc.value.code == 403
    finally:
        srv.shutdown()
        srv.server_close()


# ---------------------------------------------------------------------------
# safe_file unit checks (no HTTP)
# ---------------------------------------------------------------------------


def test_safe_file_unit_behaviour(inited_repo):
    with pytest.raises(ApiError):
        safe_file(inited_repo, "\\..\\")


def test_safe_file_accepts_benign_path(inited_repo):
    resolved = safe_file(inited_repo, "README.md")
    assert resolved.read_text("utf-8") == "human readme\n"
