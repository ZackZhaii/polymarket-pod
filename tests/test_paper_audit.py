"""Issue #69, acceptance 1: the offline paper-ledger vs bus-fill audit CLI entry point.

``python -m src.paper_audit --ledger PATH --bus PATH --json`` must exist, import the standard
library only, read both disks offline (read-only) and print its report to stdout. The deep checks
of the later acceptance items (ledger_store.verify, payload equality, duplicates, SHA256, the
UNKNOWN markers) are not claimed here; this file only pins the entry point and its basic
fill_id cross-check (matched / only-in-ledger / only-in-bus).
"""
from __future__ import annotations

import ast
import json
import socket
import subprocess
import sys
from pathlib import Path

from src import paper_audit

ROOT = Path(__file__).resolve().parents[1]


def fill(fill_id: str, market: str = "m1") -> dict:
    return {"fill_id": fill_id, "market": market, "side": "buy", "shares": 10.0}


def bus_row(fill_id: str, market: str = "m1") -> dict:
    return {"ts": "2026-10-11T00:00:00Z", "from": "Executor", "to": "Sentinel", "type": "fill",
            "market": market, "payload": fill(fill_id, market)}


def write_jsonl(path: Path, rows: list[dict]) -> None:
    payload = "".join(json.dumps(row) + "\n" for row in rows).encode("utf-8")
    path.write_bytes(payload)  # exact bytes: the appender writes LF, not the platform newline


def pair(tmp_path: Path, ledger_rows: list[dict], bus_rows: list[dict]) -> list[str]:
    ledger = tmp_path / "paper.jsonl"
    bus = tmp_path / "bus.jsonl"
    write_jsonl(ledger, ledger_rows)
    write_jsonl(bus, bus_rows)
    return ["--ledger", str(ledger), "--bus", str(bus), "--json"]


def test_the_module_imports_the_standard_library_only():
    tree = ast.parse((ROOT / "src" / "paper_audit.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in sys.stdlib_module_names for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            assert (node.module or "").split(".")[0] in sys.stdlib_module_names, node.module


def test_matching_fills_pass_and_print_json_to_stdout(tmp_path, capsys):
    code = paper_audit.main(pair(tmp_path, [fill("a1"), fill("a2")], [bus_row("a1"), bus_row("a2")]))
    out = capsys.readouterr().out
    assert code == paper_audit.EXIT_OK == 0
    report = json.loads(out)  # the result is the stdout payload
    assert report["matched"] == 2 and report["ledger_only"] == [] and report["bus_only"] == []
    assert report["problems"] == [] and report["pass"] is True


def test_orphans_on_either_side_fail_with_a_nonzero_exit(tmp_path, capsys):
    code = paper_audit.main(pair(tmp_path, [fill("a1"), fill("a2")], [bus_row("a1"), bus_row("b9")]))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1
    assert report["ledger_only"] == ["a2"] and report["bus_only"] == ["b9"] and report["pass"] is False


def test_a_torn_ledger_line_is_not_a_pass(tmp_path, capsys):
    argv = pair(tmp_path, [fill("a1")], [bus_row("a1")])
    ledger = Path(argv[1])
    raw = ledger.read_bytes()
    assert raw.endswith(b"\n")
    ledger.write_bytes(raw[:-1])  # torn tail: the last line lost its newline
    code = paper_audit.main(argv)
    assert code != paper_audit.EXIT_OK
    report = json.loads(capsys.readouterr().out)
    assert report["pass"] is False and "ledger:torn_last_row" in report["problems"]


def test_an_unreadable_or_missing_input_is_a_usage_error(tmp_path):
    assert paper_audit.main(["--ledger", str(tmp_path / "none.jsonl"), "--bus", str(tmp_path / "b.jsonl"),
                             "--json"]) == paper_audit.EXIT_USAGE == 2
    ledger = tmp_path / "paper.jsonl"
    write_jsonl(ledger, [fill("a1")])
    assert paper_audit.main(["--ledger", str(ledger), "--bus", str(tmp_path / "gone.jsonl"),
                             "--json"]) == paper_audit.EXIT_USAGE


def test_offline_the_audit_never_opens_a_socket(tmp_path, monkeypatch):
    def refused(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("paper_audit must not touch the network")

    monkeypatch.setattr(socket, "socket", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    assert paper_audit.main(pair(tmp_path, [fill("a1")], [bus_row("a1")])) == paper_audit.EXIT_OK


def test_the_inputs_are_read_only(tmp_path):
    argv = pair(tmp_path, [fill("a1")], [bus_row("a1")])
    ledger, bus = Path(argv[1]), Path(argv[3])
    before = (ledger.read_bytes(), bus.read_bytes())
    assert paper_audit.main(argv) == paper_audit.EXIT_OK
    assert (ledger.read_bytes(), bus.read_bytes()) == before  # nothing written back


def test_python_dash_m_entry_point_runs_from_the_repo_root(tmp_path):
    argv = pair(tmp_path, [fill("a1")], [bus_row("a1")])
    done = subprocess.run([sys.executable, "-m", "src.paper_audit", *argv], cwd=str(ROOT),
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout)
    assert report["matched"] == 1 and report["pass"] is True


def test_usage_error_for_bad_flags(capsys):
    assert paper_audit.main(["--bogus"]) == paper_audit.EXIT_USAGE
    assert paper_audit.main([]) == paper_audit.EXIT_USAGE  # both paths are required
    assert paper_audit.main(["--help"]) == 0
