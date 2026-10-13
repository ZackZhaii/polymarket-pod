"""Issue #69, acceptance 2–3 (+ review P1/P2 on PR #202): the offline paper-ledger vs bus-fill audit.

``python -m src.paper_audit --ledger PATH --bus PATH --json`` reads both disks offline and
read-only. Acceptance 2: the ledger side must first pass ``ledger_store.verify`` — the fixtures
below are booked through the real ``ledger_store.book``, then damaged on disk — and the bus side
is parsed strictly (bad JSON, non-object rows, a torn tail and unreadable files are all named
failures; no lenient reader that skips bad lines). Acceptance 3: every joined fill's full payload
and the message's outer ``market`` are compared with a type-sensitive normalized JSON comparison
(1 != 1.0 != true != "1"; key order and formatting normalized; the outer ``bus.ts`` deliberately
ignored). Review P1: the remaining checks are still later acceptance items, so even a clean join
ends verdict ``unknown`` with a non-zero exit and never claims PASS; review P2: damaged ledgers
(tampered content, missing or mistyped risk-control fields, non-finite numbers, a conflicting
duplicate) must fail with a non-zero exit.
"""
from __future__ import annotations

import ast
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from src import ledger_store, paper_audit

ROOT = Path(__file__).resolve().parents[1]


def book_fill(ledger: Path, market: str = "m1", token: str = "t1", **extra) -> dict:
    """One legal fill through the real ledger_store.book: snapshots stamped, verify passes."""
    row = {"ts": "2026-10-12T00:00:00Z", "market": market, "token_id": token, "side": "buy",
           "shares": 10.0, "notional_usd": 5.0, "fee_rate": 0.0, "fee_usd": 0.0,
           "market_price": 0.5, "effective_price": 0.5, "hypothetical": False,
           "counts_toward_risk": True, "risk_decision": "allow"}
    row.update(extra)
    return ledger_store.book(ledger, row)


