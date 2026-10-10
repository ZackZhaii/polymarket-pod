"""Task AG (M1): WebSocket market-channel book recorder. Offline only.

A fake transport replays scripted frames, disconnects, gaps and bad values; a fake ``websockets``
module stands in for the optional package. Nothing here opens a socket, and the suite runs
without ``websockets`` installed.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import subprocess
import sys
import types
from pathlib import Path

import pytest

from src import book as book_module
from src import paper_cycle, sentinel
from scripts import guard
from src import ws_recorder as w
from src.book_staleness import INVALID_BOOK_TS, book_age_problem, parse_book_ts
from src.podlib import read_jsonl
from src.thresholds import (
    MAX_BOOK_AGE_S,
    RECORDER_RECONNECT_ALERT_COUNT,
    WS_BACKOFF_BASE_S,
    WS_BACKOFF_CAP_S,
    WS_GROUP_FLUSH_S,
    WS_PING_INTERVAL_S,
    WS_PONG_TIMEOUT_S,
    WS_RESYNC_TIMEOUT_S,
    WS_ROW_REFRESH_S,
)

ROOT = Path(__file__).resolve().parents[1]
A, B = "111", "222"
M = "0xcond"
T0 = 1791496217153  # 2026-10-08T21:50:17.153Z
TS = "2030-01-01T00:00:00Z"  # the recorder's own clock: never a book_ts


def _book(asset=A, bids=(("0.48", "30"),), asks=(("0.52", "25"),), ts=T0, market=M, **extra):
    message = {"event_type": "book", "asset_id": asset, "market": market,
               "bids": [{"price": p, "size": s} for p, s in bids],
               "asks": [{"price": p, "size": s} for p, s in asks],
               "timestamp": str(ts), "hash": "0xabc"}
    message.update(extra)
    return message


def _ch(asset=A, price="0.49", size="10", side="BUY", **extra):
    item = {"asset_id": asset, "price": price, "size": size, "side": side, "hash": "order"}
    item.update(extra)
    return item


def _pc(*changes, ts=T0 + 1000, market=M):
    return {"event_type": "price_change", "market": market, "price_changes": list(changes), "timestamp": str(ts)}


def _core(*assets, meta=None):
    return w.BookCore(assets or (A,), meta)


def _feed(core, *messages, mono=0.0):
    out = w.Outcome()
    for message in messages:
        if isinstance(message, str):
            part = core.handle_text(message, TS, mono)
        else:
            part = core.handle(message, TS, mono)
        out.rows += part.rows
        out.resyncs += part.resyncs
        out.counts.update(part.counts)
    return out


def _live(core, asset=A):
    return core.books[asset].state == w.LIVE


# --- core: snapshots and row format -------------------------------------------------------------


def test_snapshot_row_has_every_book_py_key_and_the_exchange_time():
    legacy = book_module.snapshot({"id": "m1", "question": "q", "liquidity_usd": 5.0}, "Yes", A, TS,
                                  getter=lambda url: {"bids": [], "asks": [], "timestamp": "1"})
    core = _core(meta={A: {"market_id": "m1", "question": "q", "outcome": "Yes", "liquidity_usd": 5.0}})
    row = _feed(core, _book()).rows[0]
    assert set(legacy) <= set(row)
    assert row["book_ts"] == book_module.book_timestamp(str(T0))[0] == "2026-10-08T21:50:17.153Z"
    assert row["ts"] == TS and row["book_ts"] != row["ts"]  # local time is only ever ts
    assert (row["best_bid"], row["best_bid_size"], row["best_ask"], row["best_ask_size"], row["spread"]) == (
        0.48, 30.0, 0.52, 25.0, 0.04)
    assert (row["market_id"], row["outcome"], row["question"], row["liquidity_usd"]) == ("m1", "Yes", "q", 5.0)
    assert row["condition_id"] == M and row["source"] == "clob_ws_market" and row["book_hash"] == "0xabc"
    assert row["book_ts_problem"] is None and row["ws_state"] == "live"


def test_core_never_reads_the_local_clock(monkeypatch):
    monkeypatch.setattr(w, "utc_now", lambda: pytest.fail("core read the clock"))
    monkeypatch.setattr(w.time, "monotonic", lambda: pytest.fail("core read the clock"))
    rows = _feed(_core(), _book(), _pc(_ch(price="0.50", size="5"))).rows
    assert [row["book_ts"] for row in rows] == ["2026-10-08T21:50:17.153Z", "2026-10-08T21:50:18.153Z"]


def test_initial_dump_list_and_live_shape_tick_and_last_trade_keys():
    """The live smoke run showed the dump as a JSON list whose books carry tick_size and last_trade_price."""
    core = _core(A, B)
    out = _feed(core, json.dumps([_book(A, tick_size="0.01", last_trade_price="0.5"), _book(B, tick_size="0.01")]))
    assert core.all_live() and len(out.rows) == 2 and out.rows[0]["tick_size"] == 0.01
    assert out.resyncs == []


def test_bids_and_asks_in_any_order_give_the_right_top():
    rows = _feed(_core(), _book(bids=(("0.001", "5"), ("0.40", "1"), ("0.30", "2")),
                                asks=(("0.999", "5"), ("0.45", "3"), ("0.60", "1")))).rows
    assert (rows[0]["best_bid"], rows[0]["best_ask"]) == (0.40, 0.45)


# --- core: deltas -------------------------------------------------------------------------------


def test_price_change_adds_updates_and_removes_levels():
    core = _core()
    out = _feed(core, _book(), _pc(_ch(price="0.49", size="10", best_bid="0.49", best_ask="0.52")))
    assert out.rows[-1]["best_bid"] == 0.49 and out.rows[-1]["best_bid_size"] == 10.0
    out = _feed(core, _pc(_ch(price="0.49", size="0", best_bid="0.48", best_ask="0.52"), ts=T0 + 2000))
    assert out.rows[-1]["best_bid"] == 0.48 and out.resyncs == []
    out = _feed(core, _pc(_ch(price="0.51", size="7", side="SELL", best_bid="0.48", best_ask="0.51"), ts=T0 + 3000))
    assert out.rows[-1]["best_ask"] == 0.51 and out.rows[-1]["book_ts"] == "2026-10-08T21:50:20.153Z"


def test_a_delta_below_the_top_writes_no_row_until_the_refresh():
    core = _core()
    _feed(core, _book(bids=(("0.48", "30"), ("0.40", "5"))))
    quiet = _feed(core, _pc(_ch(price="0.40", size="6"), ts=T0 + 1000))
    assert quiet.rows == [] and quiet.counts["price_change_applied"] == 1
    late = T0 + int(WS_ROW_REFRESH_S * 1000)
    refreshed = _feed(core, _pc(_ch(price="0.40", size="7"), ts=late))
    assert len(refreshed.rows) == 1 and parse_book_ts(refreshed.rows[0]["book_ts"]).timestamp() * 1000 == late


def test_one_price_change_can_touch_both_tokens():
    core = _core(A, B)
    _feed(core, _book(A), _book(B, bids=(("0.47", "1"),), asks=(("0.53", "1"),)))
    out = _feed(core, _pc(_ch(A, price="0.50", size="1"), _ch(B, price="0.51", size="2", side="SELL")))
    assert {row["token_id"] for row in out.rows} == {A, B} and out.resyncs == []


def test_deltas_before_a_snapshot_are_ignored():
    core = _core()
    out = _feed(core, _pc(_ch()), {"event_type": "tick_size_change", "asset_id": A, "market": M,
                                   "old_tick_size": "0.01", "new_tick_size": "0.001", "timestamp": str(T0)})
    assert out.rows == [] and out.resyncs == [] and out.counts["ignored_awaiting_snapshot"] == 2
    assert not _live(core)


def test_unsubscribed_assets_and_unknown_events_for_other_assets_are_ignored():
    core = _core()
    _feed(core, _book())
    out = _feed(core, _book("999"), {"event_type": "best_bid_ask", "asset_id": "999"},
                {"event_type": "best_bid_ask", "market": "other-market"}, "PONG")
    assert out.rows == [] and out.resyncs == [] and _live(core)
    assert out.counts["unsubscribed_asset"] == 1 and out.counts["ignored_event:best_bid_ask"] == 2
    assert out.counts["pong"] == 1


@pytest.mark.parametrize("frame", [
    {"event_type": "best_bid_ask", "asset_id": A},
    {"event_type": "new_market", "market": M},
    {"event_type": "market_resolved", "assets_ids": ["999", A]},
    {"event_type": "mystery", "price_changes": [{"asset_id": A}]},
    {"event_type": "mystery"},  # names nothing: every asset
])
def test_task_aw_an_unknown_event_for_a_subscribed_asset_resyncs_it(frame):
    core = _core()
    _feed(core, _book())
    out = _feed(core, frame)
    _assert_resync(core, out, "unknown_event")
    assert out.counts[f"unknown_event:{frame['event_type']}"] == 1


def test_task_aw_an_unknown_event_names_only_its_assets():
    core = _core(A, B)
    _feed(core, _book(A), _book(B))
    out = _feed(core, {"event_type": "best_bid_ask", "asset_id": B})
    assert out.resyncs == [(B, "unknown_event")] and core.books[A].state == w.LIVE


# --- core: invalid values fail closed -----------------------------------------------------------


BAD_PRICES = ["nan", "NaN", "inf", "-inf", "Infinity", "1e999", "0", "1", "1.5", "-0.1", "abc", "", None, True,
              float("nan"), [], {}]
BAD_SIZES = ["nan", "inf", "-1", "-0.5", "1e999", "abc", "", None, False, float("inf")]


def _assert_resync(core, out, reason, asset=A):
    assert (asset, reason) in out.resyncs
    assert not _live(core, asset)
    marker = [row for row in out.rows if row.get("ws_state") == "resync" and row["token_id"] == asset]
    assert len(marker) == 1
    row = marker[0]
    assert row["book_ts"] is None and row["book_ts_problem"] == INVALID_BOOK_TS and row["resync_reason"] == reason
    assert row["best_bid"] is None and row["best_ask"] is None and row["spread"] is None


@pytest.mark.parametrize("price", BAD_PRICES, ids=repr)
def test_bad_snapshot_price_rejects_and_resyncs(price):
    core = _core()
    _feed(core, _book())
    out = _feed(core, _book(bids=((price, "1"),), ts=T0 + 5))
    _assert_resync(core, out, "invalid_price")


@pytest.mark.parametrize("size", BAD_SIZES + ["0"], ids=repr)
def test_bad_snapshot_size_rejects_and_resyncs(size):
    core = _core()
    _feed(core, _book())
    out = _feed(core, _book(asks=(("0.6", size),), ts=T0 + 5))
    _assert_resync(core, out, "invalid_size")


def test_a_rejected_first_snapshot_writes_nothing_and_stays_waiting():
    core = _core()
    out = _feed(core, _book(bids=(("nan", "1"),)))
    assert out.rows == [] and out.resyncs == [(A, "invalid_price")] and not _live(core)


@pytest.mark.parametrize("price", BAD_PRICES, ids=repr)
def test_bad_delta_price_rejects_the_message_and_resyncs(price):
    core = _core()
    _feed(core, _book())
    out = _feed(core, _pc(_ch(price="0.49", size="5"), _ch(price=price)))
    _assert_resync(core, out, "invalid_price")
    assert core.books[A].bids == {}  # nothing half-applied: the book was discarded


@pytest.mark.parametrize("size", BAD_SIZES, ids=repr)
def test_bad_delta_size_rejects_and_resyncs(size):
    core = _core()
    _feed(core, _book())
    _assert_resync(core, _feed(core, _pc(_ch(size=size))), "invalid_size")


@pytest.mark.parametrize("side", ["buy", "BID", "", None, 1])
def test_bad_side_resyncs(side):
    core = _core()
    _feed(core, _book())
    _assert_resync(core, _feed(core, _pc(_ch(side=side))), "invalid_side")


@pytest.mark.parametrize("top", ["nan", "inf", "-0.1", "1.5", "x", True])
def test_bad_reported_top_resyncs(top):
    core = _core()
    _feed(core, _book())
    _assert_resync(core, _feed(core, _pc(_ch(best_bid=top))), "invalid_top_of_book")


@pytest.mark.parametrize("stamp", ["abc", "-1", "1.5", 1.5, True, None, "", "١٢٣", "1e3", "99999999999999999999999"])
def test_bad_timestamp_resyncs(stamp):
    core = _core()
    _feed(core, _book())
    message = _pc(_ch())
    message["timestamp"] = stamp
    _assert_resync(core, _feed(core, message), "invalid_timestamp")


def test_after_a_resync_deltas_wait_for_a_fresh_snapshot():
    core = _core()
    _feed(core, _book(), _pc(_ch(size="nan")))
    out = _feed(core, _pc(_ch(price="0.50", size="3"), ts=T0 + 2000))
    assert out.rows == [] and out.counts["ignored_awaiting_snapshot"] == 1
    out = _feed(core, _book(ts=T0 + 3000))
    assert _live(core) and out.rows[0]["ws_state"] == "live" and out.rows[0]["best_bid"] == 0.48


# --- core: gaps and inconsistencies -------------------------------------------------------------


def test_timestamp_regression_resyncs_but_equal_time_is_fine():
    core = _core()
    _feed(core, _book(ts=T0 + 10))
    assert _feed(core, _pc(_ch(), ts=T0 + 10)).resyncs == []
    _assert_resync(core, _feed(core, _pc(_ch(price="0.47"), ts=T0 + 9)), "timestamp_regression")


def test_an_older_snapshot_while_live_is_a_regression():
    core = _core()
    _feed(core, _book(ts=T0 + 10))
    _assert_resync(core, _feed(core, _book(ts=T0)), "timestamp_regression")


def test_removing_a_level_we_never_saw_is_a_gap():
    core = _core()
    _feed(core, _book())
    _assert_resync(core, _feed(core, _pc(_ch(price="0.45", size="0"))), "unknown_level_removed")


@pytest.mark.parametrize("ask", ["0.48", "0.47"])
def test_crossed_or_locked_snapshot_is_rejected(ask):
    core = _core()
    _feed(core, _book())
    _assert_resync(core, _feed(core, _book(asks=((ask, "1"),), ts=T0 + 1)), "crossed_book")


def _merge(*outs):
    total = w.Outcome()
    for part in outs:
        total.rows += part.rows
        total.resyncs += part.resyncs
        total.counts.update(part.counts)
    return total


def test_a_delta_that_crosses_the_book_resyncs():
    """Issue #182: the crossed state is held (never written) for WS_GROUP_FLUSH_S; still crossed then: resync."""
    core = _core()
    _feed(core, _book())
    held = _feed(core, _pc(_ch(price="0.53", size="1")))
    assert held.rows == [] and held.resyncs == [] and held.counts["group_held"] == 1
    assert core.flush_groups(TS, WS_GROUP_FLUSH_S - 0.001).resyncs == []
    _assert_resync(core, _merge(held, core.flush_groups(TS, WS_GROUP_FLUSH_S)), "crossed_book")


