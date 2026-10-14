"""Issue #206 (Task CO) gaps: the failure branches of src/resolution_watch.py.

``collect`` is the tick's settlement collector. Its ruling (Poly风控 on #197, issuecomment-6106669720 ②) is
fail closed on EVERY branch: an exception or HTTP error, a response that is not a list, a market that is in
neither view or twice in one view, a ``closed`` / ``acceptingOrders`` that is not a JSON boolean, two views
that disagree, an unreadable history or a failed append all mean *nothing is written this tick* — no partial
settlement, and the next tick retries. ``read_market`` alone had one line of coverage
(tests/test_runner_settlement.py, the allowlisted /markets path); the reasons and the "write nothing" rule
were untested. Offline: fake Gamma getters, every file under tmp_path.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from src import resolutions, resolution_watch
from src.thresholds import RESOLUTION_HOLD_HOURS
from tests import m2_runner_rig as rig

NOW = datetime.now(timezone.utc).replace(microsecond=0)
STAMP = rig.stamp(NOW)
MARKET = rig.MARKET_ID


def closed_row(hours_ago: float = 72.0, **changes) -> dict:
    """A resolved, closed Gamma row: unique 1/0 winner, UMA resolved, closed well past the hold."""
    closed = NOW - timedelta(hours=hours_ago)
    base = dict(closed=True, acceptingOrders=False, umaResolutionStatus="resolved",
                outcomePrices='["1", "0"]', closedTime=closed.strftime("%Y-%m-%d %H:%M:%S+00"))
    base.update(changes)
    return rig.gamma_row(NOW, **base)


def open_row(**changes) -> dict:
    return rig.gamma_row(NOW, **dict(closed=False, acceptingOrders=True, **changes))


class Gamma:
    """The two ``/markets?id=`` views of each held market, keyed by (market_id, closed view).

    A key that was not set answers ``[]`` (the market is not in that list), so a test says only what it
    means. Every URL is recorded; nothing here touches the network.
    """

    def __init__(self, responses: dict | None = None, *, error: Exception | None = None) -> None:
        self.responses = dict(responses or {})
        self.error = error
        self.urls: list[str] = []

    def __call__(self, url: str):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        query = parse_qs(urlsplit(url).query)
        market_id = (query.get("id") or [""])[0]
        want_closed = query.get("closed") == ["true"]
        return self.responses.get((market_id, want_closed), [])

    @property
    def reads(self) -> int:
        return len(self.urls)


def both_views(row: dict, market: str = MARKET) -> dict:
    return {(market, True): [row], (market, False): [row]}


def collect_path(tmp_path):
    return tmp_path / "data" / "resolutions.jsonl"


# ------------------------------------------------------------------ read_market: a bad read is a failure, never a row

def test_read_market_treats_a_raised_read_as_a_failure_and_reads_no_second_view():
    getter = Gamma(error=ConnectionError("no route to host"))
    row, failure = resolution_watch.read_market(getter, MARKET)
    assert (row, failure) == (None, "gamma_read_failed:ConnectionError")
    assert getter.reads == 1  # the closed view is read first; a failed read is not retried this tick


@pytest.mark.parametrize("response", [{"markets": []}, None])
def test_read_market_rejects_a_response_that_is_not_a_list(response):
    getter = Gamma({(MARKET, True): response})
    row, failure = resolution_watch.read_market(getter, MARKET)
    assert (row, failure) == (None, "gamma_bad_response")
    assert getter.reads == 1


def test_read_market_needs_exactly_one_matching_row_in_a_view():
    """A view that yields no matching row (a market in neither list) or the market twice fails closed."""
    missing = Gamma()  # both views answer []
    assert resolution_watch.read_market(missing, MARKET) == (None, "gamma_market_missing")

    twice = Gamma({(MARKET, True): [closed_row(), closed_row()]})
    assert resolution_watch.read_market(twice, MARKET) == (None, "gamma_duplicate_market")

    # a row for another market never counts as this market's row
    foreign = Gamma({(MARKET, True): [rig.gamma_row(NOW, id="m2-other")]})
    assert resolution_watch.read_market(foreign, MARKET) == (None, "gamma_market_missing")


@pytest.mark.parametrize("field,value,reason", [
    ("closed", "true", "gamma_non_bool_closed"),
    ("acceptingOrders", "false", "gamma_non_bool_acceptingOrders"),
])
def test_read_market_rejects_a_non_bool_closed_or_accepting_orders(field, value, reason):
    """``closed`` / ``acceptingOrders`` must be JSON booleans: a string or an int is not guessed into a bool."""
    getter = Gamma({(MARKET, True): [closed_row(**{field: value})]})
    assert resolution_watch.read_market(getter, MARKET) == (None, reason)


def test_read_market_rejects_two_views_that_disagree_on_closed():
    """A market Gamma calls closed in one view and open in the other is not settled from."""
    disagree = Gamma({(MARKET, True): [closed_row()], (MARKET, False): [open_row()]})
    assert resolution_watch.read_market(disagree, MARKET) == (None, "gamma_views_disagree")


def test_read_market_returns_a_row_both_views_agree_is_open_and_collect_writes_nothing(tmp_path, monkeypatch):
    """Both views agree the market is open: a usable row, and no settlement snapshot and no history read."""
    getter = Gamma(both_views(open_row()))
    row, failure = resolution_watch.read_market(getter, MARKET)
    assert failure is None and row["closed"] is False

    def boom(*args, **kwargs):
        raise AssertionError("read_history must not be called when nothing is closed")

    monkeypatch.setattr(resolutions, "read_history", boom)
    path = collect_path(tmp_path)
    collected = resolution_watch.collect(Gamma(both_views(open_row())), [MARKET], NOW, STAMP, path)
    assert collected.ok and collected.closed_markets == [] and collected.written == []
    assert not path.exists()  # not an empty file either


# ------------------------------------------------------------------ collect: any failure means nothing is written

def test_collect_writes_nothing_when_one_held_market_fails(tmp_path):
    """One unreadable market must not let a sibling's row through: fail closed for the whole tick."""
    getter = Gamma({(MARKET, True): [closed_row()], (MARKET, False): [closed_row()],
                    ("m2-broken", True): {"markets": []}})
    path = collect_path(tmp_path)
    collected = resolution_watch.collect(getter, [MARKET, "m2-broken"], NOW, STAMP, path)
    assert not collected.ok
    assert collected.failures == ["m2-broken:gamma_bad_response"]
    assert collected.closed_markets == [MARKET]  # the good market was read and understood ...
    assert collected.written == [] and collected.skip_reasons == {}  # ... and still nothing was written
    assert not path.exists()