def bus_fill(row: dict, ts: str = "2026-10-12T00:00:01Z") -> dict:
    """The Executor bus message paper_ledger writes after the book (src/podlib.py bus()).

    The outer ``ts`` is the bus write time, never the fill's own ``ts``; acceptance 3 says the
    audit ignores it. The payload is the booked row exactly (paper_ledger: ``fill = booked``)."""
    return {"ts": ts, "from": "Executor", "to": "Sentinel", "type": "fill",
            "market": row["market"], "payload": row}


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def lines_of(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def rewrite(path: Path, rows: list[dict]) -> None:
    write_jsonl(path, rows)


def argv_for(ledger: Path, bus: Path) -> list[str]:
    return ["--ledger", str(ledger), "--bus", str(bus), "--json"]


def test_the_module_imports_the_standard_library_and_first_party_src_only():
    tree = ast.parse((ROOT / "src" / "paper_audit.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(alias.name.split(".")[0] in sys.stdlib_module_names
                       or alias.name.split(".")[0] == "src" for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            root = (node.module or "").split(".")[0]
            assert root in sys.stdlib_module_names or root == "src", node.module


def test_a_clean_join_is_verdict_unknown_not_pass(tmp_path, capsys):
    ledger = tmp_path / "paper.jsonl"
    rows = [book_fill(ledger, market="m1"), book_fill(ledger, market="m2")]
    bus = tmp_path / "bus.jsonl"
    # the outer bus.ts differs from the fill ts on purpose: acceptance 3 ignores it
    write_jsonl(bus, [bus_fill(rows[0], ts="2027-01-01T00:00:00Z"), bus_fill(rows[1])])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1  # P1: never PASS before the remaining checks land
    assert report["ledger_verified"] is True and report["verdict"] == "unknown"
    assert report["matched"] == 2 and report["ledger_only"] == [] and report["bus_only"] == []
    assert report["problems"] == []  # equal payloads and outer markets: acceptance 3 is checked
    assert "payload_equality_by_fill_id" not in report["unverified"]  # it is verified now
    assert "bus_sender_roles" in report["unverified"]  # acceptance 4 is still unverified


def test_a_changed_payload_field_on_the_bus_side_fails(tmp_path, capsys):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    drifted = dict(row, notional_usd=row["notional_usd"] + 1.0)  # same fill_id, drifted content
    write_jsonl(bus, [bus_fill(drifted)])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1
    assert report["verdict"] == "fail" and report["matched"] == 1
    assert any(problem.startswith(f"payload_mismatch:{row['fill_id']}") for problem in report["problems"])


@pytest.mark.parametrize("damage", [{"shares": 10}, {"hypothetical": 0}, {"counts_toward_risk": 1},
                                    {"fee_usd": "0.0"}, {"market_price": "0.5"}])
def test_a_type_only_payload_change_is_a_mismatch(tmp_path, capsys, damage):
    """Type-sensitive normalized JSON: 10 != 10.0, false != 0, 0.0 != "0.0" (Python == says equal)."""
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    drifted = dict(row)
    drifted.update(damage)
    write_jsonl(bus, [bus_fill(drifted)])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1
    assert report["verdict"] == "fail"
    assert any(problem.startswith(f"payload_mismatch:{row['fill_id']}") for problem in report["problems"])


def test_a_missing_payload_field_is_a_mismatch(tmp_path, capsys):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    drifted = dict(row)
    del drifted["fee_usd"]
    write_jsonl(bus, [bus_fill(drifted)])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1
    assert any(problem.startswith(f"payload_mismatch:{row['fill_id']}") for problem in report["problems"])


def test_key_order_and_json_formatting_are_normalized_away(tmp_path, capsys):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    shuffled = {name: row[name] for name in reversed(sorted(row))}
    message = bus_fill(shuffled, ts="2030-12-31T23:59:59Z")
    # one JSON object per line (JSONL), but different key order and JSON spacing than the ledger
    bus.write_text(json.dumps(message, separators=(", ", ": "), sort_keys=False) + "\n",
                   encoding="utf-8")
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1  # unknown, not fail: normalization saw no drift
    assert report["verdict"] == "unknown" and report["problems"] == []


@pytest.mark.parametrize("outer", ["other-market", None, 42])
def test_the_outer_bus_market_must_match_the_fill(tmp_path, capsys, outer):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    message = bus_fill(row)
    message["market"] = outer
    write_jsonl(bus, [message])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1
    assert report["verdict"] == "fail" and report["matched"] == 1  # the payload itself is untouched
    assert f"outer_market_mismatch:{row['fill_id']}" in report["problems"]


def test_a_missing_outer_market_is_a_mismatch(tmp_path, capsys):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    message = bus_fill(row)
    del message["market"]
    write_jsonl(bus, [message])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1
    assert f"outer_market_mismatch:{row['fill_id']}" in report["problems"]


def test_a_non_finite_number_in_the_bus_payload_fails(tmp_path, capsys):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(dict(row, shares=float("nan")))])  # json writes the NaN literal
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1
    assert f"non_finite_payload:{row['fill_id']}" in report["problems"]


def test_a_drifted_second_bus_copy_of_one_fill_fails(tmp_path, capsys):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row), bus_fill(dict(row, shares=row["shares"] + 1.0))])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1
    assert report["verdict"] == "fail" and report["matched"] == 1
    assert any(problem.startswith(f"payload_mismatch:{row['fill_id']}") for problem in report["problems"])


def test_orphans_on_either_side_fail_with_a_nonzero_exit(tmp_path, capsys):
    ledger = tmp_path / "paper.jsonl"
    rows = [book_fill(ledger, market="m1"), book_fill(ledger, market="m2")]
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(rows[0]), bus_fill(dict(rows[0], fill_id="b9"))])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1
    assert report["ledger_only"] == [rows[1]["fill_id"]] and report["bus_only"] == ["b9"]
    assert report["verdict"] == "fail" and report["ledger_verified"] is True


def test_a_tampered_ledger_snapshot_fails_ledger_store_verify(tmp_path, capsys):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    rows = lines_of(ledger)
    rows[0]["cash_after_usd"] += 1.0  # content drift: conservation no longer holds
    rewrite(ledger, rows)
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row)])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1
    assert report["ledger_verified"] is False and report["verdict"] == "fail"
    assert any(problem.startswith("ledger:verify:cash_mismatch:") for problem in report["problems"])


