"""Issue #353: single-id lookups use the reviewed strict /markets/keyset?id=<id> contract.

Fixtures are Poly研究's trimmed keyset samples (tests/fixtures/gamma_keyset_353, see #353). No network:
every getter here is a local fake. Every malformed answer fails closed (no fee row / a named failure).
"""
from __future__ import annotations

import copy
import json
import pathlib
import socket
import urllib.error
from urllib.parse import parse_qs, urlsplit

import pytest

from src import fee_source, m2_runner, podlib, resolution_watch

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("network attempted")
    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


FIX = pathlib.Path(__file__).parent / "fixtures" / "gamma_keyset_353"


def fx(name: str):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class Getter:
    """Serves one fixture per (id, closed) view; records every URL."""

    def __init__(self, views: dict | None = None, *, error: Exception | None = None, default=None):
        self.views, self.error, self.urls = dict(views or {}), error, []
        self.default = {"markets": []} if default is None else default

    def __call__(self, url: str):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        q = parse_qs(urlsplit(url).query)
        return copy.deepcopy(self.views.get(((q.get("id") or [""])[0], q.get("closed") == ["true"]), self.default))


def _query(url: str) -> dict:
    parts = urlsplit(url)
    assert parts.scheme + "://" + parts.netloc + parts.path == podlib.GAMMA_PREFIX + "/keyset"
    return parse_qs(parts.query)


# ------------------------------------------------------------------ URLs

def test_gamma_market_url_uses_keyset_id_without_closed():
    url = m2_runner.gamma_market_url("559651")
    assert _query(url) == {"id": ["559651"]}
    assert m2_runner.url_allowed(url)


@pytest.mark.parametrize("closed,expected", [(True, {"id": ["5644218"], "closed": ["true"]}),
                                             (False, {"id": ["5644218"]})])
def test_resolution_gamma_url_uses_keyset_and_closed_true(closed, expected):
    url = resolution_watch.gamma_url("5644218", closed=closed)
    assert _query(url) == expected and m2_runner.url_allowed(url)


def test_no_legacy_id_query_in_src():
    src = pathlib.Path(__file__).resolve().parents[1] / "src"
    for path in src.glob("*.py"):
        assert 'GAMMA_PREFIX + "?"' not in path.read_text(encoding="utf-8"), path.name


# ------------------------------------------------------------------ m2_runner._gamma_market (fee row)

def test_gamma_market_keyset_open_row_and_fee():
    getter = Getter({("559651", False): fx("keyset_open_559651.json")})
    row = m2_runner._gamma_market(getter, "559651")
    assert row["id"] == "559651" and fee_source.parse_gamma_fee(row) == (0.04, None)
    assert len(getter.urls) == 1


@pytest.mark.parametrize("response", [
    fx("legacy_array_5644218.json"),            # the old bare-list shape is no longer accepted
    fx("synthetic_empty_markets.json"),          # zero rows
    fx("synthetic_duplicate_559651.json"),       # two rows with the same id
    fx("keyset_ignored_param_ids.json"),         # id filter ignored: unrelated rows + next_cursor
    fx("synthetic_error_object.json"),           # {"error": ...}, no markets key
    {"markets": None}, {"markets": {"id": "559651"}}, {"markets": "559651"}, None, "x", 3,
    {"markets": fx("keyset_open_559651.json")["markets"], "next_cursor": "MTAw"},
])
def test_gamma_market_malformed_or_ambiguous_is_no_row(response):
    market_id = "5644218" if response == fx("legacy_array_5644218.json") else "559651"
    getter = Getter()
    getter.default = response
    assert m2_runner._gamma_market(getter, market_id) is None
    assert len(getter.urls) == 1  # never pages


def test_gamma_market_id_mismatch_is_no_row():
    getter = Getter(default=fx("keyset_open_559651.json"))
    assert m2_runner._gamma_market(getter, "1") is None


@pytest.mark.parametrize("error", [
    podlib.PublicGetError("https://gamma-api.polymarket.com/markets/keyset", 3, OSError("HTTP 503")),
    urllib.error.HTTPError("https://gamma-api.polymarket.com/markets/keyset", 410, "Gone", {}, None),
    json.JSONDecodeError("Expecting value", (FIX / "synthetic_non_json.txt").read_text(encoding="utf-8"), 0),
])
def test_gamma_market_read_error_is_unreadable_fee(error):
    cache = fee_source.FeeCache(lambda mid: m2_runner._gamma_market(Getter(error=error), mid))
    quote = cache.get("559651")
    assert quote.fee_rate is None and quote.problem == fee_source.GAMMA_UNREADABLE


