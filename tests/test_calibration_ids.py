"""Task AX (M3 calibration step 2): identity fields on forecasts, scanner rows and paper fills.

Offline, temp paths only. Data plumbing: no RiskGate decision, threshold or size may move.
"""
from __future__ import annotations

import ast
import dataclasses
import json
from pathlib import Path

import pytest

from src import calibration_ids as ci
from src import forecaster, halt_store, intent_builder, paper_ledger, podlib, scanner
from src.podlib import load_risk, read_jsonl
from src.risk_gate import Intent, evaluate

ROOT = Path(__file__).resolve().parents[1]
STATE = {"open_positions": 0, "leg_already_open": False, "exposure_usd": 0.0, "leg_exposure_usd": 0.0, "daily_loss_usd": 0.0, "recent_fills": [], "own_quotes": []}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", tmp_path / "paper.jsonl")
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", tmp_path / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", tmp_path / "HALT")
    sent: list = []
    monkeypatch.setattr(paper_ledger, "bus", lambda *a, **k: sent.append(a))
    return {"tmp": tmp_path, "bus": tmp_path / "bus.jsonl", "forecasts": tmp_path / "forecasts.jsonl", "sent": sent}


def emit(paths, **kw):
    base = dict(market="m1", model_prob=0.6, token_id="t-yes", outcome_index=0, outcome="Yes", condition_id="0xc1",
                p_low=0.55, p_high=0.65, category="Politics", tags=["politics", "us"], end_date="2026-12-01T00:00:00Z",
                bus_path=paths["bus"], forecasts_path=paths["forecasts"])
    base.update(kw)
    return forecaster.emit_probability(**base)


# ------------------------------------------------------------------ ids

def test_forecast_id_is_deterministic_and_content_bound():
    kw = dict(source="news", ts="2026-10-09T00:00:00Z", market_id="m", condition_id="0xc", outcome_index=0,
              token_id="t", model_prob=0.6, p_low=None, p_high=None, model_version="v1")
    a, b = ci.make_forecast_id(**kw), ci.make_forecast_id(**kw)
    assert a == b and a.startswith("fc_") and len(a) == 3 + ci.ID_HEX_CHARS
    for key, value in [("ts", "2026-10-09T00:00:01Z"), ("model_prob", 0.61), ("token_id", "u"),
                       ("model_version", "v2"), ("outcome_index", 1)]:
        assert ci.make_forecast_id(**{**kw, key: value}) != a, key


def test_decision_id_is_derived_never_invented():
    d = ci.make_decision_id("s", "m", "t", "fc_x")
    assert d == ci.make_decision_id("s", "m", "t", "fc_x") and d.startswith("dc_")
    assert d != ci.make_decision_id("s", "m", "t2", "fc_x")
    for parts in [("s", "m", "t", None), ("s", "m", "", "fc_x"), ("", "m", "t", "fc"), ("s", None, "t", "fc")]:
        assert ci.make_decision_id(*parts) is None


def test_scanner_category_and_tags_are_gammas_own():
    assert ci.scanner_category({"category": " Sports "}) == "Sports"
    assert ci.scanner_category({}) is None and ci.scanner_category({"category": ""}) is None
    assert ci.scanner_category({"category": 3}) is None
    assert ci.scanner_tags({}) is None and ci.scanner_tags({"tags": "x"}) is None
    assert ci.scanner_tags({"tags": []}) == []
    tags = [{"slug": "us", "label": "US"}, {"label": "World"}, {"id": 1}, "raw", {"slug": "us"}]
    assert ci.scanner_tags({"tags": tags}) == ["us", "World", "raw"]


def test_the_module_is_stdlib_only():
    tree = ast.parse((ROOT / "src" / "calibration_ids.py").read_text(encoding="utf-8"))
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert mods <= {"__future__", "hashlib", "json", "math", "os", "pathlib", "typing"}, mods


# ------------------------------------------------------------------ logs/forecasts.jsonl

