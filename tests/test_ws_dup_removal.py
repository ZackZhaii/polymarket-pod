"""Task CM (A): duplicate same-timestamp level removals (M1 6h FAIL m1-20261010T1907Z,
/workspace/pod-bots/m1-6h-fail-analysis.md).

The exchange sends a match's level removal once per asset message (YES-first and NO-first items), so one removal
can arrive twice in a same-timestamp group. After Task CC holds the group, the first removal is applied and the
repeat used to raise unknown_level_removed. dup_removal_window.jsonl is 323 real frames from the box's public
capture of market 5234660 (two windows, each starting from both assets' snapshots): dev before Task CM resyncs
4 times on it, 69c8f55 resynced 4 times (top/crossed); fixed: 0. The repeat is skipped only at the SAME
timestamp; a level never held or a repeat at a later timestamp still resyncs. Offline and pure."""
from __future__ import annotations

import json
from pathlib import Path

import src.ws_recorder as w
from src.thresholds import WS_GROUP_FLUSH_S

FIX = Path(__file__).parent / "fixtures" / "ws_resync"
YES = "107540403103917101723616128435236138591116364556711257437238922612332295492509"
NO = "18368161459732947229970694377522451372979676356971809107906485726590890158916"
TS = "2030-01-01T00:00:00Z"
MKT = "0x334954cfe7d8ae24e350972470c156f5b2cdd4616dcaa5bfb71b4e2b2298d690"


def _replay(frames):
    core, counts, resyncs = w.BookCore([YES, NO]), {}, []
    for frame in frames:
        for out in (core.flush_groups(TS, frame["t"]), core.handle_text(frame["raw"], TS, frame["t"])):
            resyncs += out.resyncs
            for key, value in out.counts.items():
                counts[key] = counts.get(key, 0) + value
    out = core.flush_groups(TS, frames[-1]["t"] + WS_GROUP_FLUSH_S)
    return core, counts, resyncs + out.resyncs


def test_real_duplicate_removals_do_not_resync():
    frames = [json.loads(line) for line in (FIX / "dup_removal_window.jsonl").read_text().splitlines() if line.strip()]
    core, counts, resyncs = _replay(frames)
    assert resyncs == [] and core.all_live()
    assert counts.get("duplicate_level_removal") == 4


def _book(asset, ts, bids, asks):
    return json.dumps({"event_type": "book", "asset_id": asset, "market": MKT, "timestamp": str(ts),
                       "bids": [{"price": p, "size": s} for p, s in bids],
                       "asks": [{"price": p, "size": s} for p, s in asks]})


def _pc(ts, items):
    return json.dumps({"event_type": "price_change", "market": MKT, "timestamp": str(ts), "price_changes": [
        {"asset_id": YES, "side": side, "price": price, "size": size, "best_bid": bb, "best_ask": ba}
        for side, price, size, bb, ba in items]})


def _frames(second_ts):
    return [{"t": 1.0, "raw": _book(YES, 1000, [("0.50", "10"), ("0.49", "5")], [("0.52", "7")])},
            {"t": 1.1, "raw": _pc(1001, [("BUY", "0.50", "0", "0.49", "0.52")])},
            {"t": 1.2, "raw": _pc(second_ts, [("BUY", "0.50", "0", "0.49", "0.52")])}]


def test_same_ts_repeat_is_idempotent():
    core, counts, resyncs = _replay(_frames(1001))
    assert resyncs == [] and counts.get("duplicate_level_removal") == 1


def test_repeat_at_a_later_ts_still_resyncs():
    core, counts, resyncs = _replay(_frames(1002))
    assert resyncs == [(YES, "unknown_level_removed")]


def test_never_held_level_still_resyncs():
    frames = _frames(1001)[:1] + [{"t": 1.1, "raw": _pc(1001, [("BUY", "0.48", "0", "0.50", "0.52")])}]
    core, counts, resyncs = _replay(frames)
    assert resyncs == [(YES, "unknown_level_removed")]


def test_a_repeat_inside_one_frame_is_idempotent_too():
    frames = _frames(1001)[:1] + [{"t": 1.1, "raw": _pc(1001, [("BUY", "0.50", "0", "0.49", "0.52"),
                                                              ("BUY", "0.50", "0", "0.49", "0.52")])}]
    core, counts, resyncs = _replay(frames)
    assert resyncs == [] and counts.get("duplicate_level_removal") == 1


def test_the_same_price_on_the_other_side_is_not_a_repeat():
    # 0.50 was removed from the bids at ts 1001; removing an ask at 0.50 we never held still resyncs
    frames = _frames(1001)[:2] + [{"t": 1.2, "raw": _pc(1001, [("SELL", "0.50", "0", "0.49", "0.52")])}]
    core, counts, resyncs = _replay(frames)
    assert resyncs == [(YES, "unknown_level_removed")]


def test_a_repeat_after_a_resync_and_new_snapshot_still_resyncs():
    # the removed set is dropped with the book on a resync: a later snapshot at the same ts starts clean
    frames = _frames(1001)[:2] + [
        {"t": 1.2, "raw": _pc(1001, [("BUY", "0.30", "0", "0.49", "0.52")])},  # unknown -> resync
        {"t": 1.3, "raw": _book(YES, 1001, [("0.49", "5")], [("0.52", "7")])},
        {"t": 1.4, "raw": _pc(1001, [("BUY", "0.50", "0", "0.49", "0.52")])}]
    core, counts, resyncs = _replay(frames)
    assert resyncs == [(YES, "unknown_level_removed"), (YES, "unknown_level_removed")]
