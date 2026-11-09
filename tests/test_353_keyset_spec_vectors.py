"""Issue #353: Poly研究's migration spec §3 (T1-T14), offline vectors for the /markets/keyset?id= reads.

Fixtures: tests/fixtures/gamma_keyset_spec_353 (copied from Poly研究's scratch/gamma-keyset-fixtures, 13 trimmed
fields, see MANIFEST.md). Every getter is a local fake that records its calls; nothing touches the network.
"""
from __future__ import annotations

import copy
import json
import pathlib
import socket
import urllib.error
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest

from src import fee_source, heartbeat, m2_runner, podlib, resolution_watch
from tests import m2_runner_rig as rig

FIX = pathlib.Path(__file__).parent / "fixtures" / "gamma_keyset_spec_353"
OPEN_IDS = ["562831", "601819", "601826", "562830", "2589812"]
CLOSED_ID = "6006152"
NOW = datetime(2026, 10, 22, 0, 0, tzinfo=timezone.utc)
STAMP = "2026-10-22T00:00:00.000000Z"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def raw(name: str) -> dict:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def rows(market_id: str) -> list[dict]:
    return copy.deepcopy(raw(f"id-{market_id}-keyset.json")["rows"])


def keyset_body(fixture: dict, rows_: list[dict]) -> dict:
    """The live answer shape the fixture recorded: its top_keys, $schema ignored by the parser."""
    body: dict = {"$schema": "https://gamma-api.polymarket.com/schemas/MarketsKeyset.json", "markets": rows_}
    if fixture.get("next_cursor") not in (None, "<absent>"):
        body["next_cursor"] = fixture["next_cursor"]
    assert sorted(body) == sorted(fixture["top_keys"])
    return body


def keyset(market_id: str) -> dict:
    return keyset_body(raw(f"id-{market_id}-keyset.json"), rows(market_id))


def legacy(market_id: str) -> list[dict]:
    return copy.deepcopy(raw(f"id-{market_id}-legacy.json")["rows"])


def empty_markets(name: str) -> dict:
    fixture = raw(name)
    assert fixture["ids"] == [] and fixture["n"] == 0
    return keyset_body(fixture, [])


class Getter:
    """Serves one body per (id, closed) view; counts calls and records every URL."""

    def __init__(self, views: dict | None = None, *, error: Exception | None = None, default=None):
        self.views, self.error, self.urls = dict(views or {}), error, []
        self.default = {"markets": []} if default is None else default

    def __call__(self, url: str):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        q = parse_qs(urlsplit(url).query)
        return copy.deepcopy(self.views.get(((q.get("id") or [""])[0], q.get("closed") == ["true"]), self.default))

    @property
    def calls(self) -> int:
        return len(self.urls)


def fee_row(getter, market_id):  # S1
    return m2_runner._gamma_market(getter, market_id)


def both_views(market_id: str, body) -> Getter:
    return Getter({(market_id, True): body, (market_id, False): body})


# T1 / T2: the fee from the keyset row
@pytest.mark.parametrize("market_id,closed,rate", [("562831", False, 0.0), (CLOSED_ID, True, 0.05)])
def test_t1_t2_fee_from_keyset_row(market_id, closed, rate):
    getter = Getter({(market_id, False): keyset(market_id)})
    row = fee_row(getter, market_id)
    assert row is not None and row["id"] == market_id and row["closed"] is closed
    assert fee_source.parse_gamma_fee(row) == (rate, None)
    assert fee_source.resolve_fee(market_id, row).fee_rate == rate
    assert getter.calls == 1


# T3: legacy and keyset rows parse to the same fee and schedule sha
@pytest.mark.parametrize("market_id", OPEN_IDS + [CLOSED_ID])
def test_t3_legacy_and_keyset_rows_agree(market_id):
    (old,), (new,) = legacy(market_id), rows(market_id)
    assert fee_source.parse_gamma_fee(old) == fee_source.parse_gamma_fee(new)
    assert fee_source.schedule_sha(old) == fee_source.schedule_sha(new) is not None


# T4: open markets, both views return the row
@pytest.mark.parametrize("market_id", OPEN_IDS)
def test_t4_open_market_both_views(market_id, tmp_path):
    getter = both_views(market_id, keyset(market_id))
    row, failure = resolution_watch.read_market(getter, market_id)
    assert failure is None and row["id"] == market_id and row["closed"] is False
    collected = resolution_watch.collect(both_views(market_id, keyset(market_id)), [market_id], NOW, STAMP,
                                         tmp_path / "resolutions.jsonl")
    assert collected.ok and collected.closed_markets == [] and collected.written == []


