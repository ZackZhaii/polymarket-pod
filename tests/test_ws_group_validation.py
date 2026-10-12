"""Issue #182 (Task CC): ws_recorder validates crossed / top-of-book per same-timestamp group, not per frame.

Poly盘口's M1 resync diagnosis (/workspace/pod-bots/poly-book-m1-resync-diag.md): the exchange splits one
match into several price_change frames that share a timestamp. The resting remainder comes first, the
consumed level's removal next, then a book. The state at the end of the first frame can be crossed, or
behind the frame's own best_bid / best_ask. dev resynced on that intermediate state, so a hot market blew
through M1_MAX_GAP_RESYNCS_PER_ASSET. Fixes:
- (A) Such a state is held, never written, until the group closes. A same-ts frame that makes it consistent
  applies it; the asset's book replaces it. A newer timestamp, a tick change or WS_GROUP_FLUSH_S resyncs as
  before.
- (B) Rows publish the exchange's own best_bid / best_ask, which the applied levels must agree with.
- (C) A late resubscribe snapshot, older than deltas applied after the asset recovered, is dropped and
  counted (``stale_resubscribe_snapshot``) instead of a second resync.

The repro vectors are Poly盘口's real frames (``{"t": receive time, "raw": frame}``):
tests/fixtures/ws_resync/minimal_vector.jsonl (top_of_book_mismatch, event 1) and real_crossed_vector.jsonl
(the crossed_book event at ts 1791665125010): dev resyncs each asset once on each (2 per file); fixed: 0.
live_top_mismatch_window.jsonl is a 170-frame window of the box's live capture (dev: 4 resyncs; fixed: 0).
The reverse tests make sure real gaps still resync. Offline and pure: no network, no clock, no order.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.ws_recorder as w
from src.thresholds import M1_MAX_GAP_RESYNCS_PER_ASSET, WS_GROUP_FLUSH_S, WS_RESYNC_TIMEOUT_S

FIX = Path(__file__).parent / "fixtures" / "ws_resync"
YES = "107540403103917101723616128435236138591116364556711257437238922612332295492509"
NO = "18368161459732947229970694377522451372979676356971809107906485726590890158916"
TS = "2030-01-01T00:00:00Z"


def _frames(name):
    return [json.loads(line) for line in (FIX / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def _merge(total, part):
    total.rows += part.rows
    total.resyncs += part.resyncs
    total.counts.update(part.counts)


def replay(frames, *, settle=True):
    """Feed frames as the recorder's session loop does: due groups are flushed before each frame (mono = the
    receive time), and once more WS_GROUP_FLUSH_S after the last one."""
    core = w.BookCore([YES, NO])
    out = w.Outcome()
    for frame in frames:
        _merge(out, core.flush_groups(TS, frame["t"]))
        _merge(out, core.handle_text(frame["raw"], TS, frame["t"]))
    if settle:
        _merge(out, core.flush_groups(TS, frames[-1]["t"] + WS_GROUP_FLUSH_S))
    return core, out


def _levels(raw_book):
    message = json.loads(raw_book)
    return ({float(level["price"]): float(level["size"]) for level in message["bids"]},
            {float(level["price"]): float(level["size"]) for level in message["asks"]})


# --- the repro vectors: 0 resyncs, and the final book is the exchange snapshot ----------------------------


@pytest.mark.parametrize("name", ["minimal_vector.jsonl", "real_crossed_vector.jsonl",
                                  "live_top_mismatch_window.jsonl"])
def test_repro_vector_gives_no_resync_and_ends_on_the_snapshot(name):
    frames = _frames(name)
    core, out = replay(frames)
    assert out.resyncs == [] and not any(key.startswith("resync:") for key in out.counts)
    assert core.all_live() and out.counts["group_held"] >= 1
    last = {}  # asset -> its last snapshot, when no delta for it came after that snapshot
    for frame in frames:
        try:
            decoded = json.loads(frame["raw"])
        except ValueError:
            continue  # e.g. PONG
        for message in decoded if isinstance(decoded, list) else [decoded]:
            if "bids" in message and message.get("event_type", "book") == "book":
                last[message["asset_id"]] = json.dumps(message)
            for item in message.get("price_changes") or []:
                last.pop(item.get("asset_id"), None)
    for asset, raw in last.items():
        assert (core.books[asset].bids, core.books[asset].asks) == _levels(raw)
    assert all(row["ws_state"] == "live" for row in out.rows)


def test_minimal_vector_group_resolves_on_its_second_frame_without_the_books():
    """Event 1: frame 1 reports bb 0.950 while 0.951 still rests; frame 2 (same ts) removes 0.951. The group
    is applied at frame 2 (before any book), and the row shows the exchange's 0.950."""
    frames = _frames("minimal_vector.jsonl")[:4]
    core, out = replay(frames)
    assert out.resyncs == [] and out.counts["group_held"] == 2 and out.counts["group_resolved"] == 2
    rows = [row for row in out.rows if row["ws_event"] == "price_change" and row["token_id"] == YES]
    assert rows and rows[-1]["best_bid"] == 0.95 and rows[-1]["best_ask"] == 0.952
    assert max(core.books[YES].bids) == 0.95


