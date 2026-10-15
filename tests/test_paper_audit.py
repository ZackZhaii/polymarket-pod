"""Issue #69, acceptance 2–3 (+ review P1/P2 on PR #202): the offline paper-ledger vs bus-fill audit.

``python -m src.paper_audit --ledger PATH --bus PATH --json`` reads both disks offline and
read-only. Acceptance 2: the ledger side must first pass ``ledger_store.verify`` — the fixtures
below are booked through the real ``ledger_store.book``, then damaged on disk — and the bus side
is parsed strictly (bad JSON, non-object rows, a torn tail and unreadable files are all named
failures; no lenient reader that skips bad lines). Acceptance 3: every joined fill's full payload
and the message's outer ``market`` are compared with a type-sensitive normalized JSON comparison
(1 != 1.0 != true != "1"; key order and formatting normalized; the outer ``bus.ts`` deliberately
ignored). Acceptance 4: every bus fill must be the Executor-to-Sentinel message the pipeline writes
(``src/paper_ledger.py`` is the only fill emitter); identical duplicate copies count once and the
extra-copy count is reported; copies of one fill that differ from each other, orphans on either
side and wrong sender roles are named failures. NEW-3b (Poly指挥, merged into #69): settlement rows
(``kind`` "settlement") are booked to the ledger only — they never have a bus fill by design — so
they are counted separately (``ledger.settlements``) and are not orphans; a settlement row without
a fill_id, or a bus fill carrying a settlement's fill_id, is a named problem. Acceptance 5: a modern
hypothetical fill joins and takes part in the copy checks like any modern fill (counted in
``ledger.hypothetical_fills``); a ledger row without fill_id is pre-Task-AF history — it is never
guessed into a join, is reported UNKNOWN (``unknowns``, the derived ``legacy:`` id as a stable
name) and counted (``ledger.legacy_rows``); the coverage gate keeps all-empty or nothing-verified
runs at verdict ``unknown``, never PASS. Acceptance 6: with every check of the issue now running,
a problem-free run that cross-checked at least one fill and has no legacy row is verdict ``pass``
with exit 0, and the JSON report carries each input's SHA256, the verified / unverified counts
(``counts``) and the named difference reasons (``problems`` etc.); the independent RiskGate allow
attribution and mark-to-market are explicitly UNKNOWN in every run (``unknown_checks``) — honest
non-claims that never gate the verdict.
"""
from __future__ import annotations

