"""Task AN: read-only Gamma closed-market settlement snapshot. Offline fixtures only."""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.resolutions as resolutions
import src.scanner as scanner
from src.podlib import RISK_PATH, read_jsonl
from src.thresholds import RESOLUTION_HOLD_HOURS

NOW = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
TS = "2026-10-09T12:00:00Z"
CLOSED_49H = "2026-10-07T11:00:00Z"
CLOSED_48H = "2026-10-07T12:00:00Z"
CLOSED_47H = "2026-10-07T13:00:00Z"
BANNED = ("/order", "/auth", "api-key", "secret", "signature", "withdraw", "private_key", "commit_if_allowed")
ROW_KEYS = {
    "condition_id",
    "winning_token_id",
    "outcome_prices_final",
    "closed_ts",
    "snapshot_ts",
    "status",
    "uma_checked",
}


def _closed(index: int = 1, **extra: object) -> dict:
    row: dict[str, object] = {
        "id": f"m{index}",
        "conditionId": f"0xcond{index:02d}",
        "closed": True,
        "closedTime": CLOSED_49H,
        "version": "v1",
        "outcomes": '["Yes", "No"]',
        "outcomePrices": '["1", "0"]',
        "clobTokenIds": f'["tok-{index}-yes", "tok-{index}-no"]',
        "umaResolutionStatus": "resolved",
    }
    row.update(extra)
    return row


def _pager(pages: dict[str, tuple[list[dict], str]], seen: list[str] | None = None):
    def getter(url: str) -> dict:
        if seen is not None:
            seen.append(url)
        cursor = ""
        if "after_cursor=" in url:
            cursor = url.split("after_cursor=", 1)[1]
        markets, nxt = pages[cursor]
        return {"markets": markets, "next_cursor": nxt}

    return getter


def _run(tmp_path: Path, markets: list[dict], **kwargs):
    path = kwargs.pop("resolutions_path", tmp_path / "resolutions.jsonl")
    getter = kwargs.pop("getter", _pager({"": (markets, "")}))
    code = resolutions.main(
        getter=getter,
        resolutions_path=path,
        ts=kwargs.pop("ts", TS),
        now=kwargs.pop("now", NOW),
        **kwargs,
    )
    rows = read_jsonl(path) if path.exists() else []
    return code, path, rows


def test_hold_hours_is_the_gathered_constant():
    assert resolutions.RESOLUTION_HOLD_HOURS is RESOLUTION_HOLD_HOURS
    assert resolutions.RESOLUTION_HOLD_HOURS == 48.0


def test_happy_49h_writes_required_fields(tmp_path):
    before = RISK_PATH.read_bytes()
    code, path, rows = _run(tmp_path, [_closed(1)])
    assert code == resolutions.EXIT_OK == 0
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == ROW_KEYS
    assert row["condition_id"] == "0xcond01"
    assert row["winning_token_id"] == "tok-1-yes"
    assert row["outcome_prices_final"] == [1.0, 0.0]
    assert row["closed_ts"] == CLOSED_49H
    assert row["snapshot_ts"] == TS
    assert row["status"] == "resolved"
    assert row["uma_checked"] is True
    assert RISK_PATH.read_bytes() == before
    assert path.exists()


def test_exactly_48h_writes(tmp_path):
    code, _path, rows = _run(tmp_path, [_closed(1, closedTime=CLOSED_48H)])
    assert code == 0 and len(rows) == 1
    assert rows[0]["closed_ts"] == CLOSED_48H