def test_held_state_is_never_written():
    frames = _frames("real_crossed_vector.jsonl")
    core, out = replay(frames[:3], settle=False)  # two snapshots, then the crossing first frame
    assert [row["ws_event"] for row in out.rows] == ["book", "book"]
    assert core.books[YES].pending is not None and core.books[YES].pending["reason"] == "crossed_book"


def test_each_vector_holds_exactly_the_two_states_dev_resynced_on():
    """dev (per-frame checks) resynced YES and NO once each on these vectors (2 per file, Poly盘口's replay);
    those are exactly the two intermediate states now held, and both are closed without a resync."""
    for name in ("minimal_vector.jsonl", "real_crossed_vector.jsonl"):
        _, out = replay(_frames(name))
        assert out.counts["group_held"] == 2 and out.counts["group_unresolved"] == 0, name
        assert out.counts["group_resolved"] + out.counts["group_replaced_by_book"] == 2, name


def test_the_m1_cap_is_untouched():
    assert M1_MAX_GAP_RESYNCS_PER_ASSET == 24


# --- reverse: real gaps still resync -------------------------------------------------------------------


def test_a_dropped_closing_frame_resyncs_when_the_timestamp_moves_on():
    frames = _frames("minimal_vector.jsonl")
    later = json.loads(frames[3]["raw"])
    later["timestamp"] = "1791664462500"  # the removal frame never came; the stream moved on
    later["price_changes"] = [dict(item, price="0.940", size="5", best_bid="0.950", best_ask="0.952")
                              if item["asset_id"] == YES else dict(item, price="0.060", size="5",
                                                                     best_bid="0.048", best_ask="0.050")
                              for item in later["price_changes"]]
    core, out = replay(frames[:3] + [{"t": frames[3]["t"], "raw": json.dumps(later)}], settle=False)
    assert sorted(out.resyncs) == sorted([(YES, "top_of_book_mismatch"), (NO, "top_of_book_mismatch")])
    assert out.counts["group_unresolved"] == 2 and not core.all_live()


def test_a_dropped_closing_frame_resyncs_at_the_group_deadline():
    frames = _frames("minimal_vector.jsonl")[:3]
    core, out = replay(frames, settle=False)
    assert out.resyncs == []
    assert core.flush_groups(TS, frames[-1]["t"] + WS_GROUP_FLUSH_S * 0.9).resyncs == []
    late = core.flush_groups(TS, frames[-1]["t"] + WS_GROUP_FLUSH_S)
    assert sorted(late.resyncs) == sorted([(YES, "top_of_book_mismatch"), (NO, "top_of_book_mismatch")])
    assert [row["ws_state"] for row in late.rows] == ["resync", "resync"]  # invalidation rows at once


def test_more_same_ts_frames_cannot_postpone_the_deadline():
    frames = _frames("real_crossed_vector.jsonl")
    core, out = replay(frames[:3], settle=False)
    start = frames[2]["t"]
    again = json.loads(frames[2]["raw"])  # same ts, still crossed
    _merge(out, core.handle_text(json.dumps(again), TS, start + WS_GROUP_FLUSH_S * 0.8))
    assert out.resyncs == []
    assert core.next_group_deadline() == pytest.approx(start + WS_GROUP_FLUSH_S)
    late = core.flush_groups(TS, start + WS_GROUP_FLUSH_S)
    assert sorted(late.resyncs) == sorted([(YES, "crossed_book"), (NO, "crossed_book")])


def test_a_genuinely_regressing_timestamp_resyncs_at_once_even_inside_a_group():
    frames = _frames("minimal_vector.jsonl")
    older = json.loads(frames[3]["raw"])
    older["timestamp"] = "1791664461965"  # 1 ms before the open group
    core, out = replay(frames[:3] + [{"t": frames[3]["t"], "raw": json.dumps(older)}], settle=False)
    assert sorted(out.resyncs) == sorted([(YES, "timestamp_regression"), (NO, "timestamp_regression")])