def test_reported_top_of_book_must_match_the_local_book():
    core = _core()
    _feed(core, _book())
    assert _feed(core, _pc(_ch(price="0.40", size="1", best_bid="0.48", best_ask="0.52"))).resyncs == []
    held = _feed(core, _pc(_ch(price="0.41", size="1", best_bid="0.49", best_ask="0.52"), ts=T0 + 2000))
    assert held.resyncs == [] and held.rows == []
    _assert_resync(core, _merge(held, core.flush_groups(TS, WS_GROUP_FLUSH_S)), "top_of_book_mismatch")


def test_reported_top_is_not_checked_against_an_empty_side():
    core = _core()
    _feed(core, _book(asks=()))
    out = _feed(core, _pc(_ch(price="0.40", size="1", best_bid="0.48", best_ask="1")))
    assert out.resyncs == [] and _live(core)


def test_market_mismatch_resyncs():
    core = _core()
    _feed(core, _book())
    _assert_resync(core, _feed(core, _pc(_ch(), market="0xother")), "market_mismatch")


def test_duplicate_snapshot_level_resyncs():
    core = _core()
    _feed(core, _book())
    _assert_resync(core, _feed(core, _book(bids=(("0.4", "1"), ("0.40", "2")), ts=T0 + 1)), "duplicate_level")