# T5: settled market: closed view has the row, the default view is empty (probe-closed_wrong_view)
def test_t5_closed_market_closed_view_only(tmp_path):
    views = {(CLOSED_ID, True): keyset(CLOSED_ID), (CLOSED_ID, False): empty_markets("probe-closed_wrong_view.json")}
    row, failure = resolution_watch.read_market(Getter(views), CLOSED_ID)
    assert failure is None and row["closed"] is True and row["umaResolutionStatus"] == "resolved"
    collected = resolution_watch.collect(Getter(views), [CLOSED_ID], NOW, STAMP, tmp_path / "resolutions.jsonl")
    assert collected.ok and collected.closed_markets == [CLOSED_ID]  # handed to resolutions.snapshot


# T6: unknown id (probe-missing)
def test_t6_missing_id():
    body = empty_markets("probe-missing.json")
    assert fee_row(Getter(default=body), "999999999") is None
    assert resolution_watch.read_market(Getter(default=body), "999999999") == (None, "gamma_market_missing")


# T7: empty / non-numeric id: no request at all
@pytest.mark.parametrize("market_id", ["", "   ", "m2-alpha", "5628a1", "-1", "1.5", "５６２", None])
def test_t7_bad_id_sends_no_request(market_id):
    fee_getter, watch_getter = Getter(default=keyset("562831")), Getter(default=keyset("562831"))
    assert fee_row(fee_getter, market_id) is None
    assert resolution_watch.read_market(watch_getter, market_id) == (None, "gamma_bad_id")
    assert fee_getter.calls == 0 and watch_getter.calls == 0


def test_t7_probe_empty_shape_is_never_requested():
    probe = raw("probe-empty.json")
    assert probe["endpoint"].endswith("?id=") and probe["n"] == 20 and probe["next_cursor"] != "<absent>"
    getter = Getter(default={"markets": [], "next_cursor": probe["next_cursor"]})
    assert fee_row(getter, "") is None and resolution_watch.read_market(getter, "")[1] == "gamma_bad_id"
    assert getter.calls == 0


# T8: a single-id answer carrying next_cursor (the id filter was ignored)
@pytest.mark.parametrize("cursor", [raw("probe-empty.json")["next_cursor"], "", None])
def test_t8_next_cursor_is_bad_response(cursor):
    body = dict(keyset("562831"), next_cursor=cursor)
    getter = both_views("562831", body)
    assert fee_row(getter, "562831") is None
    assert resolution_watch.read_market(both_views("562831", body), "562831") == (None, "gamma_bad_response")
    assert all("after_cursor" not in u for u in getter.urls)  # never paged


# T9: the legacy bare-list shape is refused
def test_t9_legacy_array_is_bad_response():
    body = legacy("562831")
    assert fee_row(both_views("562831", body), "562831") is None
    assert resolution_watch.read_market(both_views("562831", body), "562831") == (None, "gamma_bad_response")


# T10: a single-id request answered with three rows (probe-multi ids)
def test_t10_non_matching_rows_are_bad_response():
    ids = raw("probe-multi.json")["ids"]
    assert ids[0] == "562831" and len(ids) == 3
    body = {"markets": [r for i in ids for r in rows(i)]}
    assert fee_row(both_views("562831", body), "562831") is None
    assert resolution_watch.read_market(both_views("562831", body), "562831") == (None, "gamma_bad_response")


@pytest.mark.parametrize("body", [None, {}, {"markets": None}, {"markets": {"id": "562831"}}, "x",
                                  {"markets": ["562831"]}])
def test_t9b_other_bad_shapes(body):
    assert fee_row(both_views("562831", body), "562831") is None
    assert resolution_watch.read_market(both_views("562831", body), "562831") == (None, "gamma_bad_response")


# T11: a failed read
@pytest.mark.parametrize("error", [urllib.error.HTTPError("u", code, "x", {}, None)
                                  for code in (404, 410, 503)] + [TimeoutError("t")])
def test_t11_read_failure(error):
    cache = fee_source.FeeCache(lambda mid: fee_row(Getter(error=error), mid), clock=lambda: 0.0)
    assert cache.fee_rate("562831") is None
    assert cache.get("562831").problem == fee_source.GAMMA_UNREADABLE
    failure = resolution_watch.read_market(Getter(error=error), "562831")
    assert failure == (None, f"gamma_read_failed:{type(error).__name__}")


