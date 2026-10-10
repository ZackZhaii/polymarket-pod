"""Task AJ: the end-to-end offline hard gate (backlog #24).

scripts/e2e_offline.py runs the real pipeline on tests/fixtures/e2e/ (scanner rows -> /book
recorder -> paper_cycle + Sentinel -> Endgame / NewsForecaster signals -> Intent -> RiskGate ->
paper fill -> ledger_store -> conservation -> Sentinel). Only the clock and I/O paths are faked.
The run must match golden.json exactly and break no invariant (no network, no exchange action,
risk.toml pinned, live_enabled false, conservation, unique fill ids).

golden.json changes only with ``--update-golden`` (tests/conftest.py, off by default).
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import shutil
import socket
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import e2e_offline as harness
from src import book_staleness, ledger_store, podlib, wash_detect
from src.thresholds import MIN_EDGE

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "e2e"
GOLDEN = FIXTURES / "golden.json"
PINNED = "0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04"
RISK = ROOT / "config" / "risk.toml"
RISK_BEFORE = RISK.read_bytes()


@pytest.fixture(scope="module")
def run() -> dict:
    return harness.run_once(FIXTURES)


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def decision(run: dict, label: str) -> dict:
    found = [d for d in run["actual"]["decisions"] if d["label"] == label]
    assert len(found) == 1, label
    return found[0]


def bus_rows(run: dict, kind: str | None = None, market: str | None = None) -> list[dict]:
    return [r for r in run["detail"]["bus"]
            if (kind is None or r["type"] == kind) and (market is None or r["market"] == market)]


# ---------------------------------------------------------------- the hard gate

def test_the_run_matches_golden(run, request):
    if request.config.getoption("--update-golden"):
        assert run["problems"] == []  # never write golden from a run that broke an invariant
        harness.write_golden(FIXTURES, run["actual"])
        pytest.fail("golden.json rewritten by --update-golden: review git diff tests/fixtures/e2e/golden.json, "
                    "then rerun without the flag")
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert harness.golden_problems(run["actual"], golden) == []


def test_the_run_breaks_no_invariant(run):
    assert run["problems"] == []


def test_the_gate_passes_and_reports_a_summary():
    problems, summary = harness.gate(ROOT)
    assert problems == []
    assert summary.startswith("2 allow / 5 veto, cash 949.803488, sentinel halts ['e2e-charlie'], network 0")


def test_the_whole_run_is_fast():
    import time
    start = time.perf_counter()
    harness.run_once(FIXTURES)
    assert time.perf_counter() - start < 10.0  # a guard check must stay cheap; it takes ~0.1 s


# ---------------------------------------------------------------- the stages are connected

def test_scanner_rows_feed_the_recorder_and_the_cycle(run):
    actual = run["actual"]
    passed = actual["scanner"]["passed"]
    assert passed == ["e2e-alpha", "e2e-bravo", "e2e-charlie", "e2e-delta", "e2e-echo", "e2e-foxtrot", "e2e-golf"]
    assert actual["scanner"]["first_reason"] == {"long_tail_price": 1, "low_liquidity": 1, "missing_version": 1}
    assert actual["books"]["recorded"] == 2 * len(passed) == len(actual["books"]["rows"])
    assert {row["market_id"] for row in actual["books"]["rows"]} == set(passed)
    assert actual["cycle_1"]["sentinel_checked"] == len(passed)
    assert actual["cycle_1"]["gaps"] == 1 and len(bus_rows(run, "signal", "e2e-foxtrot")) >= 1


def test_endgame_emits_a_signal_only(run):
    endgame = run["actual"]["endgame"]
    assert endgame["candidates"] == ["e2e-hotel"]
    assert endgame["signals"] == [{"exchange_action": "none", "market": "e2e-hotel", "outcome": "Yes",
                                   "outcome_index": 0, "price": 0.985, "token_id": "78001"}]
    assert not [d for d in run["actual"]["decisions"] if d["market"] == "e2e-hotel"]
    assert not bus_rows(run, "fill", "e2e-hotel") and not bus_rows(run, "veto", "e2e-hotel")


def test_every_intent_starts_from_a_newsforecaster_signal(run):
    forecasts = [r for r in run["detail"]["bus"] if r["from"] == "NewsForecaster"]
    # 7 intents, the replay commit, two corrupted-ledger commits, two Task AL halt-consumer commits,
    # four Task AP heartbeat-halt commits (all vetoed), six Task AR daily-loss cases (one each),
    # one Task AU slippage-floor signal (shared by its cases)
    assert len(forecasts) == 23
    assert all(r["to"] == "RiskGate" and r["payload"]["order"] is False for r in forecasts)


# ---------------------------------------------------------------- scenarios

def test_stale_book_vetoes_and_books_nothing(run):
    d = decision(run, "charlie-stale")
    assert (d["allow"], d["reasons"], d["booked_rows"]) == (False, ["stale_book"], 0)
    assert d["edge"] >= MIN_EDGE  # only the staleness vetoes it
    assert not bus_rows(run, "fill", "e2e-charlie")


def test_non_finite_price_vetoes(run):
    d = decision(run, "delta-nan")
    assert d["allow"] is False and d["booked_rows"] == 0
    assert d["reasons"][0] == "non_finite_market_price"
    assert d["reasons"] == ["non_finite_market_price", "market_price_out_of_range", "invalid_spread",
                            "effective_price_out_of_range", "non_positive_edge"]
    assert d["edge"] == -1.0
    row = next(r for r in run["actual"]["books"]["rows"] if r["token_id"] == "74001")
    assert row["best_ask"] == "NaN" and row["spread"] == "NaN"  # the recorder passed the NaN through


def test_wash_vetoes(run):
    d = decision(run, "echo-wash")
    assert (d["allow"], d["reasons"], d["side"]) == (False, ["wash_same_market"], "sell")
    assert d["edge"] >= MIN_EDGE


def test_edge_below_min_vetoes(run):
    d = decision(run, "foxtrot-thin-edge")
    assert (d["allow"], d["reasons"]) == (False, ["edge_below_min"])
    assert 0 < d["edge"] < MIN_EDGE


def test_allows_book_one_fill_each_with_fees_from_fees_py(run):
    alpha, bravo = decision(run, "alpha-allow"), decision(run, "bravo-allow")
    assert (alpha["allow"], alpha["reasons"], alpha["booked_rows"]) == (True, [], 1)
    assert (bravo["allow"], bravo["reasons"], bravo["booked_rows"]) == (True, [], 1)
    assert alpha["fill"]["effective_price"] == 0.415 and alpha["fee_usd"] == 0.0
    assert bravo["edge"] == pytest.approx(0.70 - 0.615 - 0.02 * 0.615)
    # issue #104: shares = min(intent_builder's shares, what fits 1% of 1000 all-in), on the 0.01 grid
    assert bravo["fill"]["fee_usd"] == 0.196062  # max(notional * rate, shares * rate * price)
    assert (alpha["fill"]["shares"], alpha["fill"]["notional_usd"]) == (24.09, 9.99735)  # 10 / 0.415, fee-free
    assert (bravo["fill"]["shares"], bravo["fill"]["notional_usd"]) == (15.94, 9.8031)  # 10 / (0.615 + fee)
    for d in (alpha, bravo):
        assert d["fill"]["shares"] <= d["intent_builder"]["shares"]  # never above intent_builder's size
    for fill in (alpha["fill"], bravo["fill"]):
        assert fill["notional_usd"] + fill["fee_usd"] <= 10.0  # cost plus fee within the 1% cap


def test_cap_vetoes_a_sixth_leg(run):
    d = decision(run, "golf-cap")
    assert (d["allow"], d["reasons"]) == (False, ["max_open_positions"])
    assert run["actual"]["ledger_after_intents"]["open_legs"] == podlib.load_risk()["max_open_positions"] == 5


def test_cash_and_positions(run):
    ledger = run["actual"]["ledger_after_intents"]
    assert ledger["cash_usd"] == pytest.approx(949.803488)
    assert ledger["positions"]["e2e-alpha|71001"] == {"cost_basis_usd": 9.99735, "shares": 24.09}
    assert ledger["positions"]["e2e-bravo|72001"] == {"cost_basis_usd": 9.8031, "shares": 15.94}
    assert ledger["problems"] == [] and ledger["unique_rows"] == ledger["lines"] == 5


def test_duplicate_fill_replay_is_deduped(run):
    replay = run["actual"]["duplicate_replay"]
    main = run["actual"]["ledger_after_intents"]
    assert (replay["lines"], replay["unique_rows"], replay["duplicates_ignored"]) == (6, 5, 1)
    assert replay["problems"] == [] and replay["cash_unchanged"] and replay["positions_unchanged"]
    assert replay["load_state"] == {"duplicate_fill_rows": 1, "ledger_problems": [], "leg_already_open": True,
                                    "open_positions": 5}
    then = replay["then"]
    assert then["allow"] is True and then["booked_rows"] == 1
    assert replay["ledger"]["cash_usd"] == pytest.approx(main["cash_usd"] - 9.99735)
    assert replay["ledger"]["positions"]["e2e-alpha|71001"]["cost_basis_usd"] == pytest.approx(19.9947)


def test_corrupted_ledger_before_the_call_vetoes_and_books_nothing(run):
    torn = run["actual"]["corrupt_at_start"]
    assert (torn["allow"], torn["reasons"], torn["booked_rows"]) == (False, ["invalid_risk_state"], 0)
    assert torn["ledger_unchanged"] is True
    # Task AL gap 7: invalid_risk_state is now a persisted global halt that cancels every open quote.
    assert torn["halted"] is True and torn["halt_reason"] == "invalid_risk_state"
    assert torn["global_halts"] == [{"market": "", "reason": "invalid_risk_state", "scope": "global",
                                     "source": "Sentinel"}]
    assert torn["open_quotes_after"] == 0


def test_corrupted_ledger_during_the_call_halts_and_books_nothing(run):
    mid = run["actual"]["corrupt_mid_write"]
    assert (mid["allow"], mid["halted"], mid["halt_reason"], mid["booked_rows"]) == (False, True, "ledger_integrity", 0)
    assert "unterminated_last_row" in mid["ledger_problems"]
    assert mid["ledger_is_start_plus_tear"] is True
    halts = [r for r in bus_rows(run, "halt") if r["from"] == "Executor"]
    assert len(halts) == 1 and halts[0]["payload"]["exchange_action"] == "none"
    # Task AL gap 7: the Executor's ledger halt is a persisted global halt too.
    assert halts[0]["payload"]["scope"] == "global"
    assert mid["global_halts"] == [{"market": "", "reason": "ledger_integrity", "scope": "global",
                                    "source": "Executor"}]


def test_two_consecutive_stale_checks_halt_through_sentinel(run):
    first, second = run["actual"]["cycle_1"], run["actual"]["cycle_2"]
    assert first["stale_markets"] == second["stale_markets"] == ["e2e-charlie"]
    assert first["sentinel_halts"] == [] and second["sentinel_halts"] == ["e2e-charlie"]
    halts = [r for r in bus_rows(run, "halt") if r["from"] == "Sentinel" and r["payload"]["scope"] == "market"]
    assert [(h["market"], h["payload"]["paper_quotes_cancelled"], h["payload"]["exchange_action"]) for h in halts] \
        == [("e2e-charlie", 1, "none")]
    quotes = {q["market"]: q for q in run["actual"]["quotes_after"]}
    assert quotes["e2e-charlie"]["status"] == "cancelled" and quotes["e2e-charlie"]["cancelled_by"] == "Sentinel"
    assert quotes["e2e-charlie"]["cancelled_ts"] == run["actual"]["clock"]["second_check"]
    assert quotes["e2e-foxtrot"]["status"] == "open"  # a halt cancels only the halted market's quotes


# ---------------------------------------------------------------- Task AL: halt consumer

def test_halt_blocks_later_intents_and_the_maker(run):
    a = run["actual"]["halt_consumer"]["a_halt_blocks_later_intents"]
    assert a["active"] == [{"market": "e2e-charlie", "scope": "market", "source": "Sentinel",
                            "reason": "stale_book_consecutive: 2 consecutive stale or failed book checks "
                                      "(last: stale_book)"}]
    assert a["commit"]["allow"] is False and a["commit"]["booked_rows"] == 0
    assert a["commit"]["reasons"][0] == "halt_market"
    assert a["maker_refused"] is True and a["maker_quotes_written"] == 0
    assert a["other_market_reasons"] == []  # a market halt never blocks another market


def test_corrupt_halt_file_blocks_every_market(run):
    b = run["actual"]["halt_consumer"]["b_corrupt_halt_file_blocks_globally"]
    # bravo-allow was allowed on the same ledger start; the corrupt halt file alone now vetoes it
    assert decision(run, "bravo-allow")["allow"] is True
    assert (b["commit"]["allow"], b["commit"]["reasons"], b["commit"]["booked_rows"]) == \
        (False, ["halt_state_corrupt"], 0)
    assert b["ledger_unchanged"] is True and b["corrupt_file_kept"] is True
    assert b["cycle_gaps"] == 0 and "e2e-alpha" in b["cycle_halted_markets"]


def test_market_resume_cannot_lift_a_global_halt_and_only_the_operator_resumes(run):
    c = run["actual"]["halt_consumer"]["c_d_resume_rules"]
    by = {a["label"]: (a["released"], a["reason"]) for a in c["attempts"]}
    assert by["market_resume_of_global"] == (False, "market_resume_cannot_lift_global")
    assert by["sentinel_resume"] == (False, "resume_not_operator")
    assert by["executor_resume_of_market"] == (False, "resume_not_operator")
    assert by["operator_without_approval"] == (False, "missing_approved_by")
    assert c["bus_refused_non_operator_resume"] is True
    assert len(c["active_after_rejected_resumes"]) == 2  # nothing was released by a rejected resume
    assert by["operator_market_resume"] == (True, None)
    assert c["after_market_resume"]["other_market_reasons"] == ["halt_global"]  # global still blocks it
    assert by["operator_global_resume"] == (True, None) and c["active_after_global_resume"] == []
    resumes = bus_rows(run, "resume")
    assert len(resumes) == 2 and all(r["from"] == "Operator" and r["payload"]["approved_by"] == "e2e-operator"
                                     and r["payload"]["halt_ref"]["id"] for r in resumes)


# ---------------------------------------------------------------- Task AO: intent_builder shadow (phase 1)

SHADOW_EXPECTED = {  # SPEC_signal_to_intent 9.3 phase 1
    "charlie-stale": ["stale_book"],
    "delta-nan": ["non_positive_edge"],  # the NaN YES ask cannot be walked; the NO side has no edge
    "echo-wash": ["sell_requires_position"],
    "foxtrot-thin-edge": ["edge_below_min"],
    "alpha-allow": [],
    "bravo-allow": [],
    "golf-cap": [],  # the position cap stays RiskGate's
}


def test_intent_builder_shadow_reasons_per_scenario(run):
    for label, reasons in SHADOW_EXPECTED.items():
        shadow = decision(run, label)["intent_builder"]
        assert shadow["reasons"] == reasons, label
        assert shadow["intent"] is (not reasons), label


def test_intent_builder_shadow_allows_are_capped_and_kelly_is_shadow(run):
    for label in ("alpha-allow", "bravo-allow", "golf-cap"):
        shadow = decision(run, label)["intent_builder"]
        assert shadow["outcome_index"] == 0 and shadow["size_pct"] == 0.01
        assert shadow["kelly_frac"] > 0.01 and shadow["marks"] == ["top_of_book_only"]
    assert decision(run, "alpha-allow")["intent_builder"]["kelly_frac"] == pytest.approx(0.15 * 0.055 / 0.585)


def test_intent_builder_shadow_does_not_change_riskgate_decisions(run, golden):
    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k != "intent_builder"}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value
    assert strip(run["actual"]) == strip(golden)
    assert [d["allow"] for d in run["actual"]["decisions"]] == [False, False, False, False, True, True, False]


def test_a_looser_intent_builder_breaks_the_gate(monkeypatch):
    """If the builder produced an intent RiskGate vetoes for an edge reason, the run reports it."""
    from src import intent_builder

    monkeypatch.setattr(intent_builder, "MIN_EDGE", -1.0)  # never edge_below_min, and no edge floor at all
    monkeypatch.setattr(intent_builder, "EDGE_TOLERANCE", 0.0)
    result = harness.run_once(FIXTURES)
    assert any("intent_builder produced an intent RiskGate vetoes" in p for p in result["problems"])


# ---------------------------------------------------------------- invariants

def test_conservation_holds_and_fill_ids_are_unique(run):
    for view in (run["detail"]["main_ledger"], run["detail"]["after_replay"]):
        read = ledger_store.parse_ledger(view["bytes"])
        assert ledger_store.verify(read) == []
        assert len(read.ids) == len(set(read.ids))
    booked = run["detail"]["booked_ids"]
    assert len(booked) == len(set(booked)) == 3
    assert all(re.fullmatch(r"[0-9a-f]{32}", fill_id) for fill_id in booked)


def test_no_exchange_action_anywhere(run):
    rows = run["detail"]["bus"]
    assert {r["type"] for r in rows} <= {"signal", "veto", "fill", "halt", "resume"}
    assert all(r["from"] == "Operator" and r["to"] == "Sentinel" for r in rows if r["type"] == "resume")
    assert all(r["type"] == "resume" for r in rows if r["from"] == "Operator")  # Operator sends nothing else
    assert all((r["payload"] or {}).get("exchange_action", "none") == "none" for r in rows)
    assert all((r["payload"] or {}).get("order", False) is False for r in rows)
    fills = [r for r in rows if r["type"] == "fill"]
    assert [f["payload"]["fill_id"] for f in fills] == run["detail"]["booked_ids"]
    assert all(f["from"] == "Executor" and f["payload"]["risk_decision"] == "allow" for f in fills)


def test_no_network_call(run):
    assert run["detail"]["attempts"] == [] and run["actual"]["invariants"]["network_attempts"] == 0


def test_risk_toml_untouched_and_live_disabled_throughout(run):
    assert hashlib.sha256(RISK.read_bytes().replace(b"\r\n", b"\n")).hexdigest() == PINNED
    assert run["actual"]["invariants"]["risk_sha256"] == PINNED
    assert RISK.read_bytes() == RISK_BEFORE
    assert run["actual"]["invariants"]["live_enabled"] == ["False"]
    assert run["actual"]["invariants"]["live_checks"] >= 12  # every cycle and every commit re-read it
    assert podlib.load_risk()["live_enabled"] is False


# ---------------------------------------------------------------- the harness itself

def test_tripwires_record_and_raise_then_restore(tmp_path):
    real_socket = socket.socket
    clock = harness.Clock(datetime(2026, 9, 1, 12, tzinfo=timezone.utc))
    with harness.offline_sandbox(tmp_path, clock) as box:
        calls = [lambda: socket.create_connection(("127.0.0.1", 9)), lambda: socket.socket(),
                 lambda: urllib.request.urlopen("https://clob.polymarket.com/book?token_id=1"),
                 lambda: podlib.public_get("https://clob.polymarket.com/book?token_id=1")]
        for call in calls:
            with pytest.raises(harness.NetworkTouched):
                call()
        try:  # a caught touch is still recorded
            socket.getaddrinfo("example.invalid", 443)
        except Exception:  # noqa: BLE001
            pass
        assert box["attempts"] == ["socket.create_connection", "socket.socket", "urllib.request.urlopen",
                                   "podlib.urlopen", "socket.getaddrinfo"]
    assert socket.socket is real_socket


def test_a_swallowed_network_touch_still_fails_the_run(monkeypatch):
    from src import forecaster
    real = forecaster.emit_probability

    def leaky(**kwargs):
        try:
            podlib.public_get("https://clob.polymarket.com/book?token_id=1")
        except Exception:  # noqa: BLE001 - a regression that hides the fetch
            pass
        return real(**kwargs)

    monkeypatch.setattr(forecaster, "emit_probability", leaky)
    result = harness.run_once(FIXTURES)
    assert any(problem.startswith("network touched") for problem in result["problems"])


def test_clock_is_frozen_inside_and_restored(tmp_path):
    now = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    clock = harness.Clock(now)
    real = podlib.datetime
    with harness.offline_sandbox(tmp_path, clock):
        assert podlib.utc_now() == "2026-09-01T12:00:00Z"
        assert wash_detect._now(None) == now
        assert book_staleness.book_age_problem("2026-09-01T11:58:01Z", None, 120.0) is None
        assert book_staleness.book_age_problem("2026-09-01T11:57:59Z", None, 120.0) == "stale_book"
        assert isinstance(datetime.now(timezone.utc), book_staleness.datetime)  # real datetimes still pass
        clock.now = now + timedelta(seconds=30)
        assert podlib.utc_now() == "2026-09-01T12:00:30Z"
    assert podlib.datetime is real and book_staleness.datetime is real and wash_detect.datetime is real


def test_the_fixture_clock_is_far_from_the_wall_clock():
    """An unpatched clock read would make every fresh book stale and break golden."""
    now = harness.parse_now(json.loads((FIXTURES / "scenario.json").read_text(encoding="utf-8"))["now"])
    assert datetime.now(timezone.utc) - now > timedelta(days=7)


def test_default_data_paths_are_redirected(tmp_path):
    clock = harness.Clock(datetime(2026, 9, 1, 12, tzinfo=timezone.utc))
    from src import paper_cycle, paper_ledger, sentinel
    with harness.offline_sandbox(tmp_path, clock) as box:
        assert podlib.BUS_PATH == box["bus"] and paper_ledger.PAPER_PATH == box["paper"]
        assert paper_ledger.QUOTES_PATH == sentinel.QUOTES_PATH == paper_cycle.QUOTES_PATH == box["quotes"]
        assert str(paper_cycle.SENTINEL_STATE_PATH).startswith(str(box["unused"]))
    assert podlib.BUS_PATH != box["bus"]


@pytest.mark.parametrize("path,value", [
    (("decisions", 0, "reasons"), []),
    (("decisions", 4, "allow"), False),
    (("ledger_after_intents", "cash_usd"), 949.61),
    (("cycle_2", "sentinel_halts"), []),
    (("duplicate_replay", "duplicates_ignored"), 0),
    (("corrupt_mid_write", "halted"), False),
    (("bus", "counts", "Executor>Sentinel:fill"), 4),
    (("invariants", "network_attempts"), 1),
])
def test_any_golden_edit_is_caught(run, golden, path, value):
    edited = copy.deepcopy(golden)
    target = edited
    for key in path[:-1]:
        target = target[key]
    assert target[path[-1]] != value
    target[path[-1]] = value
    assert harness.golden_problems(run["actual"], edited)


def test_golden_compare_is_exact():
    assert harness.golden_problems({"a": 1}, {"a": 1.0})  # int vs float is a difference
    assert harness.golden_problems({"a": [1, 2]}, {"a": [1]})
    assert harness.golden_problems({"a": 1}, {"a": 1, "b": 2})
    assert harness.golden_problems({"a": 1, "b": 2}, {"a": 1})
    assert harness.golden_problems({"a": {"b": "x"}}, {"a": {"b": "x"}}) == []


def _temp_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(FIXTURES, root / "tests" / "fixtures" / "e2e")
    (root / "config").mkdir()
    shutil.copy2(RISK, root / "config" / "risk.toml")
    return root


def test_the_gate_fails_on_a_golden_mismatch_and_never_writes_golden(tmp_path):
    root = _temp_root(tmp_path)
    golden_path = root / "tests" / "fixtures" / "e2e" / "golden.json"
    data = json.loads(golden_path.read_text(encoding="utf-8"))
    data["decisions"][0]["reasons"] = []
    golden_path.write_text(json.dumps(data), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in golden_path.parent.iterdir()}
    problems, _ = harness.gate(root)
    assert problems == ["$.decisions[0].reasons: 1 items != golden 0"]
    assert {p.name: p.read_bytes() for p in golden_path.parent.iterdir()} == before


@pytest.mark.parametrize("name", [*harness.INPUT_FILES, harness.GOLDEN_NAME])
def test_the_gate_fails_on_a_missing_fixture(tmp_path, name):
    root = _temp_root(tmp_path)
    (root / "tests" / "fixtures" / "e2e" / name).unlink()
    problems, _ = harness.gate(root)
    assert problems == [f"missing fixture tests/fixtures/e2e/{name}"]


def test_a_crlf_checkout_of_the_pinned_risk_file_passes(tmp_path):
    root = _temp_root(tmp_path)
    risk = root / "config" / "risk.toml"
    risk.write_bytes(risk.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert harness.gate(root)[0] == []


def test_a_crlf_checkout_of_the_fixtures_passes(tmp_path):
    root = _temp_root(tmp_path)
    for path in (root / "tests" / "fixtures" / "e2e").iterdir():
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert harness.gate(root)[0] == []


def test_fixtures_keep_lf_on_checkout():
    text = (FIXTURES / ".gitattributes").read_text(encoding="utf-8")
    assert "* text eol=lf" in text.splitlines()


def test_the_gate_fails_when_risk_toml_drifts(tmp_path):
    root = _temp_root(tmp_path)
    risk = root / "config" / "risk.toml"
    risk.write_bytes(risk.read_bytes() + b"\n")
    problems, _ = harness.gate(root)
    assert any("risk.toml sha256" in problem for problem in problems)


# ---------------------------------------------------------------- --update-golden

def test_update_golden_is_off_by_default(request):
    tree = ast.parse((ROOT / "tests" / "conftest.py").read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
             and getattr(node.func, "attr", "") == "addoption"
             and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "--update-golden"]
    assert len(calls) == 1
    kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in calls[0].keywords if kw.arg in {"action", "default"}}
    assert kwargs == {"action": "store_true", "default": False}
    # On only when this very run passed the flag.
    passed = any(str(arg) == "--update-golden" for arg in request.config.invocation_params.args)
    assert request.config.getoption("--update-golden") is passed


def test_nothing_but_the_flag_can_write_golden():
    source = (ROOT / "scripts" / "e2e_offline.py").read_text(encoding="utf-8")
    assert "environ" not in source and "getenv" not in source
    tree = ast.parse(source)
    callers = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
               and any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "write_golden" for c in ast.walk(node))}
    assert callers == set()  # the harness never calls write_golden; only the test above does, under the flag
    this = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    writers = [node.name for node in ast.walk(this) if isinstance(node, ast.FunctionDef)
               and any(isinstance(c, ast.Attribute) and c.attr == "write_golden" for c in ast.walk(node))]
    assert writers == ["test_the_run_matches_golden"]


def test_golden_is_canonical_strict_json():
    text = GOLDEN.read_text(encoding="utf-8")
    data = json.loads(text, parse_constant=lambda name: pytest.fail(f"non-strict JSON constant {name}"))
    assert harness.golden_text(data) == text


def test_the_flag_is_documented():
    for rel in ("docs/PHASE0_RUN.md", "tests/fixtures/e2e/README.md"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "--update-golden" in text and "python -m pytest tests/test_e2e_offline.py" in text, rel


# ---------------------------------------------------------------- fixtures are sanitised and consistent

def test_fixtures_are_sanitised():
    banned = re.compile(r"api[_-]?key|secret|passphrase|signature|private[_-]?key|0x[0-9a-fA-F]{40}|\.env\b")
    for path in FIXTURES.iterdir():
        assert not banned.search(path.read_text(encoding="utf-8")), path.name
    markets = json.loads((FIXTURES / "gamma_markets.json").read_text(encoding="utf-8"))
    assert all(row["id"].startswith("e2e-") for row in markets)


def test_the_start_ledger_is_a_valid_ledger():
    read = ledger_store.parse_ledger((FIXTURES / "ledger_start.jsonl").read_bytes().replace(b"\r\n", b"\n"))
    assert read.problems == [] and ledger_store.verify(read) == []
    assert read.ids == ["e2e-start-0001", "e2e-start-0002", "e2e-start-0003"]


# ---------------------------------------------------------------- Task AR: daily loss

def test_daily_loss_cases_by_meaning(run):
    section = run["actual"]["daily_loss"]
    assert set(section) == {"a_loss_closed_today", "b_loss_closed_yesterday", "c_gain_does_not_offset",
                            "d_below_cap", "e_fees_plus_loss", "f_unparseable_ts"}
    for name in ("a_loss_closed_today", "c_gain_does_not_offset", "e_fees_plus_loss"):
        case = section[name]
        assert (case["allow"], case["reasons"], case["booked_rows"], case["halted"]) == (
            False, ["max_daily_loss"], 0, False), name
    assert section["a_loss_closed_today"]["state"]["daily_realized_loss_usd"] == 31.0
    assert section["b_loss_closed_yesterday"]["state"]["daily_loss_usd"] == 0.0
    assert section["c_gain_does_not_offset"]["state"]["daily_realized_gain_usd"] == 40.0
    assert section["c_gain_does_not_offset"]["state"]["daily_loss_usd"] == 31.0
    assert section["e_fees_plus_loss"]["state"]["daily_fees_usd"] == 0.2
    for name in ("b_loss_closed_yesterday", "d_below_cap"):
        assert (section[name]["allow"], section[name]["reasons"]) == (True, []), name
    f = section["f_unparseable_ts"]
    assert (f["allow"], f["reasons"], f["halted"], f["ledger_unchanged"]) == (False, ["invalid_risk_state"], True, True)
    assert f["state"]["daily_loss_usd"] is None
    assert [h["scope"] for h in f["global_halts"]] == ["global"]
    assert not [p for p in run["problems"] if p.startswith("daily_loss")]


# ---------------------------------------------------------------- Task AU: slippage floor

def test_slippage_floor_cases_by_meaning(run):
    section = run["actual"]["slippage_floor"]
    cases = section["cases"]
    assert section["floor"] == 0.005
    for name in ("a_zero", "b_just_below"):
        assert (cases[name]["allow"], cases[name]["reasons"]) == (False, ["slippage_below_floor"]), name
    for name in ("c_at_floor", "d_above"):
        assert (cases[name]["allow"], cases[name]["reasons"]) == (True, []), name
    assert "non_finite_assumed_slippage" in cases["e_nan"]["reasons"] and cases["e_nan"]["allow"] is False
    assert "invalid_assumed_slippage" in cases["f_missing"]["reasons"] and cases["f_missing"]["allow"] is False
    assert section["ledger_unchanged"] is True and section["global_halts"] == []
    builder = section["intent_builder"]
    assert (builder["allow"], builder["assumed_slippage"], builder["reasons_riskgate"]) == (True, 0.005, [])
    assert section["cli"] == {"0": "refused_exit_2", "0.0049": "refused_exit_2", "nan": "refused_exit_2",
                              "-0.01": "refused_exit_2", "0.005": "accepted", "0.01": "accepted"}
    assert not [p for p in run["problems"] if p.startswith("slippage_floor")]