import ast
import hashlib
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
    audit ignores it. The payload is the booked row exactly (paper_ledger: ``fill = booked``).
    Acceptance 4: the sender roles are exactly ``from`` Executor ``to`` Sentinel — paper_ledger
    (src/paper_ledger.py:671) is the only fill emitter and podlib refuses a fill from anyone else."""
    return {"ts": ts, "from": "Executor", "to": "Sentinel", "type": "fill",
            "market": row["market"], "payload": row}


def settlement_row(market: str = "m1", token: str = "t1", shares: float = 10.0,
                   payout: float = 1.0) -> dict:
    """One legal settlement row (issue #126 / NEW-3b): closes the whole open leg exactly.

    Must follow a counted buy of ``shares`` of ``token`` in ``market`` (the ``book_fill`` helper
    books exactly that), or ledger_store refuses it (``settlement_not_closing``)."""
    return {"ts": "2026-10-12T00:00:00Z", "market": market, "token_id": token, "side": "sell",
            "shares": shares, "notional_usd": shares * payout, "fee_rate": 0.0, "fee_usd": 0.0,
            "market_price": payout, "effective_price": payout, "hypothetical": False,
            "counts_toward_risk": True, "risk_decision": "settlement", "kind": "settlement",
            "condition_id": "0xc1", "winning_token_id": token, "settlement_payout": payout,
            "closed_ts": "2026-10-11T00:00:00Z"}


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


def test_a_clean_join_is_a_full_pass_with_sha256_and_counts(tmp_path, capsys):
    """Acceptance 6: every check of issue #69 now runs, so a clean join with at least one
    cross-checked fill and no legacy row is verdict ``pass`` / exit 0, and the report carries each
    input's SHA256 (of the exact bytes on disk — read after the run, so the inputs are proven
    unchanged too), the verified / unverified counts and the two permanent UNKNOWN non-claims."""
    ledger = tmp_path / "paper.jsonl"
    rows = [book_fill(ledger, market="m1"), book_fill(ledger, market="m2")]
    bus = tmp_path / "bus.jsonl"
    # the outer bus.ts differs from the fill ts on purpose: acceptance 3 ignores it
    write_jsonl(bus, [bus_fill(rows[0], ts="2027-01-01T00:00:00Z"), bus_fill(rows[1])])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_OK == 0  # the fully checked pass is reachable now
    assert report["ledger_verified"] is True and report["verdict"] == "pass"
    assert report["matched"] == 2 and report["ledger_only"] == [] and report["bus_only"] == []
    assert report["problems"] == []  # equal payloads and outer markets: acceptance 3 is checked
    assert report["ledger"]["sha256"] == hashlib.sha256(ledger.read_bytes()).hexdigest()
    assert report["bus"]["sha256"] == hashlib.sha256(bus.read_bytes()).hexdigest()
    assert report["counts"] == {"verified_fills": 2, "unverified_rows": 0}
    assert report["unknowns"] == [] and report["ledger"]["legacy_rows"] == 0
    assert report["unknown_checks"] == ["riskgate_allow_attribution", "mark_to_market"]
    assert "unverified" not in report  # nothing is left unimplemented


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
    # acceptance 6: the fill was cross-checked (counted verified) and the difference carries a
    # named reason — the drifted field — rather than a bare count
    assert report["counts"]["verified_fills"] == 1 and report["counts"]["unverified_rows"] == 0
    assert any(problem.startswith(f"payload_mismatch:{row['fill_id']}:notional_usd")
               for problem in report["problems"])


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
    assert code == paper_audit.EXIT_OK == 0  # pass, not fail: normalization saw no drift
    assert report["verdict"] == "pass" and report["problems"] == []


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
    assert paper_audit.main(argv_for(ledger, bus)) == paper_audit.EXIT_OK  # clean pass, offline


def test_the_inputs_are_read_only(tmp_path):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row)])
    before = (ledger.read_bytes(), bus.read_bytes())
    assert paper_audit.main(argv_for(ledger, bus)) == paper_audit.EXIT_OK
    assert (ledger.read_bytes(), bus.read_bytes()) == before  # nothing written back


def test_python_dash_m_entry_point_runs_from_the_repo_root(tmp_path):
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row)])
    done = subprocess.run([sys.executable, "-m", "src.paper_audit", *argv_for(ledger, bus)],
                          cwd=str(ROOT), capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr  # a clean run is the fully checked pass now
    report = json.loads(done.stdout)
    assert report["matched"] == 1 and report["verdict"] == "pass"
    assert report["counts"] == {"verified_fills": 1, "unverified_rows": 0}


def test_usage_error_for_bad_flags(capsys):
    assert paper_audit.main(["--bogus"]) == paper_audit.EXIT_USAGE
    assert paper_audit.main([]) == paper_audit.EXIT_USAGE  # both paths are required
    assert paper_audit.main(["--help"]) == 0


# --- acceptance 4: sender roles, duplicate / conflict copies, settlement rows (NEW-3b) ----------


@pytest.mark.parametrize("roles", [{"from": "RiskGate"}, {"to": "Operator"},
                                   {"from": "Sentinel", "to": "Executor"}])
def test_wrong_sender_roles_on_a_bus_fill_fail(tmp_path, capsys, roles):
    """Acceptance 4: 错误发送角色均失败并返回非零. paper_ledger is the only fill emitter and its
    fills are always Executor → Sentinel (podlib.bus refuses a fill from anyone else)."""
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    message = bus_fill(row)
    message.update(roles)
    write_jsonl(bus, [message])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1 and report["verdict"] == "fail"
    assert f"sender_role_mismatch:{row['fill_id']}" in report["problems"]


def test_identical_duplicate_bus_copies_count_once_and_report_the_count(tmp_path, capsys):
    """Acceptance 4: 完全一致的重复副本只计一次并报告数量. Two copies written at different bus
    times (the outer ts is the write time and is ignored) are still one identical copy."""
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row, ts="2026-10-12T00:00:01Z"),
                      bus_fill(row, ts="2026-10-12T00:00:09Z")])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_OK == 0  # identical extra copies are not a difference
    assert report["verdict"] == "pass" and report["problems"] == []
    assert report["matched"] == 1 and report["bus"]["duplicate_fills"] == 1


def test_conflicting_duplicate_bus_copies_fail(tmp_path, capsys):
    """Acceptance 4: 冲突重复均失败并返回非零. One copy equals the booked row, the second drifted:
    even though one copy matches, the pair contradicts itself."""
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger)
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row), bus_fill(dict(row, shares=row["shares"] + 1.0))])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1 and report["verdict"] == "fail"
    assert f"duplicate_conflict:{row['fill_id']}" in report["problems"]


def test_settlement_rows_are_counted_separately_and_are_not_orphans(tmp_path, capsys):
    """NEW-3b: 带 kind=settlement 的账本行单独计数，不进入 ledger_only. The M2 runner books a
    settlement to the ledger only (no bus fill by design), so it must not read as an orphan."""
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger, market="m1", token="t1")
    settle = ledger_store.book(ledger, settlement_row(market="m1", token="t1"))
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row)])  # the settlement has no bus copy, on purpose
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_OK == 0  # the settlement is by-design ledger-only: no problem
    assert report["verdict"] == "pass" and report["problems"] == []
    assert report["ledger"]["settlements"] == 1 and report["matched"] == 1
    assert report["ledger_only"] == [] and report["bus_only"] == []


def test_a_bus_fill_with_a_settlements_fill_id_fails(tmp_path, capsys):
    """NEW-3b: bus 上出现带同一 fill_id 的 fill 则报 problem（结算不应有 bus fill）."""
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger, market="m1", token="t1")
    settle = ledger_store.book(ledger, settlement_row(market="m1", token="t1"))
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row), bus_fill(settle)])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1 and report["verdict"] == "fail"
    assert f"settlement_with_bus_fill:{settle['fill_id']}" in report["problems"]


def test_a_settlement_row_without_a_fill_id_is_a_named_problem(tmp_path, capsys):
    """NEW-3b: 结算行仍需有 fill_id，缺失照样报 problem. Both rows below are pre-Task-AF legacy
    rows (no fill_id), so ledger_store.verify passes and the audit itself must name the defect."""
    ledger = tmp_path / "paper.jsonl"
    legacy_buy = {"ts": "2026-10-11T00:00:00Z", "market": "m1", "token_id": "t1", "side": "buy",
                  "shares": 10.0, "notional_usd": 5.0, "fee_rate": 0.0, "fee_usd": 0.0,
                  "market_price": 0.5, "effective_price": 0.5}
    legacy_settle = dict(settlement_row(), ts="2026-10-12T00:00:00Z")
    write_jsonl(ledger, [legacy_buy, legacy_settle])
    bus = tmp_path / "bus.jsonl"
    bus.write_bytes(b"")  # no bus fill at all
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1 and report["verdict"] == "fail"
    assert "ledger:settlement_without_fill_id" in report["problems"]


# --- acceptance 5: hypothetical copies join; legacy rows are UNKNOWN, never guessed --------------


def legacy_row(**extra) -> dict:
    """One pre-Task-AF fill: no fill_id and none of the modern risk-control fields.

    ledger_store accepts such rows only before the first id'd row (the legacy prefix), and its
    replay still counts them, so this shape is legal history that passes ledger_store.verify."""
    row = {"ts": "2026-10-11T00:00:00Z", "market": "m1", "token_id": "t1", "side": "buy",
           "shares": 10.0, "notional_usd": 5.0, "fee_rate": 0.0, "fee_usd": 0.0,
           "market_price": 0.5, "effective_price": 0.5}
    row.update(extra)
    return row


def test_a_modern_hypothetical_fill_joins_and_participates_in_the_copy_check(tmp_path, capsys):
    """Acceptance 5: 现代假设成交也参与副本检查. A hypothetical fill (hypothetical true /
    counts_toward_risk false) carries a fill_id like every modern fill and is written to both logs,
    so it joins and its identical duplicate copies count once, exactly like a real fill's."""
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger, market="m1", hypothetical=True, counts_toward_risk=False)
    assert row["hypothetical"] is True and row["counts_toward_risk"] is False
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row, ts="2026-10-12T00:00:01Z"),
                      bus_fill(row, ts="2026-10-12T00:00:09Z")])  # identical extra copy
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_OK == 0  # a hypothetical clean join is a pass like any other
    assert report["verdict"] == "pass" and report["problems"] == []
    assert report["matched"] == 1 and report["bus"]["duplicate_fills"] == 1
    assert report["ledger"]["hypothetical_fills"] == 1