def test_an_unknown_level_removal_inside_a_group_is_still_a_hard_gap():
    frames = _frames("minimal_vector.jsonl")
    bad = json.loads(frames[3]["raw"])
    bad["price_changes"] = [dict(item, price="0.930") if item["asset_id"] == YES else item
                            for item in bad["price_changes"]]
    core, out = replay(frames[:3] + [{"t": frames[3]["t"], "raw": json.dumps(bad)}], settle=False)
    assert (YES, "unknown_level_removed") in out.resyncs


def test_a_crossed_book_snapshot_still_resyncs_while_a_group_is_open():
    frames = _frames("minimal_vector.jsonl")
    crossed = json.loads(frames[4]["raw"])
    crossed["asks"] = [{"price": "0.949", "size": "1"}]
    core, out = replay(frames[:3] + [{"t": frames[4]["t"], "raw": json.dumps(crossed)}], settle=False)
    assert (YES, "crossed_book") in out.resyncs


def test_a_tick_size_change_closes_an_open_group_with_its_reason():
    frames = _frames("real_crossed_vector.jsonl")
    core, out = replay(frames[:3], settle=False)
    tick = {"event_type": "tick_size_change", "asset_id": YES, "market": json.loads(frames[0]["raw"])["market"],
            "old_tick_size": "0.001", "new_tick_size": "0.01", "timestamp": "1791665125010"}
    _merge(out, core.handle(tick, TS, frames[2]["t"] + 0.001))
    assert out.resyncs == [(YES, "crossed_book")]


def test_a_disconnect_drops_an_open_group():
    core, _ = replay(_frames("real_crossed_vector.jsonl")[:3], settle=False)
    core.invalidate_all("disconnect_closed", TS, 1.0)
    assert all(book.pending is None for book in core.books.values())
    assert core.next_group_deadline() is None


# --- (C) the late resubscribe snapshot ---------------------------------------------------------------------

A = "111"
M = "0xcond"
T0 = 1791496217153


def _book(ts, bids=(("0.48", "30"),), asks=(("0.52", "25"),)):
    return {"event_type": "book", "asset_id": A, "market": M, "timestamp": str(ts), "hash": "h",
            "bids": [{"price": p, "size": s} for p, s in bids], "asks": [{"price": p, "size": s} for p, s in asks]}


def _pc(ts, price, size, side="BUY", **extra):
    item = {"asset_id": A, "price": price, "size": size, "side": side, "hash": "o", **extra}
    return {"event_type": "price_change", "market": M, "timestamp": str(ts), "price_changes": [item]}


def _gap_then_recover(reason="crossed_book"):
    """A held crossing group times out (gap resync, the recorder resubscribes), the exchange's own book
    recovers the asset at T0+1500, then a delta at T0+2000 is applied."""
    core = w.BookCore([A])
    out = w.Outcome()
    _merge(out, core.handle(_book(T0), TS, 0.0))
    _merge(out, core.handle(_pc(T0 + 1000, "0.53", "1"), TS, 1.0))
    _merge(out, core.flush_groups(TS, 1.0 + WS_GROUP_FLUSH_S))
    assert out.resyncs == [(A, reason)]
    _merge(out, core.handle(_book(T0 + 1500), TS, 1.3))
    _merge(out, core.handle(_pc(T0 + 2000, "0.49", "5"), TS, 1.4))
    assert core.all_live() and core.books[A].last_ms == T0 + 2000
    return core, out


def test_a_late_resubscribe_snapshot_is_dropped_not_a_second_resync():
    core, out = _gap_then_recover()
    late = core.handle(_book(T0 + 1800), TS, 1.8)
    assert late.resyncs == [] and late.counts["stale_resubscribe_snapshot"] == 1 and late.rows == []
    assert core.all_live() and core.books[A].last_ms == T0 + 2000 and 0.49 in core.books[A].bids


def test_only_one_late_snapshot_is_forgiven():
    core, _ = _gap_then_recover()
    core.handle(_book(T0 + 1800), TS, 1.8)
    again = core.handle(_book(T0 + 1900), TS, 1.9)
    assert again.resyncs == [(A, "timestamp_regression")]


def test_a_snapshot_older_than_the_recovery_book_is_still_a_regression():
    core, _ = _gap_then_recover()
    assert core.handle(_book(T0 + 1400), TS, 1.8).resyncs == [(A, "timestamp_regression")]


def test_a_late_snapshot_after_the_resync_timeout_is_still_a_regression():
    core, _ = _gap_then_recover()
    late = core.handle(_book(T0 + 1800), TS, 1.0 + WS_GROUP_FLUSH_S + WS_RESYNC_TIMEOUT_S + 0.01)
    assert late.resyncs == [(A, "timestamp_regression")]


