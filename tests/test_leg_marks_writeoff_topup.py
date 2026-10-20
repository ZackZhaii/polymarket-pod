"""Issue #197 x #217 (C217): a written-off leg's close adds no closed_loss_topup (its write-off is realized)."""
from __future__ import annotations

from src import leg_marks


def _row(ts, side, shares, usd, **extra):
    row = {"fill_id": f"{ts}-{side}", "ts": ts, "market": "m", "token_id": "t", "side": side, "shares": shares,
           "notional_usd": usd, "counts_toward_risk": True, "hypothetical": False, "risk_decision": "allow"}
    row.update(extra)
    return row


SNAP = {"late": False, "legs": [{"leg_key": "m|t", "shares": 10.0, "cost_basis_usd": 5.0, "mark_usd": 9.0,
                                 "mark_invalid": False}]}


def test_a_losing_settlement_of_an_unwritten_leg_tops_up_to_the_start_of_day_mark():
    rows = [_row("2026-10-12T08:00:00Z", "buy", 10.0, 5.0),
            _row("2026-10-13T10:00:00Z", "sell", 10.0, 0.0, kind="settlement", risk_decision="settlement")]
    pos = leg_marks.positions(rows, "2026-10-13")
    assert leg_marks.closed_loss_topup(pos, SNAP) == 4.0  # max(5 cost loss, 9 mark - 0) - 5 already counted


def test_a_written_off_legs_settlement_adds_no_topup():
    rows = [_row("2026-10-12T08:00:00Z", "buy", 10.0, 5.0),
            _row("2026-10-13T09:00:00Z", "sell", 0.0, 0.0, kind="writeoff", risk_decision="writeoff",
                 writeoff_shares=10.0, writeoff_usd=9.0, writeoff_cost_usd=5.0, writeoff_sod_uplift_usd=4.0),
            _row("2026-10-13T10:00:00Z", "sell", 10.0, 0.0, kind="settlement", risk_decision="settlement")]
    pos = leg_marks.positions(rows, "2026-10-13")
    assert [c.written_off for c in pos.closed] == [True]
    assert leg_marks.closed_loss_topup(pos, SNAP) == 0.0  # the 9.0 write-off (cost + uplift) is the day's loss
