"""Task AF (backlog #20): paper ledger integrity. Offline; every ledger lives in tmp_path.

(a) fill ids + dedupe, (b) atomic writes with simulated crashes, (c) cash / position / cost-basis
conservation. Legacy ledgers (no ids) must load exactly as before: the EXPECTED_LEGACY values were
recorded from the pre-AF code (origin/dev 06e7097) on the committed fixtures.
"""
from __future__ import annotations

import ast
import json
import math
import os
import shutil
from pathlib import Path

import pytest

import src.halt_store as halt_store

from src import fees, ledger_store, paper_ledger
from src.fees import booked_fee_usd, ledger_fee
from src.ledger_store import FILL_ID_FIELD, LedgerIntegrityError, parse_ledger, verify
from src.podlib import load_risk, utc_now
from src.risk_gate import evaluate
from src.thresholds import LEDGER_OPEN_EPS, PAPER_EQUITY_USD

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ledger"
FIXTURE_NOW = "2026-10-08T12:00:00Z"

# Task AR (backlog #4): daily_loss_usd now adds today's gross realized loss to the fees. In
# legacy_mixed.jsonl m1/yes buys 24.69135802 shares for 10.0 USD and sells 10 of them for 4.0 USD:
# realized loss = average cost of 10 shares - 4.0. Every other value is unchanged.
LEGACY_MIXED_LOSS = 0.37000000000000005 + (10.0 * 10.0 / 24.69135802 - 4.0)

# (fixture, market, token, open_positions, leg_already_open, exposure_usd, leg_exposure_usd,
#  daily_loss_usd, len(recent_fills)) from the pre-AF load_state, utc_now = FIXTURE_NOW.
EXPECTED_LEGACY = [
    ('legacy_mixed.jsonl', 'm1', 'yes', 6, True, 35.928825621684325, 5.928825621684326, LEGACY_MIXED_LOSS, 9),
    ('legacy_mixed.jsonl', 'm1', 'no', 6, True, 35.928825621684325, 10.0, LEGACY_MIXED_LOSS, 9),
    ('legacy_mixed.jsonl', 'm2', '', 6, True, 35.928825621684325, 5.0, LEGACY_MIXED_LOSS, 9),
    ('legacy_mixed.jsonl', 'm3', 't', 6, True, 35.928825621684325, 5.0, LEGACY_MIXED_LOSS, 9),
    ('legacy_mixed.jsonl', 'm5', 't', 6, True, 35.928825621684325, 2.0, LEGACY_MIXED_LOSS, 9),
    ('legacy_mixed.jsonl', 'zz', '', 6, False, 35.928825621684325, 0.0, LEGACY_MIXED_LOSS, 9),
    ('legacy_commit_rows.jsonl', 'm6', 'tok6', 0, False, 0.0, 0.0, 0.40253165, 3),
    ('legacy_commit_rows.jsonl', 'phase0-self-check:hypothetical-allow', '', 0, False, 0.0, 0.0, 0.40253165, 3),
]


BUS: list[tuple] = []


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "paper.jsonl"
    BUS.clear()
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", path)
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: BUS.append(args))
    yield path
    assert paper_ledger.PAPER_PATH == path  # no test may fall back to the real logs/paper.jsonl


def _legacy(ledger: Path, name: str, monkeypatch, crlf: bool = False) -> None:
    data = (FIXTURES / name).read_bytes().replace(b"\r\n", b"\n")
    ledger.write_bytes(data.replace(b"\n", b"\r\n") if crlf else data)
    monkeypatch.setattr(paper_ledger, "utc_now", lambda: FIXTURE_NOW)


def _intent(**kwargs):
    defaults = dict(market="af", token_id="t", model_prob=0.7, market_price=0.4, fee_rate=0.02,
                    hypothetical=False, book_ts=utc_now())
    defaults.update(kwargs)
    return paper_ledger._intent(**defaults)


def _commit(**kwargs):
    return paper_ledger.commit_if_allowed(_intent(**kwargs), load_risk())