def test_emit_seals_the_forecast_before_the_signal(paths):
    message = emit(paths)
    rows = read_jsonl(paths["forecasts"])
    assert len(rows) == 1 and tuple(rows[0]) == ci.FORECAST_ROW_FIELDS
    row = rows[0]
    payload = message["payload"]
    assert row["forecast_id"] == payload["forecast_id"] and row["ts"] == payload["forecast_ts"]
    assert row["model_version"] == payload["model_version"] == forecaster.MODEL_VERSION
    assert row["sealed_before_book_read"] is True and row["calibration_problems"] == []
    assert row["schema_version"] == ci.SCHEMA_VERSION
    assert (row["market_id"], row["condition_id"], row["token_id"], row["outcome_index"]) == ("m1", "0xc1", "t-yes", 0)
    assert row["tags"] == ["politics", "us"] and row["category"] == "Politics"
    # signal->intent ruling: outcome, token and interval ride in the payload with the forecast id
    for key, value in {"outcome_index": 0, "outcome": "Yes", "token_id": "t-yes", "condition_id": "0xc1",
                       "p_low": 0.55, "p_high": 0.65, "order": False}.items():
        assert payload[key] == value, key


def test_a_legacy_call_is_flagged_not_filled_in(paths):
    message = emit(paths, token_id=None, outcome_index=None, outcome=None, condition_id=None, p_low=None,
                   p_high=None, category=None, tags=None, end_date=None)
    row = read_jsonl(paths["forecasts"])[0]
    assert row["calibration_problems"] == ["missing_condition_id", "missing_end_date", "missing_outcome_index",
                                           "missing_token_id"]
    assert row["token_id"] is None and row["outcome_index"] is None and row["tags"] is None
    payload = message["payload"]
    # absent; Issue #105: intent_builder no longer reads that as outcome 0, the signal is rejected
    assert not {"outcome_index", "token_id", "condition_id", "p_low", "p_high", "outcome"} & set(payload)
    signal = intent_builder.signal_from_bus(message)
    assert signal.outcome_index is None and signal.token_id is None
    assert intent_builder.outcome_index_problems(signal) == ["missing_outcome_index", "missing_token_id"]


def test_the_signal_never_goes_out_unsealed(paths):
    paths["forecasts"].write_bytes(b'{"torn": ')
    with pytest.raises(ci.ForecastLogError):
        emit(paths)
    assert not paths["bus"].exists()
    assert paths["forecasts"].read_bytes() == b'{"torn": '


@pytest.mark.parametrize("bad", [dict(p_low=0.7), dict(p_high=0.5), dict(p_low=None), dict(outcome_index=-1),
                                 dict(outcome_index=True), dict(token_id=""), dict(model_version=""),
                                 dict(model_prob=float("nan")), dict(model_prob=1.0)])
def test_bad_forecasts_are_refused_before_anything_is_written(paths, bad):
    with pytest.raises(ValueError):
        emit(paths, **bad)
    assert not paths["bus"].exists() and not paths["forecasts"].exists()


def test_append_is_one_complete_line_and_idempotent(paths):
    emit(paths)
    row = read_jsonl(paths["forecasts"])[0]
    ci.append_forecast(paths["forecasts"], row)  # same forecast again: not written twice
    data = paths["forecasts"].read_bytes()
    assert data.count(b"\n") == 1 and data.endswith(b"\n")
    with pytest.raises(ci.ForecastLogError):
        ci.append_forecast(paths["forecasts"], {**row, "extra": 1})
    with pytest.raises(ci.ForecastLogError):
        ci.append_forecast(paths["forecasts"], {**row, "forecast_id": "fc_other", "model_prob": float("nan")})
    assert paths["forecasts"].read_bytes() == data