def _tick(old="0.01", new="0.001", ts=T0 + 1000, market=M, asset=A):
    return {"event_type": "tick_size_change", "asset_id": asset, "market": market,
            "old_tick_size": old, "new_tick_size": new, "timestamp": str(ts)}


def test_task_aw_a_valid_tick_size_change_resnapshots_the_asset():
    core = _core()
    _feed(core, _book(tick_size="0.01"))
    out = _feed(core, _tick())
    _assert_resync(core, out, "tick_size_change")
    assert out.counts["tick_size_resnapshot"] == 1
    assert _feed(core, _pc(_ch(price="0.401", ts=T0 + 1100))).rows == []  # no prices until a fresh snapshot
    out = _feed(core, _book(tick_size="0.001", ts=T0 + 1200))
    assert out.rows[-1]["tick_size"] == 0.001 and _live(core)


def test_tick_size_change_is_checked_against_the_tick_held():
    core = _core()
    _feed(core, _book(tick_size="0.01"))
    _assert_resync(core, _feed(core, _tick(old="0.001", new="0.0001")), "tick_size_mismatch")


@pytest.mark.parametrize("bad", ["nan", "0", "1", "-0.01", "x", None])
def test_bad_tick_size_resyncs(bad):
    core = _core()
    _feed(core, _book())
    _assert_resync(core, _feed(core, _tick(new=bad)), "invalid_tick_size")


def test_last_trade_price_is_validated_but_changes_no_book():
    core = _core()
    _feed(core, _book())
    good = {"event_type": "last_trade_price", "asset_id": A, "market": M, "price": "0.5", "size": "3",
            "side": "BUY", "timestamp": str(T0 + 5), "fee_rate_bps": "0"}
    assert _feed(core, good).rows == [] and _live(core)
    _assert_resync(core, _feed(core, dict(good, size="nan")), "invalid_size")


def test_a_frame_that_is_not_json_resyncs_every_asset():
    core = _core(A, B)
    _feed(core, _book(A), _book(B))
    out = _feed(core, "{not json")
    assert {asset for asset, _ in out.resyncs} == {A, B} and not core.all_live()
    assert len([row for row in out.rows if row["ws_state"] == "resync"]) == 2


@pytest.mark.parametrize("message", [5, "text", None, {"asset_id": A}, {"event_type": None, "market": M}])
def test_malformed_messages_resync_what_they_could_belong_to(message):
    core = _core(A, B)
    _feed(core, _book(A), _book(B, market="0xother"))
    out = core.handle(message, TS, 0.0)
    hit = {asset for asset, _ in out.resyncs}
    assert hit and all(reason == "malformed_message" for _, reason in out.resyncs)
    if isinstance(message, dict) and message.get("market") == M:
        assert hit == {A}


def test_a_waiting_asset_keeps_its_wait_start():
    core = _core()
    core.start_waiting(5.0)
    _feed(core, "{bad", mono=50.0)
    assert core.oldest_awaiting_since() == 5.0


# --- runner with a fake transport ---------------------------------------------------------------


class Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


class FakeConnection:
    """Plays a script; answers each PING with a PONG unless ``pong`` is False (Task AW)."""

    def __init__(self, script, clock, sent, pong=True):
        self.script = list(script)
        self.clock = clock
        self.sent = sent
        self.closed = False
        self.pong = pong
        self.pending: list[str] = []

    def send(self, text):
        if self.closed:
            raise w.TransportClosed("closed")
        self.sent.append(text)
        if text == "PING" and self.pong:
            self.pending.append("PONG")

    def recv(self, timeout):
        assert timeout >= 0
        if self.pending:
            self.clock.t += 0.01
            return self.pending.pop(0)
        if not self.script:
            self.clock.t += timeout
            return None
        step = self.script.pop(0)
        if step == "CLOSE":
            self.closed = True
            raise w.TransportClosed("server closed")
        if step == "IDLE":
            self.clock.t += timeout
            return None
        self.clock.t += 0.01
        return step if isinstance(step, str) else json.dumps(step)

    def close(self):
        self.closed = True


class FakeTransport:
    def __init__(self, sessions, clock, pong=True):
        self.sessions = list(sessions)
        self.clock = clock
        self.pong = pong
        self.sent: list[str] = []
        self.urls: list[str] = []

    def connect(self, url, timeout):
        self.urls.append(url)
        assert 0 < timeout <= WS_RESYNC_TIMEOUT_S
        if not self.sessions:
            raise OSError("no route")
        session = self.sessions.pop(0)
        if isinstance(session, BaseException):
            raise session
        return FakeConnection(session, self.clock, self.sent, self.pong)