def _row(side="buy", shares=10.0, notional=4.0, rate=0.02, price=0.4, market="m", token="t", **extra):
    fee = booked_fee_usd(ledger_fee(rate, price, price, notional, shares))
    row = dict(ts=utc_now(), market=market, token_id=token, side=side, shares=shares, notional_usd=notional,
               fee_rate=rate, market_price=price, effective_price=price, fee_usd=fee,
               hypothetical=False, counts_toward_risk=True, risk_decision="allow")
    row.update(extra)
    return row


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _rewrite(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _state(market="af", token="t"):
    return paper_ledger.load_state(market, token)


def _invalid(state) -> bool:
    return state["open_positions"] is None and state["daily_loss_usd"] is None and state["ledger_problems"]


# --- legacy ledgers still load (fixtures) ---------------------------------------------------------


@pytest.mark.parametrize("expected", EXPECTED_LEGACY, ids=lambda e: f"{e[0]}:{e[1]}:{e[2]}")
@pytest.mark.parametrize("crlf", [False, True], ids=["lf", "crlf"])
def test_legacy_fixture_loads_exactly_as_before(ledger, monkeypatch, expected, crlf):
    name, market, token, positions, already, exposure, leg_exposure, loss, count = expected
    _legacy(ledger, name, monkeypatch, crlf=crlf)  # CRLF: what Windows text-mode appends wrote
    state = paper_ledger.load_state(market, token)
    assert (state["open_positions"], state["leg_already_open"], state["exposure_usd"],
            state["leg_exposure_usd"], state["daily_loss_usd"], len(state["recent_fills"])) == (
        positions, already, exposure, leg_exposure, loss, count)
    assert state["ledger_problems"] == [] and state["duplicate_fill_rows"] == 0


def test_legacy_rows_get_deterministic_distinct_ids():
    data = (FIXTURES / "legacy_mixed.jsonl").read_bytes().replace(b"\r\n", b"\n")
    first, second = parse_ledger(data), parse_ledger(data)
    assert first.ids == second.ids and len(set(first.ids)) == len(first.ids) == 9
    assert all(fill_id.startswith("legacy:") for fill_id in first.ids)
    assert first.ids[0] == ledger_store.legacy_fill_id(1, first.rows[0])
    # The two identical m5 rows are two real fills: never collapsed.
    assert first.rows[7] == first.rows[8] and first.ids[7] != first.ids[8] and first.duplicates_ignored == 0


def test_legacy_identical_rows_both_count(ledger, monkeypatch):
    _legacy(ledger, "legacy_mixed.jsonl", monkeypatch)
    legs = paper_ledger.leg_positions(_state()["recent_fills"])
    assert legs[("m5", "t")]["shares"] == 4.0


def test_legacy_rows_are_replayed_into_cash(ledger, monkeypatch):
    _legacy(ledger, "legacy_mixed.jsonl", monkeypatch)
    read = parse_ledger(ledger.read_bytes())
    cash, legs, problems = ledger_store.replay(read.rows, read.ids)
    buys = 10.0 + 10.0 + 5.0 + 5.0 + 8.0 + 1.0 + 1.0  # m2 has no notional: 5 shares at 1 USD
    sells = 4.0
    fees_paid = 0.2 + 0.0 + 0.1 + 0.0 + 0.05 + 0.3 + 0.01 + 0.01
    assert problems == []
    assert cash == pytest.approx(PAPER_EQUITY_USD - buys + sells - fees_paid, abs=LEDGER_OPEN_EPS)
    assert legs[("m1", "yes")] == pytest.approx({"shares": 14.69135802, "cost_basis_usd": 6.0})


def test_booking_on_top_of_a_legacy_ledger(ledger, monkeypatch):
    _legacy(ledger, "legacy_commit_rows.jsonl", monkeypatch)
    monkeypatch.setattr(paper_ledger, "utc_now", utc_now)
    before = ledger.read_bytes()
    result = _commit()
    assert result["allow"] is True
    fill = result["fill"]
    assert ledger.read_bytes().startswith(before)  # old rows untouched, byte for byte
    assert len(fill[FILL_ID_FIELD]) == 32 and int(fill[FILL_ID_FIELD], 16) >= 0
    legacy_cash = PAPER_EQUITY_USD - 10.0 + 10.0 - 0.2 - 0.20253165
    expected_cash = legacy_cash - fill["notional_usd"] - fill["fee_usd"]
    assert fill["cash_after_usd"] == pytest.approx(expected_cash, abs=LEDGER_OPEN_EPS)
    assert fill["leg_shares_after"] == fill["shares"]
    assert fill["leg_cost_basis_after_usd"] == fill["notional_usd"]
    state = _state()
    assert state["ledger_problems"] == [] and state["open_positions"] == 1 and state["leg_already_open"]


def test_a_legacy_row_after_an_id_row_is_missing_fill_id(ledger, monkeypatch):
    assert _commit()["allow"] is True
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_row(market="late")) + "\n")
    state = _state()
    assert _invalid(state) and state["ledger_problems"] == ["missing_fill_id:2"]
    decision = evaluate(_intent(market="x"), load_risk(), state)
    assert decision.allow is False and "invalid_risk_state" in decision.reasons


