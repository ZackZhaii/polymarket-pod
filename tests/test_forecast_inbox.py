"""Issue #118 (Task CD, M2-B): the NewsForecaster inbox ``poly_forecast_inbox/1`` and its anti-leak seal.

Offline: a frozen clock (forecaster's utc_now and the ``now`` passed in), a socket tripwire (no test here may open
a connection), every path in tmp_path. Covers every rejection reason, the duplicate and conflict rules, the
price-free candidates whitelist, and that the seal comes before any book read (the injected getter sees the
sealed row and the signal already written).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.forecast_inbox as fi
import src.forecaster as forecaster
from src.podlib import read_jsonl
from src.thresholds import FORECAST_MAX_ASOF_LAG_S, FORECAST_MIN_HALF_WIDTH, MIN_HOURS_TO_EXPIRY

NOW = datetime(2026, 10, 11, 3, 0, 0, tzinfo=timezone.utc)
SEAL_TS = "2026-10-11T03:00:00Z"
MARKET = {"id": "m1", "question": "Will X happen?", "condition_id": "0xc1", "token_ids": ["t-yes", "t-no"],
          "outcomes": ["Yes", "No"], "end_date": "2026-11-01T00:00:00Z", "closed": False, "accepting_orders": True,
          "uma_resolution_status": None, "uma_resolution_statuses": None, "category": "Politics",
          "tags": ["politics"], "event_id": "e1", "event_slug": "ev", "best_bid": 0.5, "best_ask": 0.52,
          "spread": 0.02, "price": 0.51, "outcome_prices": [0.51, 0.49], "volume_usd": 1e6,
          "volume_24h_usd": 5e4, "liquidity_usd": 6e4, "price_source": "gamma_bestBid_bestAsk_mid"}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    def tripwire(*args, **kwargs):
        raise AssertionError("network access in an offline test")
    monkeypatch.setattr(socket, "socket", tripwire)
    monkeypatch.setattr(socket, "create_connection", tripwire)
    monkeypatch.setattr(forecaster, "utc_now", lambda: SEAL_TS)


@pytest.fixture
def box(tmp_path):
    inbox = tmp_path / "inbox"
    return {"pending": inbox / "pending" / "2026-10-11", "archive": inbox / "archive",
            "forecasts": tmp_path / "forecasts.jsonl", "bus": tmp_path / "bus.jsonl"}


def good(**changes):
    row = {"schema": fi.INBOX_SCHEMA, "author": "Poly研判", "model_version": "poly-yanpan/2026-10-a",
           "market_id": "m1", "condition_id": "0xc1", "outcome_index": 0, "outcome": "Yes", "token_id": "t-yes",
           "p": 0.62, "p_low": 0.55, "p_high": 0.68, "as_of": "2026-10-11T02:30:00.000Z",
           "sources": [{"url": "https://www.reuters.com/world/x", "title": "t", "published_at": "2026-10-10T20:00:00Z",
                        "kind": "rss"}],
           "rationale": "Blind estimate from the public sources.", "market_price_seen": False}
    for key, value in changes.items():
        if value is _ABSENT:
            row.pop(key, None)
        else:
            row[key] = value
    return row


_ABSENT = object()


def deliver(box, row, name="0001.json", raw=None):
    box["pending"].mkdir(parents=True, exist_ok=True)
    path = box["pending"] / name
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(raw if raw is not None else json.dumps(row).encode("utf-8"))
    os.replace(tmp, path)
    return path


def ingest(box, path, markets=None, now=NOW, **kw):
    return fi.ingest_one(path, markets={"m1": MARKET} if markets is None else markets, now=now,
                         forecasts_path=box["forecasts"], bus_path=box["bus"], archive_dir=box["archive"], **kw)


# ------------------------------------------------------------------ the happy path


def test_a_valid_file_is_sealed_signalled_and_archived_read_only(box):
    path = deliver(box, good())
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    result = ingest(box, path)
    assert result.status == fi.SEALED and result.reasons == () and result.sha256 == sha
    rows = read_jsonl(box["forecasts"])
    assert len(rows) == 1 and rows[0]["note"] == f"inbox_sha256={sha}" and rows[0]["forecast_id"] == result.forecast_id
    assert (rows[0]["outcome_index"], rows[0]["token_id"], rows[0]["p_low"], rows[0]["p_high"]) == (0, "t-yes", 0.55, 0.68)
    assert (rows[0]["category"], rows[0]["end_date"], rows[0]["event_id"]) == ("Politics", MARKET["end_date"], "e1")
    signals = read_jsonl(box["bus"])
    assert len(signals) == 1 and signals[0]["payload"]["forecast_id"] == result.forecast_id
    assert signals[0]["payload"]["token_id"] == "t-yes" and signals[0]["payload"]["order"] is False
    archived = box["archive"] / "2026-10-11" / "0001.json"
    assert not path.exists() and archived.exists() and hashlib.sha256(archived.read_bytes()).hexdigest() == sha
    assert not os.stat(archived).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)


def test_the_interval_boundary_and_the_lag_boundary_pass(box):
    row = good(p_low=0.5, p=0.53, p_high=0.56,  # half-width 0.03 exactly (0.56 - 0.5 = 0.06000000000000005)
               as_of=(NOW - timedelta(seconds=FORECAST_MAX_ASOF_LAG_S)).strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    assert fi.validate_inbox_row(row, market_row=MARKET, now=NOW) == []


# ------------------------------------------------------------------ every rejection reason


def _no_trace(box, path, result, reason):
    assert result.status == fi.REJECTED and reason in result.reasons, result
    assert not box["forecasts"].exists() and not box["bus"].exists()
    assert path.exists() and not box["archive"].exists()


@pytest.mark.parametrize("changes,reason", [
    ({"market_price_seen": True}, "price_seen"),
    ({"sources": [{"url": "https://polymarket.com/event/x"}]}, "price_source_leak"),
    ({"sources": [{"url": "https://www.reuters.com/a"}, {"url": "https://clob.polymarket.com/book?token_id=1"}]},
     "price_source_leak"),
    ({"sources": [{"url": "https://POLYMARKET.com./market/x"}]}, "price_source_leak"),
    ({"as_of": "2026-10-10T20:59:59.000Z"}, "stale_forecast"),  # 6 h 1 s before now
    ({"as_of": "2026-10-11T03:00:01.000Z"}, "stale_forecast"),  # after now
    ({"token_id": "t-no"}, "token_mismatch"),
    ({"outcome_index": 1}, "token_mismatch"),
    ({"outcome": "No"}, "token_mismatch"),
    ({"token_id": "t-other"}, "token_mismatch"),
    ({"condition_id": "0xother"}, "condition_mismatch"),
    ({"p_low": 0.61, "p_high": 0.66}, "interval_too_narrow"),
    ({"outcome_index": _ABSENT}, "missing_outcome_index"),
    ({"outcome_index": None}, "missing_outcome_index"),
    ({"outcome_index": 2}, "invalid_outcome_index"),
    ({"outcome_index": True}, "invalid_outcome_index"),
    ({"outcome_index": "0"}, "invalid_outcome_index"),
    ({"token_id": _ABSENT}, "missing_token_id"),
    ({"token_id": ""}, "missing_token_id"),
    ({"token_id": None}, "missing_token_id"),
    ({"p_low": _ABSENT}, "missing_interval"),
    ({"p_high": _ABSENT}, "missing_interval"),
    ({"p_low": None, "p_high": None}, "missing_interval"),
    ({"p": 0.7}, "invalid_probability"),
    ({"p_low": 0.0}, "invalid_probability"),
    ({"p_high": 1.0}, "invalid_probability"),
    ({"schema": "poly_forecast_inbox/2"}, "schema_error"),
    ({"extra": 1}, "schema_error"),
    ({"author": ""}, "schema_error"),
    ({"model_version": _ABSENT}, "schema_error"),
    ({"rationale": None}, "schema_error"),
    ({"market_price_seen": _ABSENT}, "schema_error"),
    ({"market_price_seen": "false"}, "schema_error"),
    ({"sources": []}, "schema_error"),
    ({"sources": [{"url": "ftp://x"}]}, "schema_error"),
    ({"sources": [{"url": "https://x.org", "kind": "tweet"}]}, "schema_error"),
    ({"as_of": "2026-10-11T02:30:00+00:00"}, "schema_error"),
    ({"as_of": "2026-10-11"}, "schema_error"),
    ({"p": "0.62"}, "schema_error"),
    ({"p": _ABSENT}, "schema_error"),
    ({"category": 3}, "schema_error"),
    ({"token_id": 5}, "schema_error"),
    ({"market_id": "m-unknown"}, "unknown_market"),
])
def test_each_rejection_reason_and_nothing_is_written(box, changes, reason):
    path = deliver(box, good(**changes))
    _no_trace(box, path, ingest(box, path), reason)


@pytest.mark.parametrize("key", ["p", "p_low", "p_high"])
@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_non_finite_numbers_are_rejected(box, key, value):
    text = json.dumps(good(**{key: 0.123456789})).replace("0.123456789", value)
    path = deliver(box, None, raw=text.encode("utf-8"))
    _no_trace(box, path, ingest(box, path), "non_finite")


@pytest.mark.parametrize("raw", [b"", b"not json", b"[1, 2]", b"\xff\xfe", b"null"])
def test_a_file_that_is_not_a_json_object_is_a_schema_error(box, raw):
    path = deliver(box, None, raw=raw)
    _no_trace(box, path, ingest(box, path), "schema_error")


@pytest.mark.parametrize("market", [
    dict(MARKET, closed=True),
    dict(MARKET, accepting_orders=False),
    dict(MARKET, accepting_orders=None),
    dict(MARKET, uma_resolution_status="proposed"),
    dict(MARKET, uma_resolution_statuses='["resolved"]'),
    dict(MARKET, end_date=(NOW + timedelta(hours=MIN_HOURS_TO_EXPIRY) - timedelta(seconds=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")),
    dict(MARKET, end_date=""),
    dict(MARKET, end_date="soon"),
])
def test_a_market_whose_outcome_may_be_known_is_not_sealed(box, market):
    path = deliver(box, good())
    _no_trace(box, path, ingest(box, path, markets={"m1": market}), "outcome_may_be_known")


def test_the_expiry_boundary_passes():
    market = dict(MARKET, end_date=(NOW + timedelta(hours=MIN_HOURS_TO_EXPIRY)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    assert fi.validate_inbox_row(good(), market_row=market, now=NOW) == []


def test_a_naive_now_is_refused():
    assert "invalid_now" in fi.validate_inbox_row(good(), market_row=MARKET, now=NOW.replace(tzinfo=None))


def test_all_reasons_are_reported_together_in_order():
    reasons = fi.validate_inbox_row(good(market_price_seen=True, token_id=None, p_low=_ABSENT), market_row=MARKET,
                                    now=NOW)
    assert reasons == ["missing_token_id", "missing_interval", "price_seen"]


def test_the_half_width_constant_is_the_rule():
    low = 0.5
    high = low + 2 * FORECAST_MIN_HALF_WIDTH - 1e-6
    assert "interval_too_narrow" in fi.validate_inbox_row(good(p_low=low, p=low + 0.01, p_high=high),
                                                          market_row=MARKET, now=NOW)


# ------------------------------------------------------------------ duplicates and conflicts


def test_the_same_file_again_is_a_duplicate_and_writes_nothing(box):
    first = deliver(box, good())
    data = first.read_bytes()
    assert ingest(box, first).status == fi.SEALED
    before_f, before_b = box["forecasts"].read_bytes(), box["bus"].read_bytes()
    again = deliver(box, None, raw=data)  # the same file delivered a second time
    result = ingest(box, again)
    assert result.status == fi.DUPLICATE and result.forecast_id is None
    assert box["forecasts"].read_bytes() == before_f and box["bus"].read_bytes() == before_b
    assert not again.exists()  # byte-identical to the archived copy


def test_the_same_content_under_another_name_is_a_duplicate_too(box):
    first = deliver(box, good())
    data = first.read_bytes()
    ingest(box, first)
    before = box["forecasts"].read_bytes()
    other = deliver(box, None, name="0002.json", raw=data)
    assert ingest(box, other).status == fi.DUPLICATE and box["forecasts"].read_bytes() == before


def test_a_sealed_file_that_could_not_be_archived_is_archived_on_the_next_pass(box, monkeypatch):
    path = deliver(box, good())
    real = fi._archive

    def broken(*args):
        raise OSError("disk")
    monkeypatch.setattr(fi, "_archive", broken)
    assert ingest(box, path).status == fi.SEALED and path.exists()
    monkeypatch.setattr(fi, "_archive", real)
    before = box["forecasts"].read_bytes()
    assert ingest(box, path).status == fi.DUPLICATE  # never sealed twice
    assert box["forecasts"].read_bytes() == before and not path.exists()
    assert (box["archive"] / "2026-10-11" / "0001.json").exists()


def test_other_bytes_under_an_archived_name_are_a_conflict(box):
    ingest(box, deliver(box, good()))
    before = box["forecasts"].read_bytes()
    path = deliver(box, good(p=0.6))
    result = ingest(box, path)
    assert result.status == fi.REJECTED and result.reasons == ("inbox_conflict",)
    assert box["forecasts"].read_bytes() == before and path.exists()


def test_a_forecast_id_sealed_with_other_content_is_refused_without_a_signal(box, monkeypatch):
    monkeypatch.setattr(forecaster.calibration_ids, "make_forecast_id", lambda **kw: "fc_fixed")
    ingest(box, deliver(box, good()))
    before_f, before_b = box["forecasts"].read_bytes(), box["bus"].read_bytes()
    path = deliver(box, good(p=0.6), name="0002.json")
    result = ingest(box, path)
    assert result.status == fi.REJECTED and result.reasons == ("forecast_log_error",)
    assert box["forecasts"].read_bytes() == before_f and box["bus"].read_bytes() == before_b and path.exists()


def test_a_damaged_forecast_log_refuses_and_sends_nothing(box):
    box["forecasts"].write_bytes(b'{"torn": ')
    path = deliver(box, good())
    result = ingest(box, path)
    assert result.status == fi.REJECTED and result.reasons == ("forecast_log_error",)
    assert box["forecasts"].read_bytes() == b'{"torn": ' and not box["bus"].exists() and path.exists()


def test_an_unreadable_path_is_rejected(box):
    result = ingest(box, box["pending"] / "missing.json")
    assert result.status == fi.REJECTED and result.reasons == ("unreadable_file",)


# ------------------------------------------------------------------ seal before book


def test_the_seal_comes_before_any_book_read(box, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(fi, "utc_now_ms", lambda: "2026-10-11T03:00:00.250Z")

    def read_book(market_row, token_id):
        sealed = read_jsonl(box["forecasts"])
        signals = read_jsonl(box["bus"]) if box["bus"].exists() else []
        calls.append(f"book:{token_id}:sealed={len(sealed)}:signals={len(signals)}")
        return {"bids": [], "asks": []}

    result = ingest(box, deliver(box, good()), read_book=read_book)
    assert calls == ["book:t-yes:sealed=1:signals=1"]
    assert result.status == fi.SEALED and result.reasons == () and result.book_fetched_at == "2026-10-11T03:00:00.250Z"
    assert result.forecast_ts == SEAL_TS


def test_a_book_read_not_after_the_seal_is_flagged(box, monkeypatch):
    monkeypatch.setattr(fi, "utc_now_ms", lambda: "2026-10-11T03:00:00.000Z")
    result = ingest(box, deliver(box, good()), read_book=lambda market_row, token_id: None)
    assert result.reasons == ("forecast_not_sealed_before_book",)


@pytest.mark.parametrize("changes", [{"market_price_seen": True}, {"token_id": None}])
def test_a_rejected_file_never_reaches_the_book(box, changes):
    calls: list = []
    ingest(box, deliver(box, good(**changes)), read_book=lambda *a: calls.append(a))
    assert calls == []


# ------------------------------------------------------------------ emit_probability(require_ids=True)


@pytest.mark.parametrize("missing", [dict(outcome_index=None), dict(token_id=None), dict(token_id="  ")])
def test_require_ids_refuses_before_anything_is_written(box, missing):
    kw = dict(market="m1", model_prob=0.6, outcome_index=0, token_id="t-yes", bus_path=box["bus"],
              forecasts_path=box["forecasts"], require_ids=True)
    kw.update(missing)
    with pytest.raises(ValueError):
        forecaster.emit_probability(**kw)
    assert not box["forecasts"].exists() and not box["bus"].exists()


def test_without_require_ids_the_legacy_call_still_works(box):
    message = forecaster.emit_probability(market="m1", model_prob=0.6, bus_path=box["bus"],
                                          forecasts_path=box["forecasts"])
    assert "outcome_index" not in message["payload"]


# ------------------------------------------------------------------ candidates


def test_candidates_carry_no_price_field(tmp_path):
    path = tmp_path / "m2" / "candidates.jsonl"
    other = dict(MARKET, id="m2", description="desc", resolution_source="https://x.org", bestBid=0.1,
                 lastTradePrice=0.2, outcomePrices="[0.1, 0.9]", volume24hr=5.0)
    assert fi.write_candidates([MARKET, other], path) == 2
    rows = read_jsonl(path)
    assert [row["market_id"] for row in rows] == ["m1", "m2"]
    for row in rows:
        assert set(row) <= set(fi.CANDIDATE_FIELDS)
        assert not set(row) & set(fi.PRICE_FIELDS)
        for word in ("bid", "ask", "spread", "price", "volume", "liquidity"):
            assert word not in json.dumps(sorted(row)).lower(), word
    assert rows[1]["description"] == "desc" and rows[0]["token_ids"] == ["t-yes", "t-no"]
    assert not (path.parent / "candidates.jsonl.tmp").exists()


def test_candidates_whitelist_has_no_price_name():
    for name in fi.CANDIDATE_FIELDS:
        assert not any(word in name.lower() for word in ("bid", "ask", "spread", "price", "volume", "liquidity"))


def test_candidates_replace_atomically(tmp_path):
    path = tmp_path / "candidates.jsonl"
    fi.write_candidates([MARKET], path)
    fi.write_candidates([], path)
    assert path.read_text(encoding="utf-8") == ""


def test_module_has_no_network_or_key_imports():
    import ast
    tree = ast.parse(Path(fi.__file__).read_text(encoding="utf-8"))
    mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    mods |= {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not mods & {"urllib.request", "http.client", "socket", "ssl", "requests", "src.scanner", "src.book",
                       "src.public_get"}, mods
    assert math.isfinite(FORECAST_MAX_ASOF_LAG_S)


def test_the_price_domain_comes_from_the_pinned_book_endpoint():
    assert fi._PRICE_DOMAIN == "polymarket.com"
    assert fi._leaks_prices("https://docs.polymarket.com/x") and not fi._leaks_prices("https://notpolymarket.com/x")
