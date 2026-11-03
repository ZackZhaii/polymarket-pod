"""EN / issue #365: huge-int Gamma feeSchedule.rate/exponent must not crash.

Huge ints must give the fee problem (no OverflowError); controls unchanged; no xfail marks.
"""
from __future__ import annotations

import json
import math
import socket

import pytest

import src.heartbeat as heartbeat
import src.halt_store as halt_store
import src.forecaster as forecaster
import src.sentinel_tick as sentinel_tick
from src import fee_source as fs
from src import m2_runner
from tests import m2_runner_rig as rig

BIG = 10 ** 400
BIG_STR = "1" + "0" * 400
BASE = {"feesEnabled": True, "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def _row(enabled=True, **schedule):
    row = json.loads(json.dumps(BASE))
    row["feesEnabled"] = enabled
    for k, v in schedule.items():
        if v is _DROP:
            row["feeSchedule"].pop(k, None)
        else:
            row["feeSchedule"][k] = v
    return row


_DROP = object()

# (field, value, feesEnabled, expected problem); expected = the real fee_source reason code
BUG = [
    pytest.param("rate", BIG, True, fs.FEE_RATE_INVALID, id="rate+1e400int"),
    pytest.param("rate", -BIG, True, fs.FEE_RATE_INVALID, id="rate-1e400int"),
    pytest.param("rate", BIG, False, fs.FEE_RATE_INVALID, id="free-rate+1e400int"),
    pytest.param("rate", -BIG, False, fs.FEE_RATE_INVALID, id="free-rate-1e400int"),
    pytest.param("exponent", BIG, True, fs.FEE_SCHEDULE_UNSUPPORTED, id="exp+1e400int"),
    pytest.param("exponent", -BIG, True, fs.FEE_SCHEDULE_UNSUPPORTED, id="exp-1e400int"),
]
CONTROLS = [
    ("rate", BIG_STR, True, fs.FEE_RATE_INVALID), ("rate", "-" + BIG_STR, True, fs.FEE_RATE_INVALID),
    ("rate", "1e400", True, fs.FEE_RATE_INVALID), ("rate", "0.05", True, fs.FEE_RATE_INVALID),
    ("rate", True, True, fs.FEE_RATE_INVALID), ("rate", False, True, fs.FEE_RATE_INVALID),
    ("rate", None, True, fs.FEE_RATE_INVALID), ("rate", _DROP, True, fs.FEE_RATE_INVALID),
    ("rate", math.nan, True, fs.FEE_RATE_INVALID), ("rate", math.inf, True, fs.FEE_RATE_INVALID),
    ("rate", -math.inf, True, fs.FEE_RATE_INVALID), ("rate", -0.01, True, fs.FEE_RATE_OUT_OF_RANGE),
    ("rate", 10 ** 300, True, fs.FEE_RATE_OUT_OF_RANGE),  # big but float-representable
    ("exponent", BIG_STR, True, fs.FEE_SCHEDULE_UNSUPPORTED), ("exponent", "1e400", True, fs.FEE_SCHEDULE_UNSUPPORTED),
    ("exponent", True, True, fs.FEE_SCHEDULE_UNSUPPORTED), ("exponent", None, True, fs.FEE_SCHEDULE_UNSUPPORTED),
    ("exponent", _DROP, True, fs.FEE_SCHEDULE_UNSUPPORTED), ("exponent", math.nan, True, fs.FEE_SCHEDULE_UNSUPPORTED),
    ("exponent", math.inf, True, fs.FEE_SCHEDULE_UNSUPPORTED), ("exponent", -1, True, fs.FEE_SCHEDULE_UNSUPPORTED),
    ("exponent", 0.5, True, fs.FEE_SCHEDULE_UNSUPPORTED),
    ("rate", BIG_STR, False, fs.FEE_RATE_INVALID), ("rate", math.nan, False, fs.FEE_RATE_INVALID),
]


@pytest.mark.parametrize("field,value,enabled,problem", BUG)
def test_finite_number_huge_int_is_not_finite(field, value, enabled, problem):
    assert fs._finite_number(value) is None


@pytest.mark.parametrize("field,value,enabled,problem", BUG + CONTROLS)
def test_parse_gamma_fee_rejects(field, value, enabled, problem):
    assert fs.parse_gamma_fee(_row(enabled, **{field: value})) == (None, problem)


@pytest.mark.parametrize("field,value,enabled,problem", BUG + CONTROLS)
def test_fee_cache_get_does_not_raise_and_rejects(field, value, enabled, problem):
    t = [0.0]
    cache = fs.FeeCache(lambda mid: _row(enabled, **{field: value}), clock=lambda: t[0], wall=lambda: "w")
    quote = cache.get("m")
    assert quote.fee_rate is None and quote.problem == problem
    assert cache.fee_rate("m") is None  # negative-cached, still rejected


def test_valid_schedule_still_resolves():
    assert fs.parse_gamma_fee(_row()) == (0.05, None)
    assert fs.resolve_fee("m", _row()).fee_rate == 0.05
    assert fs.FeeCache(lambda mid: _row(), wall=lambda: "w").get("m").fee_rate == 0.05


# ---------------------------------------------------------------- runner level (offline rig, network mocked)

@pytest.fixture
def env(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    paths = rig.isolate(monkeypatch, tmp_path)
    # The shared conftest supplies the remaining isolation; the runner rig alone does not.
    for path in [*paths.values(), heartbeat.HEARTBEAT_DIR, halt_store.HALT_STATE_PATH,
                 halt_store.HALT_FALLBACK_PATH, forecaster.FORECASTS_PATH,
                 sentinel_tick.ALERTS_PATH, sentinel_tick.TICK_STATE_PATH]:
        assert path.is_relative_to(tmp_path.parent)
    return {**paths, "now": datetime.now(timezone.utc), "tmp": tmp_path}


def _runner(env, schedule):
    fake = rig.FakeGetters(env["now"], gamma_row_changes={"feesEnabled": True, "feeSchedule": schedule})
    rig.deliver(env["run"], env["now"])
    code = rig.run_once(env["run"], fake)
    return code, fake


def _assert_skipped_not_crashed(env, fake, code, problem):
    assert code == m2_runner.EXIT_OK
    steps = rig.read_jsonl(env["run"] / "journal.jsonl")
    assert steps[-1]["step"] == "skipped" and steps[-1]["reasons"] == ["missing_fee_data", problem]
    assert fake.book_urls == [] and rig.read_jsonl(env["paper"]) == []
    record, _ = heartbeat.read_beat(heartbeat.beat_path(m2_runner.PROC), m2_runner.PROC)
    assert record["state"] != heartbeat.STATE_CRASHED
    assert record["progress"]["missing_fee_total"] == 1
    result = sentinel_tick.tick(now=env["now"], bus_path=env["tmp"] / "bus2.jsonl",
                                quotes_path=env["tmp"] / "quotes2.jsonl",
                                ledger_path=env["tmp"] / "paper2.jsonl")
    assert not any("process_crashed" in str(h) for h in result["halts"])


@pytest.mark.parametrize("schedule,problem", [
    pytest.param({"rate": BIG, "exponent": 1, "takerOnly": True}, fs.FEE_RATE_INVALID, id="rate"),
    pytest.param({"rate": 0.05, "exponent": BIG, "takerOnly": True}, fs.FEE_SCHEDULE_UNSUPPORTED,
                 id="exponent"),
    pytest.param({"rate": BIG_STR, "exponent": 1, "takerOnly": True}, fs.FEE_RATE_INVALID, id="rate-str-control"),
    pytest.param({"rate": None, "exponent": 1, "takerOnly": True}, fs.FEE_RATE_INVALID, id="rate-none-control"),
])
def test_runner_skips_market_instead_of_crashing(env, schedule, problem):
    code, fake = _runner(env, schedule)
    _assert_skipped_not_crashed(env, fake, code, problem)


@pytest.mark.parametrize("error", [RuntimeError, ValueError, TypeError])
def test_crash_is_still_a_global_halt_for_other_exceptions(env, error):
    """An unrelated conversion error traverses real main/heartbeat/Sentinel, not just its policy constant."""
    class UnexpectedNumber(int):
        def __float__(self) -> float:
            raise error("unexpected fee conversion")

    # Gamma's fake getter preserves this object; conversion happens outside its caught getter layer.
    schedule = {"rate": UnexpectedNumber(1), "exponent": 1, "takerOnly": True}
    with pytest.raises(error, match="unexpected fee conversion"):
        _runner(env, schedule)
    record, problem = heartbeat.read_beat(heartbeat.beat_path(m2_runner.PROC), m2_runner.PROC)
    assert problem is None
    assert record["state"] == heartbeat.STATE_CRASHED and record["exit_code"] is None
    assert rig.read_jsonl(env["paper"]) == []
    ledger = env["tmp"] / "paper2.jsonl"
    ledger.write_text("", encoding="utf-8")
    result = sentinel_tick.tick(now=env["now"], bus_path=env["tmp"] / "bus2.jsonl",
                                quotes_path=env["tmp"] / "quotes2.jsonl", ledger_path=ledger)
    assert result["checked"][m2_runner.PROC] == heartbeat.STATE_CRASHED
    assert "global:*:process_crashed:m2_runner" in result["halts"]
