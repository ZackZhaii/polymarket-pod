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


def _stored(
    condition_id: str = "0xcond01",
    token: str = "tok-1-yes",
    prices: tuple[float, ...] = (1.0, 0.0),
    closed_ts: str = CLOSED_49H,
    snapshot_ts: str = TS,
    uma_checked: bool = False,
) -> dict:
    """One stored data/resolutions.jsonl row, exactly as src/resolutions.py writes it."""
    return {
        "condition_id": condition_id,
        "winning_token_id": token,
        "outcome_prices_final": list(prices),
        "closed_ts": closed_ts,
        "snapshot_ts": snapshot_ts,
        "status": "resolved" if uma_checked else "closed",
        "uma_checked": uma_checked,
    }


def _line(row: dict) -> bytes:
    return (json.dumps(row) + "\n").encode("utf-8")


def _write_history(path: Path, rows: list[dict]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(_line(row) for row in rows))
    return path.read_bytes()


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
    before = path.read_bytes()
    raw = [_closed(i) for i in range(4)]
    getter = _pager({"": (raw[:2], "c1"), "c1": (raw[2:], "c1")})
    code, _, rows = _run(tmp_path, raw, getter=getter, resolutions_path=path)
    assert code == resolutions.EXIT_INCOMPLETE == scanner.EXIT_INCOMPLETE != 0
    assert path.read_bytes() == before
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
    # issue #57: a stored confirmed row is never rewritten and the newest winner is never chosen
    again = _closed(1, outcomePrices='["0", "1"]', clobTokenIds='["tok-1-yes", "tok-1-no"]')
    code, _, rows = _run(tmp_path, [again], resolutions_path=path)
    assert code == 0 and len(rows) == 1
    assert rows[0]["winning_token_id"] == "tok-1-yes"


def test_same_fetch_dedupes_duplicate_condition_ids(tmp_path):
    a = _closed(1)
    b = _closed(1, id="m1-dup")
    code, _path, rows = _run(tmp_path, [a, b])
    assert code == 0 and len(rows) == 1


# ---------------------------------------------------------------- issue #57: pending -> resolved

def test_pending_snapshot_is_confirmed_once_and_the_old_bytes_stay(tmp_path, capsys):
    path = tmp_path / "resolutions.jsonl"
    pending = _stored()
    before = _write_history(path, [pending])
    code, _, rows = _run(tmp_path, [_closed(1)], resolutions_path=path)
    assert code == resolutions.EXIT_OK
    assert len(rows) == 2
    assert rows[0] == pending  # the unconfirmed row is kept byte for byte, never rewritten
    confirmed = rows[1]
    assert set(confirmed) == ROW_KEYS
    assert confirmed["condition_id"] == pending["condition_id"]
    assert confirmed["winning_token_id"] == pending["winning_token_id"]
    assert confirmed["outcome_prices_final"] == pending["outcome_prices_final"]
    assert confirmed["closed_ts"] == pending["closed_ts"]
    assert confirmed["snapshot_ts"] == TS
    assert confirmed["uma_checked"] is True and confirmed["status"] == "resolved"
    after = path.read_bytes()
    assert after[: len(before)] == before and len(after) > len(before)
    out = capsys.readouterr().out
    assert "written=1 upgrades=1" in out


def test_an_empty_history_file_is_an_empty_history(tmp_path):
    path = tmp_path / "resolutions.jsonl"
    path.write_bytes(b"")
    code, _, rows = _run(tmp_path, [_closed(1)], resolutions_path=path)
    assert code == resolutions.EXIT_OK and len(rows) == 1


def test_a_second_confirmation_is_idempotent(tmp_path, capsys):
    path = tmp_path / "resolutions.jsonl"
    _write_history(path, [_stored()])
    assert _run(tmp_path, [_closed(1)], resolutions_path=path)[0] == resolutions.EXIT_OK
    before = path.read_bytes()
    code, _, rows = _run(tmp_path, [_closed(1)], resolutions_path=path, ts="2026-10-09T13:00:00Z")
    assert code == resolutions.EXIT_OK and len(rows) == 2
    assert path.read_bytes() == before
    assert "already_confirmed:1" in capsys.readouterr().out


def test_a_status_regression_after_a_confirmation_appends_nothing(tmp_path, capsys):
    path = tmp_path / "resolutions.jsonl"
    before = _write_history(path, [_stored(uma_checked=True, snapshot_ts="2026-10-08T00:00:00Z")])
    code, _, rows = _run(tmp_path, [_closed(1, umaResolutionStatus="pending")], resolutions_path=path)
    assert code == resolutions.EXIT_OK and len(rows) == 1
    assert path.read_bytes() == before
    assert "already_confirmed:1" in capsys.readouterr().out