def test_47h_skips_hold_pending(tmp_path):
    code, path, rows = _run(tmp_path, [_closed(1, closedTime=CLOSED_47H)])
    assert code == 0 and rows == []
    assert not path.exists()


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"closed": False}, "not_closed"),
        ({"closed": "true"}, "not_closed"),
        ({"closedTime": None}, "missing_closed_ts"),
        ({"closedTime": ""}, "missing_closed_ts"),
        ({"closedTime": "2026-10-07"}, "invalid_closed_ts"),
        ({"closedTime": "2026-10-07T11:00:00"}, "invalid_closed_ts"),
        ({"closedTime": "not-a-date"}, "invalid_closed_ts"),
        ({"closedTime": "2026-10-10T00:00:00Z"}, "future_closed_ts"),
        ({"conditionId": ""}, "missing_condition_id"),
        ({"conditionId": 123}, "missing_condition_id"),
        ({"umaResolutionStatus": None}, "missing_uma_status"),
        ({"umaResolutionStatus": ""}, "missing_uma_status"),
        ({"umaResolutionStatus": "  "}, "missing_uma_status"),
        ({"outcomePrices": None}, "missing_outcome_prices"),
        ({"outcomePrices": ""}, "missing_outcome_prices"),
        ({"outcomePrices": "[]"}, "missing_outcome_prices"),
        ({"outcomePrices": '["1", "nope"]'}, "missing_outcome_prices"),
        ({"outcomePrices": '["NaN", "0"]'}, "non_finite_outcome_prices"),
        ({"outcomePrices": ["inf", 0]}, "non_finite_outcome_prices"),
        ({"outcomePrices": '["0.5", "0.5"]'}, "no_unique_winner"),
        ({"outcomePrices": '["1", "1"]'}, "no_unique_winner"),
        ({"outcomePrices": '["0.9", "0.1"]'}, "no_unique_winner"),
        ({"version": None}, "missing_version"),
        ({"version": "v3"}, "unsupported_version"),
    ],
)
def test_missing_or_invalid_fields_skip_the_row(tmp_path, extra, reason):
    market = _closed(1)
    for key, value in extra.items():
        if value is None:
            market.pop(key, None)
        else:
            market[key] = value
    row, skip = resolutions.classify_market(market, NOW, TS)
    assert row is None and skip == reason
    code, path, rows = _run(tmp_path, [market])
    assert code == 0 and rows == [] and not path.exists()


def test_uma_pending_writes_uma_checked_false(tmp_path):
    code, _path, rows = _run(tmp_path, [_closed(1, umaResolutionStatus="pending")])
    assert code == 0 and len(rows) == 1
    assert rows[0]["uma_checked"] is False
    assert rows[0]["status"] == "closed"
    assert rows[0]["winning_token_id"] == "tok-1-yes"


def test_v2_position_ids_pick_the_winner(tmp_path):
    market = _closed(
        2,
        version="v2",
        positionIds=["111", "222"],
        clobTokenIds='["ignore-yes", "ignore-no"]',
        outcomePrices=["0", "1"],
    )
    code, _path, rows = _run(tmp_path, [market])
    assert code == 0 and rows[0]["winning_token_id"] == "222"
    assert rows[0]["outcome_prices_final"] == [0.0, 1.0]


def test_token_count_mismatch_skips(tmp_path):
    market = _closed(1, clobTokenIds='["only-one"]')
    row, skip = resolutions.classify_market(market, NOW, TS)
    assert row is None and skip == "token_count_mismatch"
    code, path, rows = _run(tmp_path, [market])
    assert code == 0 and rows == [] and not path.exists()