def test_a_drifted_extra_copy_of_a_hypothetical_fill_fails(tmp_path, capsys):
    """Acceptance 5: the copy check does not stop at joining — a second, drifted copy of a
    hypothetical fill contradicts the first one and is a named conflict with a non-zero exit."""
    ledger = tmp_path / "paper.jsonl"
    row = book_fill(ledger, hypothetical=True, counts_toward_risk=False)
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row), bus_fill(dict(row, shares=row["shares"] + 1.0))])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1 and report["verdict"] == "fail"
    assert f"duplicate_conflict:{row['fill_id']}" in report["problems"]


def test_legacy_rows_without_fill_id_are_unknown_and_counted_not_guessed(tmp_path, capsys):
    """Acceptance 5: 无 fill_id 的历史成交报告 UNKNOWN 和未核验数量，不猜测关联. A legacy row has
    no honest join: it is not named a problem, not joined and not an orphan — it is UNKNOWN with
    its derived legacy id (a stable name, never a join key) and the unverified count."""
    ledger = tmp_path / "paper.jsonl"
    write_jsonl(ledger, [legacy_row(), legacy_row(market="m2", token_id="t2")])
    bus = tmp_path / "bus.jsonl"
    bus.write_bytes(b"")
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1  # unknown: unverified history is never a pass
    assert report["verdict"] == "unknown" and report["problems"] == []
    assert report["ledger"]["legacy_rows"] == 2 and len(report["unknowns"]) == 2
    assert all(name.startswith("legacy:") for name in report["unknowns"])
    assert report["matched"] == 0 and report["ledger_only"] == [] and report["bus_only"] == []
    # acceptance 6: the unverified count names the rows that can never be joined
    assert report["counts"] == {"verified_fills": 0, "unverified_rows": 2}


