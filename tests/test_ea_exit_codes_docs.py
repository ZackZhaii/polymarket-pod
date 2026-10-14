"""Task EA (issue #245): EXIT_CODES.md matches m2_runner / secret_scan reality.

The registry Sentinel column for m2_runner 2/3/7 is ``不可见``: those exits return before
Heartbeat is created so Sentinel never sees them, and prose and table must agree (PR #338). Offline; no network.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXIT_CODES = (ROOT / "docs" / "EXIT_CODES.md").read_text(encoding="utf-8")


def _section(name: str) -> str:
    marker = f"## `{name}`"
    start = EXIT_CODES.index(marker)
    rest = EXIT_CODES[start + len(marker) :]
    nxt = rest.find("\n## `")
    return rest if nxt < 0 else rest[:nxt]


def test_m1_run_documents_6h_not_24h():
    sec = _section("src/m1_run.py")
    assert "24 h" not in sec and "24h" not in sec
    assert "6 h" in sec
    assert "M1_RUN_TARGET_S" in sec or "21600" in sec


def test_m2_runner_pre_heartbeat_exits_documented():
    sec = _section("src/m2_runner.py")
    # old inaccurate claim
    assert "before any read, seal or commit" not in sec
    # pre-heartbeat exits (main returns before _run creates Heartbeat)
    assert "before" in sec.lower() and "heartbeat" in sec.lower()
    for needle in ("EXIT_USAGE", "EXIT_ALREADY_RUNNING", "EXIT_SELF_CHECK"):
        assert needle in sec
    assert "exit 3" in sec.lower() or "EXIT_ALREADY_RUNNING" in sec
    # must not overwrite a running instance's heartbeat
    assert "must never" in sec.lower() or "never write" in sec.lower() or "no heartbeat" in sec.lower()
    # settlement reads happen after preflight (Gamma), not "before any read"
    assert "settlement" in sec.lower()


def test_secret_scan_exit2_covers_unenumerable_directory():
    sec = _section("src/secret_scan.py")
    row2 = [ln for ln in sec.splitlines() if ln.startswith("| 2 |")][0]
    assert "directory" in row2.lower() or "enumerat" in row2.lower() or "unenumerable" in row2.lower()


def test_m2_runner_prose_and_table_agree_on_invisible_exits():
    sec = _section("src/m2_runner.py")
    rows = {ln.split("|")[1].strip(): ln for ln in sec.splitlines() if ln.startswith("| ")}
    for code in ("2", "3", "7"):
        assert rows[code].rstrip().endswith("| 不可见 |"), rows[code]
    assert "m2_start" in sec and "exits 5" in sec
    assert "stderr" in sec and "supervisor" in sec
    assert "availability blind spot" in sec
    assert "normal refusal" in sec
    assert "no alert fires in practice" not in sec