def test_collect_reports_an_unreadable_history_and_writes_nothing(tmp_path):
    """A torn / foreign history line is a failure, not a row to append next to (returned by name)."""
    path = collect_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    torn = b'{"condition_id": "0xdead"}'
    path.write_bytes(torn)

    collected = resolution_watch.collect(Gamma(both_views(closed_row())), [MARKET], NOW, STAMP, path)
    assert collected.failures == ["resolutions_history_unreadable:HistoryError"]
    assert collected.written == [] and not collected.ok
    assert path.read_bytes() == torn  # the unusable history is left exactly as it was


def test_collect_reports_a_failed_append_and_returns_no_rows(tmp_path, monkeypatch):
    """A failed write is reported by name: the row is not counted as collected."""
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(resolutions, "append_jsonl", boom)
    path = collect_path(tmp_path)
    collected = resolution_watch.collect(Gamma(both_views(closed_row())), [MARKET], NOW, STAMP, path)
    assert collected.failures == ["resolutions_write_failed:OSError"]
    assert collected.written == [] and not collected.ok


def test_collect_writes_at_the_hold_boundary_and_skips_a_market_inside_it(tmp_path):
    """RESOLUTION_HOLD_HOURS is inclusive (elapsed == hold passes) and fail closed one second inside it."""
    path = collect_path(tmp_path)
    getter = Gamma({
        (MARKET, True): [closed_row(RESOLUTION_HOLD_HOURS)],
        (MARKET, False): [closed_row(RESOLUTION_HOLD_HOURS)],
        ("m2-young", True): [closed_row(RESOLUTION_HOLD_HOURS - 0.01, id="m2-young", conditionId="0xm2young")],
        ("m2-young", False): [closed_row(RESOLUTION_HOLD_HOURS - 0.01, id="m2-young", conditionId="0xm2young")],
    })
    collected = resolution_watch.collect(getter, [MARKET, "m2-young"], NOW, STAMP, path)
    assert collected.ok, collected.failures
    assert collected.skip_reasons == {"hold_pending": 1}
    assert [row["condition_id"] for row in collected.written] == ["0xm2alpha"]
    stored = resolutions.read_history(path)
    assert [row["condition_id"] for row in stored] == ["0xm2alpha"]
    assert stored[0]["status"] == resolutions.STATUS_RESOLVED and stored[0]["uma_checked"] is True
    assert stored[0]["closed_ts"] == (NOW - timedelta(hours=RESOLUTION_HOLD_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