@pytest.mark.parametrize("damage", [{"hypothetical": None}, {"counts_toward_risk": "false"},
                                    {"risk_decision": "veto"}])
def test_a_ledger_row_with_missing_or_mistyped_control_fields_fails(tmp_path, capsys, damage):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    rows = lines_of(ledger)
    rows[0].update(damage)
    rewrite(ledger, rows)
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row)])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1  # P2: never coerced into a valid value
    assert report["verdict"] == "fail" and report["problems"]


def test_non_finite_numbers_in_the_ledger_fail_verify(tmp_path, capsys):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    rows = lines_of(ledger)
    rows[0]["shares"] = float("nan")  # json parses the NaN literal back in
    rewrite(ledger, rows)
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row)])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1
    assert any(problem.startswith("ledger:verify:non_finite_row:") for problem in report["problems"])


def test_a_conflicting_duplicate_ledger_row_fails(tmp_path, capsys):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    rows = lines_of(ledger)
    twin = dict(rows[0])
    twin["notional_usd"] += 1.0  # same fill_id, different content
    rewrite(ledger, rows + [twin])
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row)])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code != 0 and any(problem.startswith("ledger:conflicting_fill_id")
                             for problem in report["problems"])


def test_a_torn_ledger_line_is_not_a_pass(tmp_path, capsys):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row)])
    raw = ledger.read_bytes()
    assert raw.endswith(b"\n")
    ledger.write_bytes(raw[:-1])  # torn tail: the last line lost its newline
    code = paper_audit.main(argv_for(ledger, bus))
    assert code == paper_audit.EXIT_USAGE == 2
    report = json.loads(capsys.readouterr().out)
    assert "ledger:unterminated_last_row" in report["problems"]


@pytest.mark.parametrize("bus_bytes,problem", [
    (b'{"broken"\n', "bus:malformed_row:1"),
    (b"[1]\n", "bus:non_object_row:1"),
    (b'{"type": "fill", "payload": {"fill_id": "x"}}', "bus:torn_last_row"),
])
def test_bus_parse_defects_are_named_failures(tmp_path, capsys, bus_bytes, problem):
    ledger = tmp_path / "paper.jsonl"
    book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    bus.write_bytes(bus_bytes)
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_USAGE == 2  # the file cannot be read in full
    assert problem in report["problems"] and report["verdict"] == "fail"


def test_an_unreadable_or_missing_input_is_a_usage_error(tmp_path):
    ledger = tmp_path / "paper.jsonl"
    book_fill(ledger)
    assert paper_audit.main(["--ledger", str(tmp_path / "none.jsonl"), "--bus", str(ledger),
                             "--json"]) == paper_audit.EXIT_USAGE == 2
    assert paper_audit.main(["--ledger", str(ledger), "--bus", str(tmp_path / "gone.jsonl"),
                             "--json"]) == paper_audit.EXIT_USAGE


def test_offline_the_audit_never_opens_a_socket(tmp_path, monkeypatch):
    def refused(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("paper_audit must not touch the network")

    monkeypatch.setattr(socket, "socket", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row)])
    assert paper_audit.main(argv_for(ledger, bus)) == paper_audit.EXIT_MISMATCH  # unknown, not pass


def test_the_inputs_are_read_only(tmp_path):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row)])
    before = (ledger.read_bytes(), bus.read_bytes())
    assert paper_audit.main(argv_for(ledger, bus)) == paper_audit.EXIT_MISMATCH
    assert (ledger.read_bytes(), bus.read_bytes()) == before  # nothing written back


def test_python_dash_m_entry_point_runs_from_the_repo_root(tmp_path):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row)])
    done = subprocess.run([sys.executable, "-m", "src.paper_audit", *argv_for(ledger, bus)],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert done.returncode == 1, done.stderr  # verdict unknown: never PASS before the deep checks
    report = json.loads(done.stdout)
    assert report["matched"] == 1 and report["verdict"] == "unknown"


def test_usage_error_for_bad_flags(capsys):
    assert paper_audit.main(["--bogus"]) == paper_audit.EXIT_USAGE
    assert paper_audit.main([]) == paper_audit.EXIT_USAGE  # both paths are required
    assert paper_audit.main(["--help"]) == 0