# --- (a) ids and dedupe ---------------------------------------------------------------------------


def test_every_new_fill_has_a_unique_id(ledger):
    first = _commit(market="a")["fill"]
    second = _commit(market="b")["fill"]
    assert first[FILL_ID_FIELD] != second[FILL_ID_FIELD]
    assert [row[FILL_ID_FIELD] for row in _lines(ledger)] == [first[FILL_ID_FIELD], second[FILL_ID_FIELD]]


def test_identical_repeat_is_ignored_idempotently(ledger):
    assert _commit()["allow"] is True
    before = _state("other", "x")
    line = ledger.read_bytes()
    ledger.write_bytes(line + line)  # a retried append of the very same row
    after = _state("other", "x")
    assert after["ledger_problems"] == [] and after["duplicate_fill_rows"] == 1
    for key in ("open_positions", "exposure_usd", "daily_loss_usd"):
        assert after[key] == before[key], key
    assert len(after["recent_fills"]) == 1


def test_identical_repeat_with_other_key_order_is_still_identical(ledger):
    assert _commit()["allow"] is True
    row = _lines(ledger)[0]
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(reversed(list(row.items())))) + "\n")
    state = _state("other", "x")
    assert state["ledger_problems"] == [] and state["duplicate_fill_rows"] == 1 and state["open_positions"] == 1


@pytest.mark.parametrize("field,value", [("shares", 1.0), ("notional_usd", 0.01), ("side", "sell"),
                                         ("hypothetical", True), ("note", "edited"), ("ts", "2026-01-01T00:00:00Z")])
def test_same_id_different_row_is_conflicting(ledger, field, value):
    assert _commit()["allow"] is True
    row = _lines(ledger)[0]
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(row, **{field: value})) + "\n")
    before = ledger.read_bytes()
    state = _state()
    assert _invalid(state) and f"conflicting_fill_id:{row[FILL_ID_FIELD]}" in state["ledger_problems"]
    result = _commit(market="next")
    assert result["allow"] is False and "invalid_risk_state" in result["decision"]["reasons"]
    assert ledger.read_bytes() == before  # no further booking


@pytest.mark.parametrize("bad", ["", "   ", 5, None, True, ["x"], "legacy:1:abc"])
def test_invalid_fill_id_is_an_integrity_error(ledger, bad):
    _rewrite(ledger, [_row(**{FILL_ID_FIELD: bad})])
    state = _state()
    assert _invalid(state) and state["ledger_problems"] == ["invalid_fill_id:1"]


# --- (b) atomic writes ----------------------------------------------------------------------------


def _tmp_files(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir() if p.name.endswith(".tmp"))


def _boom(*args, **kwargs):
    raise OSError("simulated crash")


@pytest.mark.parametrize("target", ["replace", "fsync"])
def test_failure_before_replace_leaves_the_old_ledger_intact(ledger, monkeypatch, target):
    assert _commit(market="first")["allow"] is True
    before = ledger.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(ledger_store.os, target, _boom)
        result = _commit(market="second")
    assert result["allow"] is False and result["halted"] and result["halt_reason"] == "ledger_write_failed"
    assert ledger.read_bytes() == before
    assert _tmp_files(ledger) == []


def test_failure_halts_and_never_announces_a_fill(ledger, monkeypatch):
    monkeypatch.setattr(ledger_store.os, "replace", _boom)
    result = _commit()
    kinds = [message[2] for message in BUS]
    assert result["allow"] is False and "fill" not in kinds and kinds[-1] == "halt"
    assert BUS[-1][4]["exchange_action"] == "none" and BUS[-1][4]["reason"] == "ledger_write_failed"
    assert not ledger.exists()