def test_incomplete_fetch_writes_nothing_and_leaves_existing(tmp_path):
    path = tmp_path / "resolutions.jsonl"
    existing = {
        "condition_id": "0xkeep",
        "winning_token_id": "t",
        "outcome_prices_final": [1.0, 0.0],
        "closed_ts": CLOSED_49H,
        "snapshot_ts": TS,
        "status": "resolved",
        "uma_checked": True,
    }
    path.write_text(json.dumps(existing) + "\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    raw = [_closed(i) for i in range(4)]
    getter = _pager({"": (raw[:2], "c1"), "c1": (raw[2:], "c1")})
    code, _, rows = _run(tmp_path, raw, getter=getter, resolutions_path=path)
    assert code == resolutions.EXIT_INCOMPLETE == scanner.EXIT_INCOMPLETE != 0
    assert path.read_text(encoding="utf-8") == before
    assert [row["condition_id"] for row in rows] == ["0xkeep"]


def test_pin_drift_does_not_fetch(tmp_path, monkeypatch):
    import src.podlib as podlib

    cfg = dict(podlib.load_risk())
    cfg["live_enabled"] = True
    monkeypatch.setattr(resolutions, "load_risk", lambda: cfg)
    calls: list[str] = []
    path = tmp_path / "resolutions.jsonl"
    code = resolutions.main(
        getter=lambda url: calls.append(url) or {"markets": [_closed(1)], "next_cursor": ""},
        resolutions_path=path,
        ts=TS,
        now=NOW,
    )
    assert code == resolutions.EXIT_PIN_DRIFT == 6
    assert calls == [] and not path.exists()


@pytest.mark.parametrize("ts", ["not-a-time", "2026-10-09T10:00:00"])
def test_bad_snapshot_clock_has_its_own_code_and_does_not_fetch(tmp_path, ts):
    # Task AV: a snapshot clock that is not a zoned ISO timestamp is exit 5, not the pin-drift code
    calls: list[str] = []
    path = tmp_path / "resolutions.jsonl"
    code = resolutions.main(
        getter=lambda url: calls.append(url) or {"markets": [_closed(1)], "next_cursor": ""},
        resolutions_path=path,
        ts=ts,
    )
    assert code == resolutions.EXIT_BAD_CLOCK == 5
    assert code != resolutions.EXIT_PIN_DRIFT
    assert calls == [] and not path.exists()


def test_exit_4_is_retired():
    codes = {name: value for name, value in vars(resolutions).items() if name.startswith("EXIT_")}
    assert 4 not in codes.values()
    assert not hasattr(resolutions, "EXIT_PIN")
    assert codes["EXIT_PIN_DRIFT"] == 6 and codes["EXIT_BAD_CLOCK"] == 5 and codes["EXIT_INCOMPLETE"] == 3


def test_closed_true_url_and_not_closed_false(tmp_path):
    seen: list[str] = []
    _run(tmp_path, [_closed(1)], getter=_pager({"": ([_closed(1)], "")}, seen))
    assert seen
    url = seen[0]
    assert "closed=true" in url
    assert "closed=false" not in url
    assert "markets/keyset" in url
    assert "order=closedTime" in url
    assert "liquidity_num_min" not in url
    assert scanner.GAMMA_KEYSET != resolutions.GAMMA_CLOSED_KEYSET
    assert "closed=false" in scanner.GAMMA_KEYSET


def test_scanner_default_fetch_still_asks_open_markets():
    seen: list[str] = []

    def getter(url: str) -> dict:
        seen.append(url)
        return {"markets": [], "next_cursor": ""}

    scanner.fetch_all_markets(getter, page_limit=5)
    assert seen and "closed=false" in seen[0] and "closed=true" not in seen[0]


def test_complete_empty_universe_is_ok_and_silent(tmp_path):
    code, path, rows = _run(tmp_path, [])
    assert code == 0 and rows == [] and not path.exists()


def test_dedup_by_condition_id(tmp_path):
    path = tmp_path / "resolutions.jsonl"
    first = _closed(1)
    code, _, rows = _run(tmp_path, [first], resolutions_path=path)
    assert code == 0 and len(rows) == 1
    again = _closed(1, outcomePrices='["0", "1"]', clobTokenIds='["tok-1-yes", "tok-1-no"]')
    code, _, rows = _run(tmp_path, [again], resolutions_path=path)
    assert code == 0 and len(rows) == 1
    assert rows[0]["winning_token_id"] == "tok-1-yes"


def test_same_fetch_dedupes_duplicate_condition_ids(tmp_path):
    a = _closed(1)
    b = _closed(1, id="m1-dup")
    code, _path, rows = _run(tmp_path, [a, b])
    assert code == 0 and len(rows) == 1


def test_source_has_no_order_fill_or_key_paths():
    source = inspect.getsource(resolutions).lower()
    assert not any(token in source for token in BANNED)
    assert "bus(" not in inspect.getsource(resolutions)
    assert '"fill"' not in inspect.getsource(resolutions)


def test_does_not_redefine_hold_hours():
    tree_src = Path(resolutions.__file__).read_text(encoding="utf-8")
    assert "RESOLUTION_HOLD_HOURS =" not in tree_src
    assert "from src.thresholds import RESOLUTION_HOLD_HOURS" in tree_src


def test_main_block_writes_a_start_finish_heartbeat():
    # Task AV: the Sentinel can only act on exit 5 / 6 if resolutions beats (like scanner, endgame_scan)
    text = Path(resolutions.__file__).read_text(encoding="utf-8")
    assert 'run_main("resolutions", main)' in text.split('if __name__ == "__main__":', 1)[1]
