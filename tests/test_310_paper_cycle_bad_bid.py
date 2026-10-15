"""Codex P2 on PR #310: a huge best_bid (float() OverflowError) crashed paper_cycle. Out-of-range or non-finite book
prices fail closed: huge skips the market as a bad book; non-finite / out-of-range is left out (no buy). No crash, no fill. Offline: tmp_path only."""
from __future__ import annotations


import pytest

import src.paper_cycle as paper_cycle
from tests.test_237b_sell_set_reject_reason import _run

BAD = [10 ** 400, float("inf"), float("-inf"), float("nan"), 1.5, 1.0, 0.0, -0.1, True, "0.4"]


@pytest.mark.parametrize("bad", BAD, ids=[f"bad{i}" for i in range(len(BAD))])
def test_bad_best_bid_never_crashes_or_buys(tmp_path, bad):
    result, messages = _run(tmp_path, {"bad": [("y", bad, 0.40), ("n", 0.45, 0.45)],  # asks 0.85: a buy gap if ok
                                       "ok": [("y2", 0.40, 0.42), ("n2", 0.55, 0.60)]})
    assert result["ok"] is True and result["fills"] == 0  # no crash, no buy
    if type(bad) is int:  # the huge bid skips the whole market as invalid_book_price: no gap at all
        assert result["gaps"] == 0
    # other bad bids are left out as before; the ask-side gap is then structurally rejected, never filled
    assert not [m for m in messages if m["market"] == "bad" and m["payload"].get("reject_reason") is None
                and m["type"] != "signal"]


@pytest.mark.parametrize("bad", [10 ** 400, float("nan"), 2.0], ids=["huge", "nan", "two"])
def test_bad_best_ask_skips_market_without_crash(tmp_path, bad):
    result, _ = _run(tmp_path, {"bad": [("y", 0.40, bad), ("n", 0.45, 0.45)]})
    assert result["ok"] is True and result["fills"] == 0 and result["gaps"] == 0


def test_unit_price_helper():
    assert paper_cycle._unit_price(None) == (None, False)
    assert paper_cycle._unit_price(0.4) == (0.4, False)
    assert paper_cycle._unit_price(10 ** 400) == (None, True)
    for dropped in (float("inf"), float("-inf"), float("nan"), 1.5, 1.0, 0.0, -0.1, True, "0.4"):
        assert paper_cycle._unit_price(dropped) == (None, False)