def test_idempotence_reads_the_row_not_the_bytes(paths):
    """Whitespace and key order on file are the same seal; a row's own top-level id is what counts."""
    emit(paths)
    row = read_jsonl(paths["forecasts"])[0]
    formatted = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(" , ", " : ")) + "\n"
    assert b'"forecast_id" : ' in formatted.encode("utf-8")  # the old byte needle would not have matched
    paths["forecasts"].write_text(formatted, encoding="utf-8")
    ci.append_forecast(paths["forecasts"], row)  # identical content, other formatting: not written twice
    assert paths["forecasts"].read_text(encoding="utf-8") == formatted
    other = {**row, "forecast_id": "fc_other"}
    paths["forecasts"].write_text(json.dumps(other, ensure_ascii=False, separators=(",", ":")) + "\n",
                                  encoding="utf-8")
    ci.append_forecast(paths["forecasts"], row)  # another id on file is not this seal
    assert [r["forecast_id"] for r in read_jsonl(paths["forecasts"])] == ["fc_other", row["forecast_id"]]


def test_true_and_one_are_not_the_same_content(paths):
    emit(paths)
    row = read_jsonl(paths["forecasts"])[0]
    stored = json.dumps({**row, "sealed_before_book_read": 1}, ensure_ascii=False, separators=(",", ":")) + "\n"
    paths["forecasts"].write_text(stored, encoding="utf-8")
    with pytest.raises(ci.ForecastLogError):
        ci.append_forecast(paths["forecasts"], row)  # true != 1: different content, same id
    assert paths["forecasts"].read_text(encoding="utf-8") == stored


@pytest.mark.parametrize("other", [{"category": "Sports"}, {"note": "a second take"},
                                   {"end_date": "2027-01-01T00:00:00Z"}])
def test_a_same_id_line_with_other_content_is_refused(paths, other):
    emit(paths)
    row = read_jsonl(paths["forecasts"])[0]
    stored = json.dumps({**row, **other}, ensure_ascii=False, separators=(",", ":")) + "\n"
    paths["forecasts"].write_text(stored, encoding="utf-8")
    with pytest.raises(ci.ForecastLogError):
        ci.append_forecast(paths["forecasts"], row)
    assert paths["forecasts"].read_text(encoding="utf-8") == stored


def test_every_same_id_row_is_read_before_idempotence_is_decided(paths):
    emit(paths)
    row = read_jsonl(paths["forecasts"])[0]
    conflict = {**row, "note": "not what the first line says"}
    text = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in (row, conflict))
    paths["forecasts"].write_text(text, encoding="utf-8")
    with pytest.raises(ci.ForecastLogError):
        ci.append_forecast(paths["forecasts"], row)  # the first line matched; the second one still wins
    assert paths["forecasts"].read_text(encoding="utf-8") == text


@pytest.mark.parametrize("data", [
    b'{"forecast_id": "fc_a"}\nnot json\n',       # a complete line that is not JSON
    b'{"forecast_id": "fc_a"}\n[]\n',             # a complete line that is not an object
    b'{"forecast_id": "fc_a"}\n"text"\n',
    b'{"forecast_id": "fc_a"}\n\n',               # a blank line
    b'{"forecast_id": "fc_a"}\n{"no_id": 1}\n',   # a row without a usable forecast_id
    b'{"forecast_id": "fc_a"}\n{"forecast_id": NaN}\n',
    b'{"forecast_id": "fc_a"}\n{"forecast_id": "fc_b"}\xff\xfe\n',  # not UTF-8
])
def test_a_damaged_log_is_never_appended_to(paths, data):
    emit(paths)
    row = read_jsonl(paths["forecasts"])[0]
    paths["forecasts"].write_bytes(data)
    with pytest.raises(ci.ForecastLogError):
        ci.append_forecast(paths["forecasts"], row)
    assert paths["forecasts"].read_bytes() == data


@pytest.mark.parametrize("change", [{"category": "Sports"}, {"note": "a second take"},
                                    {"end_date": "2027-01-01T00:00:00Z"}])