def test_one_walk_pending_then_resolved_appends_the_first_row_only(tmp_path, capsys):
    code, _path, rows = _run(tmp_path, [_closed(1, umaResolutionStatus="pending"), _closed(1, id="m1-later")])
    assert code == resolutions.EXIT_OK and len(rows) == 1
    assert rows[0]["uma_checked"] is False and rows[0]["status"] == "closed"
    assert "duplicate:1" in capsys.readouterr().out


def test_one_walk_resolved_twice_appends_one_confirmation(tmp_path):
    path = tmp_path / "resolutions.jsonl"
    _write_history(path, [_stored()])
    raw = [_closed(1), _closed(1, id="m1-later")]
    code, _, rows = _run(tmp_path, raw, resolutions_path=path)
    assert code == resolutions.EXIT_OK and len(rows) == 2
    assert rows[1]["uma_checked"] is True


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"outcomePrices": '["0", "1"]'}, "upgrade_winner_conflict"),
        (
            {
                "outcomes": '["Yes", "No", "Maybe"]',
                "outcomePrices": '["1", "0", "0"]',
                "clobTokenIds": '["tok-1-yes", "tok-1-no", "tok-1-maybe"]',
            },
            "upgrade_prices_conflict",
        ),
        ({"closedTime": CLOSED_48H}, "upgrade_closed_ts_conflict"),
    ],
)
def test_an_upgrade_conflict_is_refused_by_name(tmp_path, capsys, extra, reason):
    path = tmp_path / "resolutions.jsonl"
    before = _write_history(path, [_stored()])
    code, _, rows = _run(tmp_path, [_closed(1, **extra)], resolutions_path=path)
    assert code == resolutions.EXIT_OK and len(rows) == 1  # the newest winner is not chosen
    assert path.read_bytes() == before
    assert f"{reason}:1" in capsys.readouterr().out


def test_an_upgrade_clock_regression_is_refused(tmp_path, capsys):
    path = tmp_path / "resolutions.jsonl"
    before = _write_history(path, [_stored(snapshot_ts=TS)])
    code, _, rows = _run(tmp_path, [_closed(1)], resolutions_path=path, ts="2026-10-09T11:00:00Z")
    assert code == resolutions.EXIT_OK and len(rows) == 1
    assert path.read_bytes() == before
    assert "upgrade_clock_regression:1" in capsys.readouterr().out


def test_incomplete_paging_does_not_append_the_upgrade(tmp_path):
    path = tmp_path / "resolutions.jsonl"
    pending = _stored()
    before = _write_history(path, [pending])
    raw = [_closed(i) for i in range(3)]
    getter = _pager({"": (raw[:2], "c1"), "c1": (raw[2:], "c1")})
    code, _, rows = _run(tmp_path, raw, getter=getter, resolutions_path=path)
    assert code == resolutions.EXIT_INCOMPLETE == 3
    assert path.read_bytes() == before
    assert rows == [pending]


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("torn_last_line", _line(_stored())[:-1]),
        ("blank_line", b"\n"),
        ("bad_json", b'{"condition_id": "0xcond01"\n'),
        ("nan", b'{"condition_id": NaN}\n'),
        ("not_an_object", b"[1, 2]\n"),
        ("missing_key", _line({k: v for k, v in _stored().items() if k != "status"})),
        ("extra_key", _line({**_stored(), "note": "x"})),
        ("wrong_type", _line({**_stored(), "uma_checked": "false"})),
        ("status_disagrees", _line({**_stored(), "status": "resolved"})),
        ("no_unique_winner", _line({**_stored(), "outcome_prices_final": [0.5, 0.5]})),
        ("padded_condition_id", _line({**_stored(), "condition_id": " 0xcond01 "})),
        ("unzoned_closed_ts", _line({**_stored(), "closed_ts": "2026-10-07T11:00:00"})),
    ],
)
def test_an_unusable_history_is_exit_3_and_appends_nothing(tmp_path, capsys, name, payload):
    # issue #57: the lenient read_jsonl reader must never skip a stored row; the run fails closed
    path = tmp_path / "resolutions.jsonl"
    path.write_bytes(payload)
    calls: list[str] = []
    code, _, _rows = _run(
        tmp_path,
        [_closed(1)],
        resolutions_path=path,
        getter=lambda url: calls.append(url) or {"markets": [], "next_cursor": ""},
    )
    assert code == resolutions.EXIT_INCOMPLETE == 3  # exit 3, the Sentinel alert code
    assert calls == []  # nothing is fetched
    assert path.read_bytes() == payload  # and nothing is appended
    assert "history unusable" in capsys.readouterr().err, name


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