class ListSink:
    def __init__(self):
        self.rows: list[dict] = []

    def write(self, row):
        json.dumps(row, allow_nan=False)
        self.rows.append(row)


def _run(sessions, *, assets=(A,), runtime=100.0, meta=None, pong=True):
    clock = Clock()
    transport = FakeTransport(sessions, clock, pong)
    sink = ListSink()
    recorder = w.Recorder(list(assets), transport=transport, sink=sink, max_runtime_s=runtime, meta=meta,
                          monotonic=clock.monotonic, sleep=clock.sleep, now=lambda: TS)
    report = recorder.run()
    return report, transport, sink, clock


def test_subscribe_first_then_pings_on_the_interval():
    report, transport, sink, clock = _run([[_book()]], runtime=WS_PING_INTERVAL_S * 3.5)
    assert json.loads(transport.sent[0]) == {"assets_ids": [A], "type": "market", "initial_dump": True}
    assert transport.sent[1:] == ["PING"] * 3 and report.pings == 3
    assert report.connects == 1 and clock.t == pytest.approx(WS_PING_INTERVAL_S * 3.5)
    assert len(sink.rows) == 1


def test_disconnect_invalidates_live_books_and_reconnects_with_backoff():
    report, transport, sink, _ = _run([[_book(), "CLOSE"], [_book(ts=T0 + 9000)]], runtime=50)
    assert report.connects == 2 and report.disconnects == {"closed": 1}
    states = [row["ws_state"] for row in sink.rows]
    assert states == ["live", "resync", "live"]
    assert sink.rows[1]["resync_reason"] == "disconnect_closed"
    assert report.backoffs[0] == WS_BACKOFF_BASE_S  # healthy session: attempt reset, first delay


def test_backoff_doubles_and_is_capped():
    failures = [OSError("down")] * 9
    report, _, _, clock = _run(failures, runtime=10_000)
    expected = [min(WS_BACKOFF_CAP_S, WS_BACKOFF_BASE_S * 2 ** n) for n in range(9)]
    assert report.backoffs[:9] == expected and max(report.backoffs) == WS_BACKOFF_CAP_S
    assert report.connect_failures >= 9 and clock.sleeps[:9] == expected


def test_backoff_resets_after_a_healthy_session():
    sessions = [OSError("x"), OSError("x"), [_book(), "CLOSE"], OSError("x")]
    report, *_ = _run(sessions, runtime=30)
    assert report.backoffs[:4] == [1.0, 2.0, 1.0, 2.0]


def test_backoff_delay_values():
    assert [w.backoff_delay(n) for n in (0, 1, 2, 5, 6, 7, 64, 10_000)] == [1, 2, 4, 32, 60, 60, 60, 60]


def test_no_snapshot_within_the_resync_timeout_reconnects():
    report, transport, sink, _ = _run([["IDLE"] * 10, [_book()]], runtime=WS_RESYNC_TIMEOUT_S + 20)
    assert report.disconnects == {"resync_timeout": 1} and report.connects == 2
    assert [row["ws_state"] for row in sink.rows] == ["live"]  # never live before: no marker row


def test_a_gap_asks_for_a_fresh_snapshot_for_that_asset_only():
    script = [_book(A), _book(B), _pc(_ch(A, price="0.45", size="0")), _book(A, ts=T0 + 5000)]
    report, transport, sink, _ = _run([script], assets=(A, B), runtime=20)
    frames = [json.loads(f) for f in transport.sent if f != "PING"]
    assert frames[1:] == [{"operation": "unsubscribe", "assets_ids": [A]},
                          {"operation": "subscribe", "assets_ids": [A]}]
    assert [(row["token_id"], row["ws_state"]) for row in sink.rows] == [
        (A, "live"), (B, "live"), (A, "resync"), (A, "live")]


def test_a_gap_without_a_fresh_snapshot_ends_in_a_reconnect():
    script = [_book(), _pc(_ch(size="nan"))] + ["IDLE"] * 20
    report, *_ = _run([script, [_book(ts=T0 + 60_000)]], runtime=WS_RESYNC_TIMEOUT_S * 2)
    assert report.disconnects == {"resync_timeout": 1} and report.connects == 2


def test_runtime_is_a_hard_limit_even_while_backing_off():
    report, _, _, clock = _run([OSError("down")] * 50, runtime=10)
    assert clock.t == pytest.approx(10) and sum(report.backoffs) == pytest.approx(10)


def test_a_write_failure_stops_the_recorder_instead_of_reconnecting():
    class Broken(ListSink):
        def write(self, row):
            raise OSError("disk full")

    clock = Clock()
    transport = FakeTransport([[_book()], [_book()]], clock)
    recorder = w.Recorder([A], transport=transport, sink=Broken(), max_runtime_s=50,
                          monotonic=clock.monotonic, sleep=clock.sleep, now=lambda: TS)
    with pytest.raises(OSError):
        recorder.run()
    assert len(transport.urls) == 1


@pytest.mark.parametrize("runtime", [0, -1, float("nan"), float("inf"), "x", None, True])
def test_bad_runtime_is_refused(runtime):
    with pytest.raises(ValueError):
        w.Recorder([A], transport=FakeTransport([], Clock()), sink=ListSink(), max_runtime_s=runtime)


@pytest.mark.parametrize("assets", [[], [""], [" 1"], ["1 2"], ["a\tb"], [5]])
def test_bad_assets_are_refused(assets):
    with pytest.raises(ValueError):
        w.validate_assets(assets)


# --- output file and consumers -------------------------------------------------------------------


def test_appender_seals_a_torn_line_and_appends_whole_lines(tmp_path):
    path = tmp_path / "books.jsonl"
    path.write_bytes(b'{"ts": "old"}\n{"ts": "torn')
    sink = w.JsonlAppender(path)
    sink.write({"a": 1})
    sink.write({"b": "ü"})
    sink.close()
    lines = path.read_bytes().split(b"\n")
    assert lines[:2] == [b'{"ts": "old"}', b'{"ts": "torn'] and lines[2:] == [b'{"a":1}', '{"b":"ü"}'.encode(), b""]
    assert read_jsonl(path) == [{"ts": "old"}, {"a": 1}, {"b": "ü"}]


def test_appender_refuses_nan(tmp_path):
    sink = w.JsonlAppender(tmp_path / "b.jsonl")
    with pytest.raises(ValueError):
        sink.write({"x": float("nan")})
    sink.close()


def _markets(tmp_path):
    path = tmp_path / "markets.jsonl"
    path.write_text(json.dumps({"ts": "2026-10-08T21:00:00Z", "id": "m1", "question": "q", "condition_id": M,
                                "token_ids": [A, B], "outcomes": ["Yes", "No"], "liquidity_usd": 50000.0}) + "\n",
                    encoding="utf-8")
    return path


def _bus_free(tmp_path):
    return {"bus_path": tmp_path / "bus.jsonl", "sentinel_state_path": tmp_path / "s.json",
            "quotes_path": tmp_path / "q.jsonl"}