def test_the_forecaster_never_signals_a_refused_seal(paths, monkeypatch, change):
    """The same content keeps the same forecast_id; other content under it is refused before the bus."""
    monkeypatch.setattr(forecaster, "utc_now", lambda: "2026-10-10T00:00:00Z")
    first = emit(paths)
    # category, note and end_date are not part of the id, so only the sealed content differs
    assert first["payload"]["forecast_id"] == ci.make_forecast_id(
        source=forecaster.SOURCE, ts="2026-10-10T00:00:00Z", market_id="m1", condition_id="0xc1",
        outcome_index=0, token_id="t-yes", model_prob=0.6, p_low=0.55, p_high=0.65,
        model_version=forecaster.MODEL_VERSION)
    forecasts = paths["forecasts"].read_bytes()
    bus = paths["bus"].read_bytes()
    with pytest.raises(ci.ForecastLogError):
        emit(paths, **change)
    assert paths["forecasts"].read_bytes() == forecasts
    assert paths["bus"].read_bytes() == bus and len(read_jsonl(paths["bus"])) == 1
    assert read_jsonl(paths["bus"])[0]["payload"]["forecast_id"] == first["payload"]["forecast_id"]


def test_forecasts_path_is_the_logs_default():
    assert podlib.FORECASTS_PATH == podlib.LOG_DIR / "forecasts.jsonl"


# ------------------------------------------------------------------ scanner rows

def test_scanner_rows_carry_category_and_tags():
    market = {"id": "1", "conditionId": "0xc", "version": "v1", "clobTokenIds": '["a","b"]', "outcomes": '["Yes","No"]',
              "outcomePrices": '["0.4","0.6"]', "endDate": "2026-12-01T00:00:00Z", "category": "Sports",
              "tags": [{"slug": "nba", "label": "NBA"}]}
    row = scanner.normalize(market, "2026-10-09T00:00:00Z")
    assert row["category"] == "Sports" and row["tags"] == ["nba"]
    bare = scanner.normalize({k: v for k, v in market.items() if k not in ("category", "tags")}, "2026-10-09T00:00:00Z")
    assert bare["category"] is None and bare["tags"] is None
    assert "include_tag=true" in scanner.GAMMA_KEYSET and "closed=false" in scanner.GAMMA_KEYSET


def test_tags_never_filter_a_market():
    src = (ROOT / "src" / "scanner.py").read_text(encoding="utf-8")
    body = src[src.index("def rejection_reasons"):src.index("def ", src.index("def rejection_reasons") + 10)]
    assert "tags" not in body and "category" not in body


# ------------------------------------------------------------------ paper fills

def _ok_intent(**kw) -> Intent:
    base = dict(strategy="news-forecaster", market="m1", side="buy", model_prob=0.7, market_price=0.4,
                assumed_slippage=0.005, fee_rate=0.02, liquidity_usd=20000.0, spread=0.01, token_id="t-yes",
                book_ts=podlib.utc_now(), forecast_id="fc_abc", forecast_ts="2026-10-09T00:00:00Z",
                condition_id="0xc1", outcome_index=0, outcome="Yes", category=None, tags=("us",),
                end_date="2026-12-01T00:00:00Z", model_version="v1",
                best_bid=0.38, best_ask=0.4, kelly_fraction_used=0.15,
                kelly_frac_raw=0.1, fill_context="intent_builder")  # Task BC
    base.update(kw)
    base.setdefault("tags", ("test-market",))  # issue #131: a tagged market (missing tags veto)
    return Intent(**base)


def test_a_full_identity_fill_is_joinable(paths):
    result = paper_ledger.commit_if_allowed(_ok_intent(), load_risk())
    assert result["allow"] is True
    fill = read_jsonl(paths["tmp"] / "paper.jsonl")[0]
    assert all(key in fill for key in ci.FILL_ID_FIELDS)
    assert fill["calibration_problems"] == []
    assert fill["decision_id"] == ci.make_decision_id("news-forecaster", "m1", "t-yes", "fc_abc")
    assert fill["tags"] == ["us"] and fill["forecast_id"] == "fc_abc"