class _TornWriter:
    """A file handle that writes half the bytes and then dies, like a crash mid-write."""

    def __init__(self, handle):
        self.handle = handle

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.handle.close()
        return False

    def write(self, data):
        self.handle.write(data[: len(data) // 2])
        raise OSError("simulated crash mid-write")

    def flush(self):
        self.handle.flush()

    def fileno(self):
        return self.handle.fileno()


def test_crash_mid_write_leaves_the_old_ledger_intact(ledger, monkeypatch):
    assert _commit(market="first")["allow"] is True
    before = ledger.read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(ledger_store, "open", lambda *a, **k: _TornWriter(open(*a, **k)), raising=False)
        result = _commit(market="second")
    assert result["allow"] is False and result["halt_reason"] == "ledger_write_failed"
    assert ledger.read_bytes() == before and _tmp_files(ledger) == []
    assert _state("other", "x")["ledger_problems"] == []
    # Task AL gap 7: the ledger halt is a persisted global halt; a working disk does not lift it.
    held = _commit(market="third")
    assert held["allow"] is False and any(r.startswith("halt_global:") for r in held["decision"]["reasons"])
    halt_id = halt_store.active_halts()[0]["id"]
    halt_store.resume(from_role="Operator", scope="global", halt_id=halt_id, approved_by="test",
                      root_cause="simulated crash", postmortem="test", bus_path=ledger.parent / "bus.jsonl")
    assert _commit(market="third")["allow"] is True  # recovers after the Operator resume


def test_interrupt_during_replace_propagates_and_keeps_the_old_ledger(ledger, monkeypatch):
    assert _commit(market="first")["allow"] is True
    before = ledger.read_bytes()

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(ledger_store.os, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            _commit(market="second")
    assert ledger.read_bytes() == before and _tmp_files(ledger) == []


def test_a_stale_temp_file_from_a_killed_process_is_ignored(ledger):
    assert _commit()["allow"] is True
    (ledger.parent / f".{ledger.name}.deadbeef.tmp").write_bytes(b'{"half": ')
    assert _state("other", "x")["ledger_problems"] == []
    assert _commit(market="b")["allow"] is True


def test_ledger_changed_during_write_is_not_clobbered(ledger, monkeypatch):
    assert _commit(market="first")["allow"] is True
    real_fsync = os.fsync
    fired = []

    def fsync_then_other_writer(fd):
        real_fsync(fd)
        if not fired:  # once: the halt record written afterwards (Task AL) fsyncs too
            fired.append(fd)
            with ledger.open("ab") as handle:
                handle.write(b"x")

    with monkeypatch.context() as patch:
        patch.setattr(ledger_store.os, "fsync", fsync_then_other_writer)
        result = _commit(market="second")
    assert result["halted"] and result["ledger_problems"] == ["ledger_changed_during_write"]
    assert ledger.read_bytes().endswith(b"\nx") and _tmp_files(ledger) == []


def test_atomic_append_writes_old_bytes_plus_line(tmp_path):
    path = tmp_path / "sub" / "l.jsonl"
    ledger_store.atomic_append(path, b"", b"a\n")
    ledger_store.atomic_append(path, b"a\n", b"b\n")
    assert path.read_bytes() == b"a\nb\n" and _tmp_files(path) == []
    with pytest.raises(LedgerIntegrityError):
        ledger_store.atomic_append(path, b"stale\n", b"c\n")
    assert path.read_bytes() == b"a\nb\n"


def test_veto_does_not_create_the_ledger(ledger):
    result = _commit(model_prob=0.4)
    assert result["allow"] is False and not result.get("halted") and not ledger.exists()


@pytest.mark.parametrize("data,problem", [
    (b'{"market": "m", "side": "buy", "shares": 1.0}', "unterminated_last_row"),
    (b'{"market": "m", "side": "buy", "shares": 1.0}\n{"market": "m", "si', "malformed_row:2"),
    (b'{"market": "m"}\nnot json\n', "malformed_row:2"),
    (b'[1, 2]\n', "non_object_row:1"),
    (b'"text"\n', "non_object_row:1"),
    (b'\xff\xfe\n', "undecodable_ledger"),
])
def test_torn_or_malformed_ledgers_fail_closed(ledger, data, problem):
    """Tightening: podlib.read_jsonl silently skipped these lines; a ledger may not."""
    ledger.write_bytes(data)
    state = _state()
    assert _invalid(state) and problem in state["ledger_problems"]
    result = _commit()
    assert result["allow"] is False and ledger.read_bytes() == data


# --- (c) conservation -----------------------------------------------------------------------------


@pytest.mark.parametrize("field,delta,problem", [
    ("cash_after_usd", 0.01, "cash_mismatch"),
    ("cash_after_usd", -1e-6, "cash_mismatch"),
    ("leg_shares_after", 1.0, "position_mismatch"),
    ("leg_cost_basis_after_usd", -0.5, "cost_basis_mismatch"),
    ("shares", 1.0, "position_mismatch"),
    ("notional_usd", 0.5, "cash_mismatch"),
    ("fee_usd", 0.1, "cash_mismatch"),
])
def test_a_tampered_row_breaks_conservation_and_halts(ledger, field, delta, problem):
    assert _commit()["allow"] is True
    rows = _lines(ledger)
    rows[0][field] += delta
    _rewrite(ledger, rows)
    before = ledger.read_bytes()
    state = _state("other", "x")
    assert _invalid(state) and f"{problem}:{rows[0][FILL_ID_FIELD]}" in state["ledger_problems"]
    result = _commit(market="next")
    assert result["allow"] is False and "invalid_risk_state" in result["decision"]["reasons"]
    assert ledger.read_bytes() == before


def test_drift_within_the_tolerance_is_accepted(ledger):
    assert _commit()["allow"] is True
    rows = _lines(ledger)
    rows[0]["cash_after_usd"] += LEDGER_OPEN_EPS / 2
    _rewrite(ledger, rows)
    assert _state()["ledger_problems"] == []


@pytest.mark.parametrize("field", ledger_store.SNAPSHOT_FIELDS)
@pytest.mark.parametrize("value", ["missing", None, "1.0", float("nan"), float("inf"), True])
def test_a_missing_or_non_finite_snapshot_is_invalid(ledger, field, value):
    assert _commit()["allow"] is True
    rows = _lines(ledger)
    if value == "missing":
        del rows[0][field]
    else:
        rows[0][field] = value
    _rewrite(ledger, rows)
    state = _state()
    assert _invalid(state) and f"missing_snapshot:{field}:{rows[0][FILL_ID_FIELD]}" in state["ledger_problems"]


def test_a_fee_below_ledger_fee_is_invalid(ledger):
    assert _commit()["allow"] is True
    rows = _lines(ledger)
    lowered = rows[0]["fee_usd"] - 0.01
    rows[0]["fee_usd"] = lowered
    rows[0]["cash_after_usd"] += 0.01  # keep cash consistent: only the fee check can fire
    _rewrite(ledger, rows)
    problems = _state()["ledger_problems"]
    assert problems == [f"fee_below_ledger_fee:{rows[0][FILL_ID_FIELD]}"]


@pytest.mark.parametrize("field,value", [("fee_rate", None), ("fee_rate", "0.02"), ("notional_usd", None),
                                         ("fee_usd", None)])
def test_an_unrecomputable_fee_is_invalid(ledger, field, value):
    assert _commit()["allow"] is True
    rows = _lines(ledger)
    rows[0][field] = value
    _rewrite(ledger, rows)
    problems = _state()["ledger_problems"]
    assert f"invalid_fee:{rows[0][FILL_ID_FIELD]}" in problems


def test_an_id_row_with_an_unknown_side_is_invalid(ledger):
    assert _commit()["allow"] is True
    rows = _lines(ledger)
    rows[0]["side"] = "hold"
    _rewrite(ledger, rows)
    assert f"invalid_side:{rows[0][FILL_ID_FIELD]}" in _state()["ledger_problems"]


def test_buys_sells_and_fees_are_conserved(tmp_path):
    path = tmp_path / "l.jsonl"
    rows = [
        ledger_store.book(path, _row("buy", 25.0, 10.0, market="a", token="yes")),
        ledger_store.book(path, _row("buy", 10.0, 6.0, market="a", token="no")),
        ledger_store.book(path, _row("sell", 5.0, 2.5, price=0.5, market="a", token="yes")),
        ledger_store.book(path, _row("buy", 4.0, 2.0, market="b", token="t", hypothetical=True, counts_toward_risk=False)),
        ledger_store.book(path, _row("sell", 20.0, 9.0, price=0.45, market="a", token="yes")),
    ]
    counted = [row for row in rows if ledger_store.counts(row)]
    buys = sum(row["notional_usd"] for row in counted if row["side"] == "buy")
    sells = sum(row["notional_usd"] for row in counted if row["side"] == "sell")
    fees_paid = sum(row["fee_usd"] for row in counted)
    assert rows[-1]["cash_after_usd"] == pytest.approx(PAPER_EQUITY_USD - buys + sells - fees_paid, abs=LEDGER_OPEN_EPS)
    assert rows[-1]["leg_shares_after"] == pytest.approx(0.0, abs=LEDGER_OPEN_EPS)
    assert rows[-1]["leg_cost_basis_after_usd"] == pytest.approx(10.0 - 2.5 - 9.0)
    assert rows[1]["leg_shares_after"] == 10.0 and rows[1]["leg_cost_basis_after_usd"] == 6.0  # arb legs never net
    assert rows[3]["cash_after_usd"] == rows[2]["cash_after_usd"]  # hypothetical moves nothing
    assert verify(parse_ledger(path.read_bytes())) == []


def test_a_bad_write_is_caught_after_replace_and_halts(ledger, monkeypatch):
    assert _commit(market="first")["allow"] is True
    real = ledger_store.atomic_append

    def corrupting(path, expected, line):
        row = json.loads(line)
        row["cash_after_usd"] += 1.0
        real(path, expected, (json.dumps(row) + "\n").encode("utf-8"))

    with monkeypatch.context() as patch:
        patch.setattr(ledger_store, "atomic_append", corrupting)
        result = _commit(market="second")
    assert result["halted"] and result["halt_reason"] == "ledger_integrity"
    assert result["ledger_problems"][0].startswith("post_write:cash_mismatch:")
    assert _invalid(_state())
    before = ledger.read_bytes()
    assert _commit(market="third")["allow"] is False and ledger.read_bytes() == before


def test_book_refuses_an_invalid_ledger(tmp_path):
    path = tmp_path / "l.jsonl"
    path.write_bytes(b"garbage\n")
    with pytest.raises(LedgerIntegrityError) as caught:
        ledger_store.book(path, _row())
    assert caught.value.problems == ["malformed_row:1"] and path.read_bytes() == b"garbage\n"


@pytest.mark.parametrize("field", ["shares", "notional_usd", "fee_usd"])
def test_book_refuses_a_non_finite_fill(tmp_path, field):
    path = tmp_path / "l.jsonl"
    with pytest.raises(LedgerIntegrityError):
        ledger_store.book(path, _row(**{field: math.nan}))
    assert not path.exists()


def test_book_refuses_a_fill_that_breaks_its_own_fee(tmp_path):
    path = tmp_path / "l.jsonl"
    with pytest.raises(LedgerIntegrityError) as caught:
        ledger_store.book(path, _row(fee_usd=0.0))
    assert caught.value.problems[0].startswith("pre_write:fee_below_ledger_fee:")
    assert not path.exists()


def test_book_refuses_a_duplicate_id(tmp_path):
    path = tmp_path / "l.jsonl"
    row = ledger_store.book(path, _row())
    before = path.read_bytes()
    with pytest.raises(LedgerIntegrityError):
        ledger_store.book(path, _row(**{FILL_ID_FIELD: row[FILL_ID_FIELD]}))
    assert path.read_bytes() == before


def test_existing_ledger_is_checked_before_every_booking(ledger):
    assert _commit()["allow"] is True
    rows = _lines(ledger)
    rows[0]["cash_after_usd"] += 5
    _rewrite(ledger, rows)
    before = ledger.read_bytes()
    # Bypass RiskGate's invalid-state veto: book() itself must still refuse.
    with pytest.raises(LedgerIntegrityError):
        ledger_store.book(ledger, _row(market="z"))
    assert ledger.read_bytes() == before


# --- (d) risk-control fields (issue #68) ----------------------------------------------------------
# A modern fill (one with a fill_id) must carry hypothetical and counts_toward_risk as JSON
# booleans in one of the two legal combinations (real: false/true, hypothetical: true/false) and
# risk_decision as the string "allow". Nothing is coerced: the truthy string 'false' used to drop a
# fill from cash, positions and the daily loss while its own conservation snapshot still verified.
# "missing" deletes the key.


@pytest.mark.parametrize("field,value,problem", [
    ("hypothetical", "missing", "missing_hypothetical"),
    ("hypothetical", "false", "invalid_hypothetical"),
    ("hypothetical", "true", "invalid_hypothetical"),
    ("hypothetical", 0, "invalid_hypothetical"),
    ("hypothetical", 1, "invalid_hypothetical"),
    ("hypothetical", 0.0, "invalid_hypothetical"),
    ("hypothetical", None, "invalid_hypothetical"),
    ("hypothetical", [], "invalid_hypothetical"),
    ("counts_toward_risk", "missing", "missing_counts_toward_risk"),
    ("counts_toward_risk", "false", "invalid_counts_toward_risk"),
    ("counts_toward_risk", "true", "invalid_counts_toward_risk"),
    ("counts_toward_risk", 0, "invalid_counts_toward_risk"),
    ("counts_toward_risk", 1, "invalid_counts_toward_risk"),
    ("counts_toward_risk", None, "invalid_counts_toward_risk"),
    ("counts_toward_risk", {}, "invalid_counts_toward_risk"),
])
def test_a_modern_fill_never_coerces_its_control_fields(ledger, field, value, problem):
    assert _commit()["allow"] is True
    rows = _lines(ledger)
    if value == "missing":
        del rows[0][field]
    else:
        rows[0][field] = value
    _rewrite(ledger, rows)
    state = _state()
    assert _invalid(state) and state["ledger_problems"] == [f"{problem}:{rows[0][FILL_ID_FIELD]}"]


@pytest.mark.parametrize("hypothetical,counts_toward_risk", [(False, False), (True, True)],
                         ids=["real-but-non-counting", "hypothetical-but-counting"])
def test_only_two_control_field_combinations_are_legal(ledger, hypothetical, counts_toward_risk):
    assert _commit()["allow"] is True
    rows = _lines(ledger)
    rows[0]["hypothetical"] = hypothetical
    rows[0]["counts_toward_risk"] = counts_toward_risk
    _rewrite(ledger, rows)
    state = _state()
    assert _invalid(state) and state["ledger_problems"] == [f"inconsistent_control_fields:{rows[0][FILL_ID_FIELD]}"]


@pytest.mark.parametrize("value,problem", [
    ("missing", "missing_risk_decision"),
    ("veto", "invalid_risk_decision"),
    ("ALLOW", "invalid_risk_decision"),
    ("", "invalid_risk_decision"),
    (None, "invalid_risk_decision"),
    (1, "invalid_risk_decision"),
    (True, "invalid_risk_decision"),
])
def test_risk_decision_must_be_the_string_allow(ledger, value, problem):
    assert _commit()["allow"] is True
    rows = _lines(ledger)
    if value == "missing":
        del rows[0]["risk_decision"]
    else:
        rows[0]["risk_decision"] = value
    _rewrite(ledger, rows)
    state = _state()
    assert _invalid(state) and state["ledger_problems"] == [f"{problem}:{rows[0][FILL_ID_FIELD]}"]


def test_the_two_legal_control_combinations_are_accepted(tmp_path):
    path = tmp_path / "l.jsonl"
    real = ledger_store.book(path, _row(market="real"))
    hypo = ledger_store.book(path, _row(market="hypo", hypothetical=True, counts_toward_risk=False))
    assert verify(parse_ledger(path.read_bytes())) == []
    assert real["cash_after_usd"] < PAPER_EQUITY_USD and real["leg_shares_after"] == 10.0
    assert hypo["cash_after_usd"] == real["cash_after_usd"]  # a hypothetical fill moves nothing
    assert hypo["leg_shares_after"] == 0.0 and hypo["leg_cost_basis_after_usd"] == 0.0


@pytest.mark.parametrize("mutate,problem", [
    (lambda row: row.update(hypothetical="false"), "invalid_hypothetical"),
    (lambda row: row.update(counts_toward_risk=1), "invalid_counts_toward_risk"),
    (lambda row: row.pop("counts_toward_risk"), "missing_counts_toward_risk"),
    (lambda row: row.update(hypothetical=True), "inconsistent_control_fields"),
    (lambda row: row.update(risk_decision="veto"), "invalid_risk_decision"),
    (lambda row: row.pop("risk_decision"), "missing_risk_decision"),
])
def test_book_refuses_a_new_row_with_bad_control_fields_and_keeps_the_ledger(tmp_path, mutate, problem):
    path = tmp_path / "l.jsonl"
    ledger_store.book(path, _row(market="first"))
    before = path.read_bytes()
    row = _row(market="next", **{FILL_ID_FIELD: "bad-new"})
    mutate(row)
    with pytest.raises(LedgerIntegrityError) as caught:
        ledger_store.book(path, row)
    assert caught.value.problems == [f"{problem}:bad-new"]
    assert path.read_bytes() == before  # rejected before it is applied or written


def test_a_bad_control_field_invalidates_the_state_and_halts_booking(ledger):
    """Issue #68 repro: hypothetical='false' (a truthy string) dropped the fill from every risk
    total while its own snapshot verified. It is now an integrity problem before it is replayed,
    and commit_if_allowed books nothing and takes the existing global-halt path."""
    assert _commit()["allow"] is True
    rows = _lines(ledger)
    rows[0]["hypothetical"] = "false"
    _rewrite(ledger, rows)
    before = ledger.read_bytes()
    problem = f"invalid_hypothetical:{rows[0][FILL_ID_FIELD]}"
    state = _state("other", "x")
    assert _invalid(state) and state["ledger_problems"] == [problem]
    result = _commit(market="next")
    assert result["allow"] is False and result["halted"] is True
    assert result["halt_reason"] == "invalid_risk_state" and result["ledger_problems"] == [problem]
    assert ledger.read_bytes() == before  # no new fill
    assert any(h["scope"] == "global" for h in halt_store.active_halts())


def test_a_legacy_row_is_never_control_checked(ledger):
    # Issue #68 checks modern fills only: a pre-AF row has no fill_id and keeps its old meaning.
    _rewrite(ledger, [_row(hypothetical="false", counts_toward_risk=1)])
    state = _state()
    assert state["ledger_problems"] == []
    assert state["open_positions"] == 0  # the old truthiness rule: the string 'false' is hypothetical


# --- fees (Task AE functions) ---------------------------------------------------------------------


@pytest.mark.parametrize("side,model_prob", [("buy", 0.7), ("sell", 0.3)])
@pytest.mark.parametrize("rate", [0.0, 0.01, 0.02, 0.0312345])
@pytest.mark.parametrize("price", [0.13, 0.4, 0.61])
def test_booked_fee_is_never_lower_and_recomputes_exactly(ledger, side, model_prob, rate, price):
    intent = _intent(side=side, model_prob=model_prob if side == "buy" else max(price - 0.3, 0.01),
                     market_price=price if side == "sell" else min(price, model_prob - 0.1), fee_rate=rate,
                     market=f"{side}-{rate}-{price}")
    decision = evaluate(intent, load_risk(), _state("none", "none"))
    result = paper_ledger.commit_if_allowed(intent, load_risk())
    assert decision.allow is True and result["allow"] is True
    fill = result["fill"]
    assert fill["fee_usd"] >= booked_fee_usd(decision.fee_usd)
    assert fill["fee_usd"] >= booked_fee_usd(ledger_fee(fill["fee_rate"], fill["market_price"], fill["effective_price"],
                                                        fill["notional_usd"], fill["shares"]))
    assert _state()["ledger_problems"] == []


def test_rounding_case_books_the_higher_recomputed_fee(ledger):
    """The decision fee rounds to 0.30065086; recomputed from the stored (rounded) values it is
    0.30065087. The higher wins (+1e-8 USD), so the conservation check recomputes it exactly.
    (Issue #104 resized orders, so the case moved from the old sell 0.5339 / 0.0554 to this buy.)"""
    intent = _intent(side="buy", market_price=0.1513, model_prob=0.4513, fee_rate=0.031)
    decision = evaluate(intent, load_risk(), _state("none", "none"))
    assert booked_fee_usd(decision.fee_usd) == 0.30065086
    fill = paper_ledger.commit_if_allowed(intent, load_risk())["fill"]
    assert fill["fee_usd"] == 0.30065087
    assert _state()["ledger_problems"] == []


def test_conservation_uses_the_task_ae_fee_functions():
    source = (Path(ledger_store.__file__)).read_text(encoding="utf-8")
    assert "booked_fee_usd(expected)" in source and "ledger_fee(" in source
    assert ledger_store.ledger_fee is fees.ledger_fee and ledger_store.booked_fee_usd is fees.booked_fee_usd


# --- shape ----------------------------------------------------------------------------------------


def test_moved_names_are_re_exported_unchanged():
    assert paper_ledger.leg_key is ledger_store.leg_key
    assert paper_ledger.non_finite_ledger_rows is ledger_store.non_finite_ledger_rows
    assert paper_ledger.LEDGER_NUMBER_FIELDS == ("shares", "notional_usd", "fee_usd")
    assert paper_ledger._counts is ledger_store.counts


def test_ledger_store_is_standard_library_only_and_adds_no_number():
    tree = ast.parse(Path(ledger_store.__file__).read_text(encoding="utf-8"))
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            modules.add(node.module or "")
    assert modules == {"__future__", "hashlib", "json", "math", "os", "uuid", "dataclasses", "pathlib", "typing",
                       "src.fees", "src.thresholds"}
    numbers = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)
               and isinstance(node.value, (int, float)) and not isinstance(node.value, bool)}
    assert numbers <= {0.0, 1, 1.0}  # empty totals, line numbers from 1, 1 USD per share (as leg_positions)


def test_self_check_still_books_exactly_one_fill(ledger):
    report = paper_ledger.run_self_check(load_risk())
    assert report["hypothetical_allowed"] and report["new_fills"] == 1
    assert _state()["ledger_problems"] == []
    row = _lines(ledger)[0]
    assert row["hypothetical"] is True and row["cash_after_usd"] == PAPER_EQUITY_USD


def test_fixtures_are_unchanged_legacy_rows():
    for name in ("legacy_mixed.jsonl", "legacy_commit_rows.jsonl"):
        rows = parse_ledger((FIXTURES / name).read_bytes())
        assert rows.problems == [] and all(FILL_ID_FIELD not in row for row in rows.rows)
        assert all(field not in row for row in rows.rows for field in ledger_store.SNAPSHOT_FIELDS)


def test_copy_of_fixture_dir_is_not_written(tmp_path, ledger, monkeypatch):
    shutil.copy(FIXTURES / "legacy_mixed.jsonl", tmp_path / "copy.jsonl")
    before = (FIXTURES / "legacy_mixed.jsonl").read_bytes()
    _legacy(ledger, "legacy_mixed.jsonl", monkeypatch)
    _state()
    assert (FIXTURES / "legacy_mixed.jsonl").read_bytes() == before