def test_paper_cycle_reads_recorded_rows_as_fresh_and_markers_as_stale(tmp_path):
    books = tmp_path / "books.jsonl"
    meta = w.asset_meta(read_jsonl(_markets(tmp_path)))
    sink = w.JsonlAppender(books)
    core = w.BookCore([A, B], meta)
    for row in core.handle([_book(A), _book(B, bids=(("0.46", "1"),), asks=(("0.50", "1"),))], TS).rows:
        sink.write(row)
    sink.close()
    now = "2026-10-08T21:51:00Z"
    fresh = paper_cycle.run(markets_path=_markets(tmp_path), books_path=books, now=now, **_bus_free(tmp_path))
    assert fresh["ok"] and fresh["stale"] == 0
    rows = read_jsonl(books)
    assert book_age_problem(rows[0]["book_ts"], parse_book_ts(now)) is None
    watch = sentinel.StaleWatch(bus_path=tmp_path / "bus.jsonl", quotes_path=tmp_path / "q.jsonl")
    assert watch.check("m1", rows[0]["book_ts"], parse_book_ts(now))["stale"] is False

    sink = w.JsonlAppender(books)
    for row in core.handle_text("{garbage", "2030-01-01T00:00:01Z").rows:
        sink.write(row)
    sink.close()
    stale = paper_cycle.run(markets_path=_markets(tmp_path), books_path=books, now=now, **_bus_free(tmp_path))
    assert stale["stale"] == 1 and stale["gaps"] == 0


def test_unmapped_assets_get_no_market_id_and_paper_cycle_ignores_them(tmp_path):
    row = _feed(_core(), _book()).rows[0]
    assert row["market_id"] == ""
    books = tmp_path / "books.jsonl"
    books.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert paper_cycle._latest_books(read_jsonl(books)) == {}


def test_asset_meta_uses_the_latest_market_row_and_drops_ambiguous_tokens():
    rows = [{"ts": "2026-10-08T00:00:00Z", "id": "m1", "token_ids": [A, B], "outcomes": ["Yes", "No"], "question": "old"},
            {"ts": "2026-10-08T01:00:00Z", "id": "m1", "token_ids": [A, B], "outcomes": ["Yes", "No"], "question": "new"},
            {"ts": "2026-10-08T01:00:00Z", "id": "m2", "token_ids": [B, "333"], "outcomes": ["Yes", "No"]}]
    meta = w.asset_meta(rows)
    assert meta[A]["question"] == "new" and meta[A]["outcome"] == "Yes"
    assert B not in meta and meta["333"]["market_id"] == "m2"


# --- CLI -------------------------------------------------------------------------------------------