def test_a_fill_without_ids_is_booked_and_flagged(paths):
    bare = _ok_intent(forecast_id=None, forecast_ts=None, condition_id=None, outcome_index=None, outcome=None,
                      end_date=None, model_version=None, best_bid=None, best_ask=None,
                      kelly_fraction_used=None, kelly_frac_raw=None)
    result = paper_ledger.commit_if_allowed(bare, load_risk())
    # RiskGate decided; identity is never a veto. (Issue #131: tags are no longer identity only, they are the
    # category input RiskGate checks, so the bare intent keeps its tags; missing tags are tested in test_category.)
    assert result["allow"] is True
    fill = read_jsonl(paths["tmp"] / "paper.jsonl")[0]
    assert fill["decision_id"] is None and fill["forecast_id"] is None  # not invented
    assert fill["calibration_problems"] == ["missing_best_ask", "missing_best_bid", "missing_condition_id",
                                            "missing_decision_id", "missing_end_date", "missing_forecast_id",
                                            "missing_forecast_ts", "missing_hours_to_expiry_at_entry",
                                            "missing_kelly_frac_raw", "missing_kelly_fraction_used",
                                            "missing_market_mid",
                                            "missing_model_version", "missing_outcome_index"]
    assert all(fill[k] is None for k in ci.FILL_MARKET_FIELDS)  # never invented


def test_a_wrong_decision_id_is_flagged(paths):
    paper_ledger.commit_if_allowed(_ok_intent(decision_id="dc_wrong"), load_risk())
    fill = read_jsonl(paths["tmp"] / "paper.jsonl")[0]
    assert fill["decision_id"] == "dc_wrong" and fill["calibration_problems"] == ["decision_id_mismatch"]


def test_identity_never_changes_a_risk_decision():
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    cfg = load_risk()
    for kw in [dict(), dict(model_prob=0.41), dict(spread=0.2), dict(liquidity_usd=10.0), dict(side="sell")]:
        identified = _ok_intent(**kw, book_ts=now.isoformat())
        full = evaluate(identified, cfg, dict(STATE), now=now)
        bare = evaluate(dataclasses.replace(identified, decision_id=None, forecast_id=None, forecast_ts=None,
                                            condition_id=None, outcome_index=None, outcome=None, end_date=None,
                                            model_version=None),
                        cfg, dict(STATE), now=now)
        assert dataclasses.asdict(full) == dataclasses.asdict(bare), kw


def test_risk_gate_never_reads_identity():
    src = (ROOT / "src" / "risk_gate.py").read_text(encoding="utf-8")
    body = src[src.index("def evaluate"):]
    # Issue #131: intent.tags / intent.category are now read, but only by the category check.
    for name in ("decision_id", "forecast_id", "forecast_ts", "model_version",
                 "intent.outcome", "intent.end_date", "intent.condition_id"):
        assert name not in body, name
    reads = [line.strip() for line in body.splitlines() if ("intent.tags" in line or "intent.category" in line)
             and not line.strip().startswith(("#", "Issue #131", "tags veto", "unreadable"))]
    assert reads == ["reasons.extend(category.category_problems(intent.category, intent.tags))"], reads


# ------------------------------------------------------------------ intent builder

def test_signal_from_bus_carries_the_forecast(paths):
    message = emit(paths)
    sig = intent_builder.signal_from_bus(message)
    assert sig.forecast_id == message["payload"]["forecast_id"] == sig.signal_id
    assert sig.forecast_ts == message["payload"]["forecast_ts"] and sig.model_version == forecaster.MODEL_VERSION
    assert (sig.p_low, sig.p_high, sig.token_id, sig.outcome_index) == (0.55, 0.65, "t-yes", 0)
    legacy = intent_builder.signal_from_bus({"type": "signal", "from": "NewsForecaster", "ts": "x", "market": "m",
                                             "payload": {"model_prob": 0.4}})
    assert legacy.forecast_id is None and legacy.model_version is None and legacy.signal_id  # hash, not a forecast id


def test_market_context_keeps_gamma_category_apart_from_the_ban_list():
    row = {"id": "m", "condition_id": "0xc", "token_ids": ["a", "b"], "outcomes": ["Yes", "No"],
           "end_date": "2026-12-01T00:00:00Z", "category": "Sports", "tags": ["nba"]}
    ctx = intent_builder.market_context_from_row(row, fee_rate=0.0)
    assert ctx.category is None and ctx.gamma_category == "Sports" and ctx.tags == ("nba",)