def test_fee_cache_empty_markets_is_unreadable_fee():
    getter = Getter(default=fx("synthetic_empty_markets.json"))
    quote = fee_source.FeeCache(lambda mid: m2_runner._gamma_market(getter, mid)).get("559651")
    assert quote.fee_rate is None and quote.problem == fee_source.GAMMA_UNREADABLE


# ------------------------------------------------------------------ resolution_watch.read_market

def test_read_market_keyset_closed_market():
    getter = Getter({("5644218", True): fx("keyset_closed_5644218.json"),
                     ("5644218", False): fx("keyset_closed_5644218_default_empty.json")})
    row, failure = resolution_watch.read_market(getter, "5644218")
    assert failure is None and row["closed"] is True and row["umaResolutionStatus"] == "resolved"
    assert [_query(u).get("closed") for u in getter.urls] == [["true"], None]


def test_read_market_keyset_open_market():
    getter = Getter({("559651", True): fx("keyset_open_559651_closedtrue_empty.json"),
                     ("559651", False): fx("keyset_open_559651.json")})
    row, failure = resolution_watch.read_market(getter, "559651")
    assert failure is None and row["closed"] is False


def test_read_market_keeps_disputed_history():
    getter = Getter({("5518151", True): fx("keyset_disputed_5518151.json")})
    row, failure = resolution_watch.read_market(getter, "5518151")
    assert failure is None
    assert row["umaResolutionStatuses"] == fx("keyset_disputed_5518151.json")["markets"][0]["umaResolutionStatuses"]


@pytest.mark.parametrize("response", [
    fx("legacy_array_5644218.json"), fx("keyset_ignored_param_ids.json"), fx("synthetic_error_object.json"),
    {"markets": None}, {"markets": {"id": "5644218"}}, None, "x",
])
def test_read_market_malformed_is_bad_response(response):
    getter = Getter()
    getter.default = response
    assert resolution_watch.read_market(getter, "5644218") == (None, "gamma_bad_response")
    assert len(getter.urls) == 1


def test_read_market_both_views_empty_is_missing():
    getter = Getter({("5644218", True): fx("keyset_open_559651_closedtrue_empty.json"),
                     ("5644218", False): fx("keyset_closed_5644218_default_empty.json")})
    assert resolution_watch.read_market(getter, "5644218") == (None, "gamma_market_missing")


def test_read_market_id_mismatch_is_bad_response():
    """#353 spec 2.3 (Poly研究 0cb7188): a single-id answer holding another id's row is a bad response."""
    getter = Getter(default=fx("keyset_closed_5644218.json"))
    assert resolution_watch.read_market(getter, "1") == (None, "gamma_bad_response")


def test_read_market_duplicate_rows():
    getter = Getter({("559651", True): fx("synthetic_duplicate_559651.json")})
    assert resolution_watch.read_market(getter, "559651") == (None, "gamma_duplicate_market")


def test_read_market_views_disagree():
    getter = Getter({("559651", True): fx("synthetic_views_disagree_559651_closed.json"),
                     ("559651", False): fx("keyset_open_559651.json")})
    assert resolution_watch.read_market(getter, "559651") == (None, "gamma_views_disagree")


def test_read_market_non_bool_closed():
    getter = Getter({("5644218", True): fx("synthetic_non_bool_closed_5644218.json")})
    assert resolution_watch.read_market(getter, "5644218") == (None, "gamma_non_bool_closed")


@pytest.mark.parametrize("error,name", [
    (podlib.PublicGetError("https://gamma-api.polymarket.com/markets/keyset", 3, OSError("HTTP 429")), "PublicGetError"),
    (urllib.error.HTTPError("https://gamma-api.polymarket.com/markets/keyset", 404, "Not Found", {}, None),
     "HTTPError"),
    (json.JSONDecodeError("Expecting value", "<html>502</html>", 0), "JSONDecodeError"),
])
def test_read_market_read_error_is_named_failure(error, name):
    assert resolution_watch.read_market(Getter(error=error), "5644218") == (None, f"gamma_read_failed:{name}")