def _cli(tmp_path, argv, sessions, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(w.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(w.time, "sleep", clock.sleep)
    transport = FakeTransport(sessions, clock)
    code = w.main(argv + ["--out", str(tmp_path / "books.jsonl"), "--markets", str(_markets(tmp_path))],
                  transport=transport)
    return code, transport


def test_cli_ok_writes_mapped_rows(tmp_path, monkeypatch, capsys):
    code, transport = _cli(tmp_path, ["--assets", f"{A},{B}", "--max-runtime", "5"],
                           [[_book(A), _book(B)]], monkeypatch)
    assert code == w.EXIT_OK and transport.urls == [w.MARKET_WS_URL]
    rows = read_jsonl(tmp_path / "books.jsonl")
    assert [row["market_id"] for row in rows] == ["m1", "m1"]
    assert "orders=none" in capsys.readouterr().out


def test_cli_without_a_snapshot_exits_1(tmp_path, monkeypatch):
    code, _ = _cli(tmp_path, ["--assets", A, "--max-runtime", "5"], [], monkeypatch)
    assert code == w.EXIT_NO_ROWS


@pytest.mark.parametrize("argv", [[], ["--assets", A], ["--max-runtime", "5"], ["--assets", A, "--max-runtime", "0"],
                                  ["--assets", A, "--max-runtime", "nan"], ["--assets", A, "--max-runtime", "inf"],
                                  ["--assets", A, "--max-runtime", "-3"], ["--assets", ",", "--max-runtime", "5"],
                                  ["--assets", A, "--max-runtime", "x"]])
def test_cli_usage_errors_exit_2(tmp_path, monkeypatch, argv):
    code, transport = _cli(tmp_path, argv, [[_book()]], monkeypatch)
    assert code == w.EXIT_USAGE and transport.urls == []


def test_cli_without_websockets_exits_3(tmp_path, monkeypatch):
    _block_websockets(monkeypatch)
    out = tmp_path / "books.jsonl"
    code = w.main(["--assets", A, "--max-runtime", "5", "--out", str(out), "--markets", str(_markets(tmp_path))])
    assert code == w.EXIT_NO_WEBSOCKETS and not out.exists()


def test_cli_write_failure_exits_4(tmp_path, monkeypatch):
    monkeypatch.setattr(w.JsonlAppender, "write", lambda self, row: (_ for _ in ()).throw(OSError("disk full")))
    code, _ = _cli(tmp_path, ["--assets", A, "--max-runtime", "5"], [[_book()]], monkeypatch)
    assert code == w.EXIT_WRITE_FAILED


def test_module_entry_point_help_runs_without_websockets():
    result = subprocess.run([sys.executable, "-m", "src.ws_recorder", "--help"], cwd=ROOT,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0 and "Exit codes" in result.stdout


# --- steering (a): optional dependency, exact pin, lazy import -----------------------------------


def _requirement_lines(path: Path) -> list[str]:
    return [line.split("#", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines()
            if line.split("#", 1)[0].strip()]


def test_websockets_is_optional_and_pinned_exactly():
    assert _requirement_lines(ROOT / "requirements-ws.txt") == ["websockets==17.2.0"]
    assert re.fullmatch(r"websockets==\d+\.\d+\.\d+", _requirement_lines(ROOT / "requirements-ws.txt")[0])
    for core in ("requirements.txt", "requirements-dev.txt"):
        assert not any("websockets" in line.lower() for line in _requirement_lines(ROOT / core)), core


def test_websockets_is_imported_only_inside_the_adapter():
    tree = ast.parse((ROOT / "src" / "ws_recorder.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names] + [getattr(node, "module", None) or ""]
            assert not any(name.startswith("websockets") for name in names)
    adapter = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WebsocketsTransport")
    inside = [node for node in ast.walk(adapter) if isinstance(node, ast.ImportFrom)]
    assert {node.module for node in inside} == {"websockets.exceptions", "websockets.sync.client"}
    everywhere = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                  and (node.module or "").startswith("websockets")]
    assert len(everywhere) == len(inside)


def test_importing_the_module_does_not_import_websockets():
    code = "import sys; import src.ws_recorder; print('websockets' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert result.stdout.strip() == "False", result.stderr


def _block_websockets(monkeypatch):
    for name in ("websockets", "websockets.sync", "websockets.sync.client", "websockets.exceptions"):
        monkeypatch.setitem(sys.modules, name, None)


def test_missing_websockets_is_transport_unavailable(monkeypatch):
    _block_websockets(monkeypatch)
    with pytest.raises(w.TransportUnavailable, match="requirements-ws.txt"):
        w.WebsocketsTransport().connect(w.MARKET_WS_URL, 5.0)


# --- a fake websockets package for the real adapter ----------------------------------------------


class _FakeSocket:
    def __init__(self, inbox):
        self.sent: list[str] = []
        self.inbox = list(inbox)

    def send(self, text):
        self.sent.append(text)

    def recv(self, timeout=None):
        if not self.inbox:
            raise TimeoutError
        item = self.inbox.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        pass


def _fake_websockets(monkeypatch, inbox=()):
    calls: list[tuple[str, dict]] = []
    sockets: list[_FakeSocket] = []

    class ConnectionClosed(Exception):
        pass

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        sockets.append(_FakeSocket(inbox))
        return sockets[-1]

    package = types.ModuleType("websockets")
    sync = types.ModuleType("websockets.sync")
    client = types.ModuleType("websockets.sync.client")
    exceptions = types.ModuleType("websockets.exceptions")
    client.connect = connect
    exceptions.ConnectionClosed = ConnectionClosed
    package.sync, sync.client, package.exceptions = sync, client, exceptions
    for name, module in (("websockets", package), ("websockets.sync", sync), ("websockets.sync.client", client),
                         ("websockets.exceptions", exceptions)):
        monkeypatch.setitem(sys.modules, name, module)
    return calls, sockets, ConnectionClosed


def test_adapter_maps_timeouts_closes_and_bytes(monkeypatch):
    calls, sockets, closed = _fake_websockets(monkeypatch)
    connection = w.WebsocketsTransport().connect(w.MARKET_WS_URL, 5.0)
    sockets[0].inbox = ["PONG", b'{"x": 1}', closed("bye")]
    assert connection.recv(1.0) == "PONG" and connection.recv(1.0) == '{"x": 1}'
    with pytest.raises(w.TransportClosed):
        connection.recv(1.0)
    sockets[0].inbox = []
    assert connection.recv(1.0) is None


# --- steering (b): URL allowlist before connecting ------------------------------------------------


BAD_URLS = [
    "ws://ws-subscriptions-clob.polymarket.com/ws/market",
    "wss://ws-subscriptions-clob.polymarket.com/ws/user",
    "wss://ws-subscriptions-clob.polymarket.com/ws/market/",
    "wss://ws-subscriptions-clob.polymarket.com/ws/market/extra",
    "wss://ws-subscriptions-clob.polymarket.com/ws/market?x=1",
    "wss://ws-subscriptions-clob.polymarket.com/ws/market#f",
    "wss://ws-subscriptions-clob.polymarket.com:443/ws/market",
    "wss://user:pw@ws-subscriptions-clob.polymarket.com/ws/market",
    "wss://ws-subscriptions-clob.polymarket.com.evil.example/ws/market",
    "wss://evil.example/ws/market",
    "wss://sports-api.polymarket.com/ws",
    "wss://ws-live-data.polymarket.com/ws/market",
    "https://ws-subscriptions-clob.polymarket.com/ws/market",
    "WSS://ws-subscriptions-clob.polymarket.com/ws/market",
    "wss://WS-SUBSCRIPTIONS-CLOB.polymarket.com/ws/market",
    "wss://ws-subscriptions-clob.polymarket.com/WS/MARKET",
    " wss://ws-subscriptions-clob.polymarket.com/ws/market",
    "wss://ws-subscriptions-clob.polymarket.com//ws/market",
    "wss://ws-subscriptions-clob.polymarket.com/ws/../ws/market",
    "",
    None,
    b"wss://ws-subscriptions-clob.polymarket.com/ws/market",
]


@pytest.mark.parametrize("url", BAD_URLS, ids=repr)
def test_url_allowlist_refuses_and_connects_to_nothing(monkeypatch, url):
    calls, _, _ = _fake_websockets(monkeypatch)
    with pytest.raises(w.UrlNotAllowed):
        w.check_market_url(url)
    with pytest.raises(w.UrlNotAllowed):
        w.WebsocketsTransport().connect(url, 5.0)
    transport = FakeTransport([[_book()]], Clock())
    with pytest.raises(w.UrlNotAllowed):
        w.Recorder([A], transport=transport, sink=ListSink(), max_runtime_s=5, url=url)
    assert calls == [] and transport.urls == []


def test_the_market_channel_is_the_only_allowed_url(monkeypatch):
    calls, _, _ = _fake_websockets(monkeypatch)
    assert w.check_market_url("wss://ws-subscriptions-clob.polymarket.com/ws/market") == w.MARKET_WS_URL
    w.WebsocketsTransport().connect(w.MARKET_WS_URL, 5.0)
    assert [url for url, _ in calls] == [w.MARKET_WS_URL]


# --- steering (c): no auth, no headers, only the public market subscription ----------------------


BANNED_FRAME_WORDS = ("auth", "apikey", "api_key", "secret", "passphrase", "signature", "key", "user", "markets",
                      "token\"", "address", "nonce", "order")


def _assert_public_frame(frame: str) -> None:
    assert w.check_outgoing_frame(frame) == frame
    if frame == "PING":
        return
    lowered = frame.lower()
    for word in BANNED_FRAME_WORDS:
        assert word not in lowered, (word, frame)
    parsed = json.loads(frame)
    assert set(parsed) in ({"assets_ids", "type", "initial_dump"}, {"operation", "assets_ids"})
    assert parsed.get("type", "market") == "market"


def test_every_outgoing_frame_is_public_through_a_busy_run():
    sessions = [
        [_book(A), _book(B), _pc(_ch(A, size="nan")), "IDLE", _book(A, ts=T0 + 5), "{bad", "CLOSE"],
        OSError("down"),
        [_book(A), _book(B), "IDLE", "IDLE"],
    ]
    report, transport, *_ = _run(sessions, assets=(A, B), runtime=120)
    assert report.connects == 2 and len(transport.sent) > 5
    for frame in transport.sent:
        _assert_public_frame(frame)


def test_the_real_adapter_sends_no_headers_and_only_checked_frames(monkeypatch):
    calls, sockets, _ = _fake_websockets(monkeypatch, inbox=[json.dumps([_book(A)]), "PONG"])
    clock = Clock()
    recorder = w.Recorder([A], transport=w.WebsocketsTransport(), sink=ListSink(), max_runtime_s=25,
                          monotonic=clock.monotonic, sleep=clock.sleep, now=lambda: TS)
    original_recv = _FakeSocket.recv

    def ticking_recv(self, timeout=None):
        clock.t += timeout or 0
        return original_recv(self, timeout)

    monkeypatch.setattr(_FakeSocket, "recv", ticking_recv)
    recorder.run()
    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == w.MARKET_WS_URL and kwargs == {"open_timeout": min(WS_RESYNC_TIMEOUT_S, 25.0), "ping_interval": None}
    assert not any("header" in name or "auth" in name or "origin" in name for name in kwargs)
    assert sockets[0].sent and all(w.check_outgoing_frame(frame) == frame for frame in sockets[0].sent)
    for frame in sockets[0].sent:
        _assert_public_frame(frame)


@pytest.mark.parametrize("frame", [
    json.dumps({"assets_ids": [A], "type": "user", "initial_dump": True}),
    json.dumps({"assets_ids": [A], "type": "market", "initial_dump": True, "auth": {"apiKey": "k"}}),
    json.dumps({"markets": [M], "type": "user", "auth": {}}),
    json.dumps({"assets_ids": [A], "type": "market"}),
    json.dumps({"assets_ids": [A], "type": "market", "initial_dump": False}),
    json.dumps({"assets_ids": [A], "type": "market", "initial_dump": True, "custom_feature_enabled": True}),
    json.dumps({"operation": "delete", "assets_ids": [A]}),
    json.dumps({"operation": "subscribe", "assets_ids": []}),
    json.dumps({"operation": "subscribe", "assets_ids": [A], "secret": "s"}),
    json.dumps({"operation": "subscribe", "assets_ids": ["a b"]}),
    json.dumps([A]),
    "ping",
    "PONG",
    "not json",
    b"PING",
    None,
])
def test_frame_allowlist_refuses_anything_else(frame):
    with pytest.raises(w.FrameNotAllowed if isinstance(frame, (str, bytes, type(None))) else Exception):
        w.check_outgoing_frame(frame)


def test_the_adapter_refuses_a_bad_frame_before_sending(monkeypatch):
    _, sockets, _ = _fake_websockets(monkeypatch)
    connection = w.WebsocketsTransport().connect(w.MARKET_WS_URL, 5.0)
    with pytest.raises(w.FrameNotAllowed):
        connection.send(json.dumps({"type": "user", "auth": {"apiKey": "k"}, "markets": []}))
    assert sockets[0].sent == []


# --- steering (d): no order-capable package in requirements or imports ---------------------------


# Task AH: the list lives in scripts/guard.py (single source of truth); it may only grow.
AG_ORDER_CAPABLE = {"py_clob_client", "py_order_utils", "py_builder_signing_sdk", "clob_client", "polymarket_apis",
                    "web3", "eth_account", "eth_keys", "ccxt", "polymarket"}
ORDER_CAPABLE = guard.ORDER_CAPABLE


def test_the_guard_list_is_the_single_source_and_only_grows():
    assert ORDER_CAPABLE >= AG_ORDER_CAPABLE


def _norm(name: str) -> str:
    return re.split(r"[<>=!~\[; ]", name.strip(), maxsplit=1)[0].lower().replace("-", "_").replace(".", "_")


def test_no_order_capable_package_in_any_requirements_file():
    files = sorted(ROOT.glob("requirements*.txt"))
    assert {path.name for path in files} >= {"requirements.txt", "requirements-dev.txt", "requirements-ws.txt"}
    for path in files:
        for line in _requirement_lines(path):
            assert _norm(line) not in ORDER_CAPABLE, (path.name, line)
            assert "clob" not in _norm(line), (path.name, line)
            assert guard.banned_requirement(line) is None, (path.name, line)


def test_no_order_capable_import_anywhere_in_src():
    for path in sorted((ROOT / "src").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                roots = [(node.module or "").split(".")[0]]
            else:
                continue
            for root in roots:
                assert root.lower() not in ORDER_CAPABLE, (path.name, root)
        for _, module in guard.imported_modules(tree):
            assert guard.banned_module(module) is None, (path.name, module)


def test_recorder_source_has_no_auth_or_order_code():
    source = (ROOT / "src" / "ws_recorder.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    body = code.split('"""', 2)[2]  # skip the module docstring, which names what is refused
    for word in ("apiKey", "api_key", "passphrase", "POLY_", "private_key", "/order", "load_dotenv", "os.environ",
                 "additional_headers", "extra_headers"):
        assert word not in body, word


# --- thresholds ----------------------------------------------------------------------------------


def test_timing_constants_come_from_thresholds():
    source = (ROOT / "src" / "ws_recorder.py").read_text(encoding="utf-8")
    for name in ("WS_BACKOFF_BASE_S", "WS_BACKOFF_CAP_S", "WS_RESYNC_TIMEOUT_S", "WS_PING_INTERVAL_S", "WS_ROW_REFRESH_S"):
        assert re.search(rf"^{name}\s*=", source, re.M) is None
    assert 0 < WS_ROW_REFRESH_S < MAX_BOOK_AGE_S


# --- Task AS (gap 5): the runtime pin check -------------------------------------------------------


def _drift_after(monkeypatch, tmp_path, clean_checks):
    """src.pins sees a pinned file for ``clean_checks`` checks, then drift; halt lines go to tmp_path."""
    import src.podlib as podlib
    from src import pins

    calls = {"n": 0}

    def checked(risk_path):
        calls["n"] += 1
        return [] if calls["n"] <= clean_checks else ["live_enabled=True expected False"]
    monkeypatch.setattr(pins, "_checked", checked)
    monkeypatch.setattr(podlib, "BUS_PATH", tmp_path / "bus.jsonl")
    return calls


def test_cli_stops_with_6_when_the_risk_file_drifts_mid_run(tmp_path, monkeypatch, capsys):
    import src.halt_store as halt_store
    from src.thresholds import PIN_RECHECK_S

    calls = _drift_after(monkeypatch, tmp_path, 2)  # the start check and the first runtime check pass
    clock_box = {}
    real_clock = Clock

    class Recording(real_clock):
        def __init__(self):
            super().__init__()
            clock_box["clock"] = self
    monkeypatch.setattr(sys.modules[__name__], "Clock", Recording)
    code, _ = _cli(tmp_path, ["--assets", A, "--max-runtime", "600"], [[_book(A)]], monkeypatch)
    assert code == w.EXIT_PIN_DRIFT == 6 and calls["n"] == 3
    assert PIN_RECHECK_S <= clock_box["clock"].t < 600  # stopped at the re-check, not at --max-runtime
    assert len(read_jsonl(tmp_path / "books.jsonl")) == 1
    assert [r["reason"] for r in halt_store.active_halts()] == ["risk_toml_pin_drift"]
    assert "drifted while running" in capsys.readouterr().err


def test_cli_refuses_with_6_before_connecting(tmp_path, monkeypatch):
    _drift_after(monkeypatch, tmp_path, 0)
    code, transport = _cli(tmp_path, ["--assets", A, "--max-runtime", "5"], [[_book(A)]], monkeypatch)
    assert code == w.EXIT_PIN_DRIFT and transport.urls == [] and not (tmp_path / "books.jsonl").exists()


# --- Task AW: snapshot hash (recorded, not enforced), PONG timeout, escalation ------------------------


def _hashed_book(**changes):
    """A synthetic snapshot whose hash is the SHA-1 of the literal compact JSON below (field order pinned)."""
    literal = ('{"market":"%s","asset_id":"%s","timestamp":"%d","hash":"","bids":[{"price":"0.48","size":"30"}],'
               '"asks":[{"price":"0.52","size":"25"}],"min_order_size":"5","tick_size":"0.01","neg_risk":false,'
               '"last_trade_price":"0.5"}') % (M, A, T0)
    message = _book(min_order_size="5", tick_size="0.01", neg_risk=False, last_trade_price="0.5")
    message["hash"] = hashlib.sha1(literal.encode()).hexdigest()
    message.update(changes)
    return message


def test_task_aw_book_hash_check_matches_the_documented_field_order():
    assert w.book_hash_check(_hashed_book()) == "match"
    # the hash covers the levels: a frame whose levels differ from what was hashed mismatches
    assert w.book_hash_check(_hashed_book(bids=[{"price": "0.47", "size": "30"}])) == "mismatch"
    reordered = _hashed_book()
    assert w.book_hash_check({k: reordered[k] for k in sorted(reordered)}) == "match"  # frame key order is irrelevant


@pytest.mark.parametrize("change, expected", [
    ({"hash": ""}, "absent"), ({"hash": None}, "absent"), ({"hash": 5}, "absent"),
    ({"neg_risk": float("nan")}, "uncomputable"),
])
def test_task_aw_book_hash_check_outcomes(change, expected):
    assert w.book_hash_check(_hashed_book(**change)) == expected


def test_task_aw_book_hash_check_needs_every_hashed_field():
    message = _hashed_book()
    del message["last_trade_price"]
    assert w.book_hash_check(message) == "incomplete"
    assert w.book_hash_check(_book()) == "incomplete"  # WS frames without min_order_size etc.


def test_task_aw_the_hash_is_recorded_but_a_mismatch_does_not_resync():
    assert w.BOOK_HASH_ENFORCED is False  # Poly指挥 ruling: not validated on real WS snapshots yet
    core = _core()
    out = _feed(core, _hashed_book(), _hashed_book(hash="f" * 40, timestamp=str(T0 + 1)), _book(ts=T0 + 2))
    assert out.resyncs == [] and _live(core)
    assert [row["book_hash_check"] for row in out.rows] == ["match", "mismatch", "incomplete"]
    assert out.counts["hash_check:match"] == 1 and out.counts["hash_check:mismatch"] == 1


def test_task_aw_an_enforced_hash_mismatch_resyncs_and_counts_as_a_gap(monkeypatch):
    monkeypatch.setattr(w, "BOOK_HASH_ENFORCED", True)
    core = _core()
    assert _feed(core, _hashed_book()).rows[0]["book_hash_check"] == "match" and _live(core)
    _assert_resync(core, _feed(core, _hashed_book(hash="f" * 40, timestamp=str(T0 + 1))), "book_hash_mismatch")
    core = _core()
    assert _feed(core, _book()).rows == [] and not _live(core)  # enforced: an uncheckable snapshot is not trusted
    report, *_ = _run([[_hashed_book(hash="f" * 40)]], runtime=5)
    assert report.counts["resync:book_hash_mismatch"] >= 1


def test_task_aw_no_pong_within_the_timeout_reconnects():
    report, transport, sink, clock = _run([[_book()], [_book(ts=T0 + 9000)]], pong=False,
                                          runtime=WS_PING_INTERVAL_S + WS_PONG_TIMEOUT_S + 5)
    assert report.disconnects == {"pong_timeout": 1} and report.connects == 2
    assert [row["ws_state"] for row in sink.rows] == ["live", "resync", "live"]
    assert sink.rows[1]["resync_reason"] == "disconnect_pong_timeout"


def test_task_aw_the_pong_timeout_runs_from_the_first_unanswered_ping():
    # PINGs keep going every WS_PING_INTERVAL_S; a later PING does not restart the 30 s wait
    report, transport, *_ = _run([[_book()]], pong=False, runtime=WS_PING_INTERVAL_S + WS_PONG_TIMEOUT_S - 0.5)
    assert report.disconnects == {} and report.pings >= 3
    report, *_ = _run([[_book()]], pong=False, runtime=WS_PING_INTERVAL_S + WS_PONG_TIMEOUT_S + 0.5)
    assert report.disconnects == {"pong_timeout": 1}


def _trade(asset=A, ts=T0, market=M):
    """Task BI: a business frame that leaves the book alone (keeps the silence window open in tests)."""
    return {"event_type": "last_trade_price", "asset_id": asset, "market": market, "price": "0.5", "size": "1",
            "side": "BUY", "timestamp": str(ts)}


def test_task_aw_answered_pings_keep_the_connection(monkeypatch):
    monkeypatch.setattr(w, "WS_DATA_SILENCE_S", 1e9)  # Task BK: AW's PONG path alone (no market data here)
    report, *_ = _run([[_book()]], runtime=WS_PONG_TIMEOUT_S * 6)
    assert report.disconnects == {} and report.connects == 1 and report.pings >= 15


def test_task_aw_pong_timeouts_count_toward_the_reconnect_escalation():
    sessions = [[_book()] for _ in range(RECORDER_RECONNECT_ALERT_COUNT + 1)]
    clock = Clock()
    rec = w.Recorder([A], transport=FakeTransport(sessions, clock, pong=False), sink=ListSink(),
                     max_runtime_s=(WS_PING_INTERVAL_S + WS_PONG_TIMEOUT_S + WS_BACKOFF_BASE_S) * 3,
                     monotonic=clock.monotonic, sleep=clock.sleep, now=lambda: TS)
    rec.run()
    assert rec.report.disconnects["pong_timeout"] >= 2
    assert rec.progress()["reconnects_in_window"] >= rec.report.disconnects["pong_timeout"]
    assert rec.progress()["gap_resyncs_in_window"] == {}  # a disconnect is not a gap


def test_task_aw_gap_escalation_counts_unknown_events_but_not_tick_changes():
    clock = Clock()
    rec = w.Recorder([A], transport=FakeTransport([], clock), sink=ListSink(), max_runtime_s=10,
                     monotonic=clock.monotonic, sleep=clock.sleep, now=lambda: TS)
    rec._write(w.Outcome(resyncs=[(A, "tick_size_change")]))
    assert rec.progress()["gap_resyncs_in_window"] == {}
    rec._write(w.Outcome(resyncs=[(A, "unknown_event"), (A, "book_hash_mismatch")]))
    assert rec.progress()["gap_resyncs_in_window"] == {A: 2}


def test_task_aw_a_tick_change_resubscribes_the_asset_for_a_fresh_snapshot():
    report, transport, sink, _ = _run([[_book(), _tick(), _book(tick_size="0.001", ts=T0 + 2000)]], runtime=5)
    frames = [json.loads(f) for f in transport.sent if f != "PING"]
    assert [f.get("operation") for f in frames] == [None, "unsubscribe", "subscribe"]
    assert frames[2]["assets_ids"] == [A]
    assert [row["ws_state"] for row in sink.rows] == ["live", "resync", "live"]
    assert sink.rows[1]["resync_reason"] == "tick_size_change" and sink.rows[2]["tick_size"] == 0.001