def test_a_newer_snapshot_after_recovery_applies_and_ends_the_allowance():
    core, _ = _gap_then_recover()
    fresh = core.handle(_book(T0 + 2500), TS, 1.8)
    assert fresh.resyncs == [] and fresh.counts["snapshot_applied"] == 1
    assert core.handle(_book(T0 + 2400), TS, 1.9).resyncs == [(A, "timestamp_regression")]


def test_after_a_disconnect_an_older_snapshot_is_still_a_regression():
    core = w.BookCore([A])
    core.handle(_book(T0), TS, 0.0)
    core.invalidate_all("disconnect_closed", TS, 1.0)
    core.handle(_book(T0 + 1500), TS, 1.3)
    core.handle(_pc(T0 + 2000, "0.49", "5"), TS, 1.4)
    assert core.handle(_book(T0 + 1800), TS, 1.8).resyncs == [(A, "timestamp_regression")]


def test_without_a_gap_an_older_snapshot_is_a_regression_as_before():
    core = w.BookCore([A])
    core.handle(_book(T0 + 10), TS, 0.0)
    assert core.handle(_book(T0), TS, 0.1).resyncs == [(A, "timestamp_regression")]


# --- (B) the published top is the exchange's own -------------------------------------------------------


def test_rows_publish_the_reported_top_and_only_agreeing_groups():
    core = w.BookCore([A])
    core.handle(_book(T0), TS, 0.0)
    out = core.handle(_pc(T0 + 1000, "0.50", "3", best_bid="0.50", best_ask="0.52"), TS, 0.1)
    assert (out.rows[-1]["best_bid"], out.rows[-1]["best_ask"]) == (0.50, 0.52)
    held = core.handle(_pc(T0 + 2000, "0.51", "3", best_bid="0.50", best_ask="0.52"), TS, 0.2)
    assert held.rows == [] and held.resyncs == []  # level 0.51 above the reported 0.50: not published
    fixed = core.handle(_pc(T0 + 2000, "0.51", "0", best_bid="0.50", best_ask="0.52"), TS, 0.21)
    assert fixed.resyncs == [] and fixed.counts["group_resolved"] == 1


# --- the session loop closes due groups and resubscribes ---------------------------------------------------


class _Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class _Conn:
    def __init__(self, script, clock, sent):
        self.script, self.clock, self.sent, self.closed = list(script), clock, sent, False

    def send(self, text):
        self.sent.append(text)

    def recv(self, timeout):
        assert timeout >= 0
        if not self.script:
            self.clock.t += timeout
            return None
        step = self.script.pop(0)
        if step == "IDLE":
            self.clock.t += timeout
            return None
        self.clock.t += 0.001
        return json.dumps(step)

    def close(self):
        self.closed = True


class _Transport:
    def __init__(self, script, clock):
        self.script, self.clock, self.sent = script, clock, []

    def connect(self, url, timeout):
        return _Conn(self.script, self.clock, self.sent)


class _Sink:
    def __init__(self):
        self.rows = []

    def write(self, row):
        self.rows.append(row)


def test_the_session_loop_resyncs_a_group_left_open_and_resubscribes():
    clock = _Clock()
    transport = _Transport([_book(T0), _pc(T0 + 1000, "0.53", "1"), "IDLE"], clock)
    sink = _Sink()
    rec = w.Recorder([A], transport=transport, sink=sink, max_runtime_s=5.0, monotonic=clock.monotonic,
                     sleep=clock.sleep, now=lambda: TS)
    report = rec.run()
    assert report.counts["resync:crossed_book"] == 1 and report.counts["group_unresolved"] == 1
    assert [row["ws_state"] for row in sink.rows] == ["live", "resync"]
    frames = [json.loads(f) for f in transport.sent if f != "PING"]
    assert {"operation": "subscribe", "assets_ids": [A]} in frames[1:]


def test_the_session_loop_applies_a_group_closed_by_its_book():
    clock = _Clock()
    transport = _Transport([_book(T0), _pc(T0 + 1000, "0.53", "1"), _book(T0 + 1000, asks=(("0.54", "5"),))],
                           clock)
    sink = _Sink()
    rec = w.Recorder([A], transport=transport, sink=sink, max_runtime_s=5.0, monotonic=clock.monotonic,
                     sleep=clock.sleep, now=lambda: TS)
    report = rec.run()
    assert not any(key.startswith("resync:") for key in report.counts)
    assert report.counts["group_replaced_by_book"] == 1
    assert [row["ws_state"] for row in sink.rows] == ["live", "live"]