# T12: the URLs: /markets/keyset, only id (+ closed=true), allowlisted, never the old /markets?id=
def test_t12_urls():
    fee_url = m2_runner.gamma_market_url("562831")
    closed_url, open_url = (resolution_watch.gamma_url("562831", closed=c) for c in (True, False))
    for url, query in ((fee_url, {"id": ["562831"]}), (open_url, {"id": ["562831"]}),
                       (closed_url, {"id": ["562831"], "closed": ["true"]})):
        parts = urlsplit(url)
        assert (parts.scheme, parts.hostname, parts.path) == ("https", "gamma-api.polymarket.com", "/markets/keyset")
        assert parse_qs(parts.query) == query and "version" not in url
        assert m2_runner.url_allowed(url) and url.startswith(podlib.PUBLIC_PREFIXES)
        assert "/markets?" not in url
    getter = both_views("562831", keyset("562831"))
    resolution_watch.read_market(getter, "562831")
    fee_row(getter, "562831")
    assert getter.calls == 3 and all(urlsplit(u).path == "/markets/keyset" for u in getter.urls)


def test_t12_no_legacy_fallback_when_keyset_fails():
    getter = Getter(default=legacy("562831"))
    assert fee_row(getter, "562831") is None
    assert resolution_watch.read_market(getter, "562831") == (None, "gamma_bad_response")
    assert all(urlsplit(u).path == "/markets/keyset" for u in getter.urls)


# T13: two rows of the same id
def test_t13_duplicate():
    body = {"markets": rows("562831") * 2}
    assert resolution_watch.read_market(both_views("562831", body), "562831") == (None, "gamma_duplicate_market")
    assert fee_row(both_views("562831", body), "562831") is None


# T14: the two views disagree on closed
def test_t14_views_disagree():
    closed_row = dict(rows("562831")[0], closed=True)
    views = {("562831", True): {"markets": [closed_row]}, ("562831", False): keyset("562831")}
    assert resolution_watch.read_market(Getter(views), "562831") == (None, "gamma_views_disagree")


def test_valid_id_strip():
    assert resolution_watch.valid_gamma_id(" 562831 ") and not resolution_watch.valid_gamma_id("")


@pytest.mark.parametrize("failure", [404, 410, 503, "timeout", "empty"])
def test_runner_fee_read_failure_skips_without_crash(tmp_path, monkeypatch, failure):
    """A healthy scan plus one failed ID lookup traverses the real runner, using only owned fake I/O."""
    now = datetime.now(timezone.utc)
    paths = rig.isolate(monkeypatch, tmp_path)
    assert all(path.is_relative_to(tmp_path) for path in paths.values())
    assert heartbeat.HEARTBEAT_DIR.is_relative_to(tmp_path.parent)  # shared conftest supplies heartbeat isolation
    fake = rig.FakeGetters(now, gamma_row_changes={
        "feesEnabled": True, "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True}})
    normal_gamma, id_urls = fake.gamma, []

    def gamma(url):
        if "id" not in parse_qs(urlsplit(url).query):
            return normal_gamma(url)
        fake.gamma_urls.append(url)
        id_urls.append(url)
        if isinstance(failure, int):
            raise urllib.error.HTTPError(url, failure, "offline fixture", {}, None)
        if failure == "timeout":
            raise TimeoutError("offline fixture")
        return {"markets": []}

    fake.gamma = gamma
    rig.deliver(paths["run"], now)
    assert rig.run_once(paths["run"], fake) == m2_runner.EXIT_OK
    steps = rig.read_jsonl(paths["run"] / "journal.jsonl")
    assert steps[-1]["step"] == "skipped"
    assert steps[-1]["reasons"] == ["missing_fee_data", fee_source.GAMMA_UNREADABLE]
    assert fake.book_urls == [] and rig.read_jsonl(paths["paper"]) == []
    record, problem = heartbeat.read_beat(heartbeat.beat_path(m2_runner.PROC), m2_runner.PROC)
    assert problem is None and record["state"] != heartbeat.STATE_CRASHED
    assert record["progress"]["missing_fee_total"] == 1
    assert len(id_urls) == 1 and urlsplit(id_urls[0]).path == "/markets/keyset"