def test_a_legacy_prefix_with_a_modern_fill_on_top_reports_both(tmp_path, capsys):
    """A ledger may legally start with legacy rows (before the first id'd one). The modern fill
    booked on top joins normally; the legacy row stays UNKNOWN and is never read as an orphan."""
    ledger = tmp_path / "paper.jsonl"
    write_jsonl(ledger, [legacy_row()])
    row = book_fill(ledger, market="m1", token="t1")  # booked on top of the legacy prefix
    bus = tmp_path / "bus.jsonl"
    write_jsonl(bus, [bus_fill(row)])
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1  # unknown: the legacy row is unverified history
    assert report["matched"] == 1 and report["ledger"]["legacy_rows"] == 1
    assert report["ledger_only"] == [] and report["bus_only"] == [] and report["problems"] == []


def test_all_empty_inputs_are_unknown_not_a_full_pass(tmp_path, capsys):
    """Acceptance 5: 覆盖不足或全空输入不能报告全量 PASS. Two empty logs verify trivially and have
    no problem at all, but nothing was cross-checked, so the verdict is unknown with a non-zero
    exit — never a pass, even once the later acceptance items land."""
    ledger = tmp_path / "paper.jsonl"
    bus = tmp_path / "bus.jsonl"
    ledger.write_bytes(b"")
    bus.write_bytes(b"")
    code = paper_audit.main(argv_for(ledger, bus))
    report = json.loads(capsys.readouterr().out)
    assert code == paper_audit.EXIT_MISMATCH == 1
    assert report["verdict"] == "unknown" and report["matched"] == 0 and report["problems"] == []
    assert report["ledger"]["rows"] == 0 and report["bus"]["rows"] == 0
    # acceptance 6: counts and hashes are reported even for empty inputs (hash of zero bytes)
    assert report["counts"] == {"verified_fills": 0, "unverified_rows": 0}
    assert report["ledger"]["sha256"] == hashlib.sha256(b"").hexdigest()
    assert report["bus"]["sha256"] == hashlib.sha256(b"").hexdigest()
    assert report["unknown_checks"] == ["riskgate_allow_attribution", "mark_to_market"]
