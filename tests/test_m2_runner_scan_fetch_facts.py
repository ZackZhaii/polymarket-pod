"""EU: existing scan events retain same-return metadata, never coverage certification."""
from __future__ import annotations

import copy
import socket
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from src import ledger_store, m2_runner, podlib, scanner
from tests import m2_runner_rig as rig


KEYS = ("pages", "fetched", "stop_reason", "complete", "cap_hit", "max_markets",
        "server_min_liquidity", "base_url")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        pytest.fail("EU offline fixture attempted network access")

    monkeypatch.setattr(socket.socket, "connect", refused)
    monkeypatch.setattr(socket, "create_connection", refused)
    monkeypatch.setattr(socket, "getaddrinfo", refused)
    monkeypatch.setattr(podlib, "urlopen", refused)


@pytest.fixture
def case(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    now = datetime.now(timezone.utc) - timedelta(seconds=1)

    def no_extra_clock():
        pytest.fail("_scan must use the supplied now, not call state.clock")

    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(paths["run"]),
                                  clock=no_extra_clock, monotonic=lambda: 1000.0)
    previous = {"old": {"id": "old", "marker": "prior snapshot"}}
    state.markets, state.scan_at = previous, now - timedelta(hours=1)
    state.counters["scans_total"], state.counters["scan_failures_total"] = 7, 2
    return SimpleNamespace(paths=paths, now=now, state=state, previous=previous, old_scan_at=state.scan_at,
                           counters=copy.deepcopy(state.counters))


def pager(case, pages):
    urls = []

    def get(url):
        urls.append(url)
        cursor = parse_qs(urlsplit(url).query).get("after_cursor", [""])[0]
        value = pages[cursor]
        if value == "network_error":
            raise podlib.PublicGetError(url, 3, TimeoutError("owned page failure"))
        batch, nxt = value
        return {"markets": batch, "next_cursor": nxt}

    return get, urls


def run_real_scan(case, monkeypatch, getter, *, raw=None, bounds=None):
    real_scan, real_fetch = scanner.scan, scanner.fetch_all_markets
    real_candidates, real_event = m2_runner.forecast_inbox.write_candidates, m2_runner.event
    result, snapshots, order, after = [], [], [], []
    if bounds:
        def bounded(*args, **kwargs):
            options = {**kwargs, **bounds}
            return real_fetch(*args, **options)
        monkeypatch.setattr(scanner, "fetch_all_markets", bounded)

    def scan(**kwargs):
        options = {**kwargs, "ts": case.now.isoformat(timespec="microseconds")}
        if raw is not None:
            options["raw_markets"] = raw
        value = real_scan(**options)
        result.append(value)
        snapshots.append(copy.deepcopy(value))
        return value

    def candidates(rows, path):
        order.append("candidates")
        return real_candidates(rows, path)

    def event(state, kind, now, **fields):
        order.append(kind)
        return real_event(state, kind, now, **fields)

    monkeypatch.setattr(scanner, "scan", scan)
    monkeypatch.setattr(m2_runner.forecast_inbox, "write_candidates", candidates)
    monkeypatch.setattr(m2_runner, "event", event)
    observed = SimpleNamespace(results=result, snapshots=snapshots, order=order, after=after)
    case.observed = observed
    m2_runner._scan(case.state, case.now, m2_runner.GuardedGetter(getter, lambda: after.append("after")))
    assert result == snapshots, "observer mutated the scanner return"
    return observed


def events(case):
    return rig.read_jsonl(case.state.paths.events)


def test_real_multipage_duplicate_fetch_facts_are_retained_on_existing_scanned(case, monkeypatch):
    first = rig.gamma_row(case.now)
    duplicate = rig.gamma_row(case.now, question="ignored later duplicate")
    second = rig.gamma_row(case.now, id="900002")
    get, urls = pager(case, {"": ([first], "c1"), "c1": ([duplicate, second], "")})
    observed = run_real_scan(case, monkeypatch, get)
    row = events(case)[0]
    expected = {key: observed.results[0]["fetch"][key] for key in KEYS}
    assert row["fetch"] == expected
    assert tuple(row["fetch"]) == KEYS
    assert (row["fetch"]["pages"], row["fetch"]["fetched"], row["scanned"], row["passed"]) == (2, 2, 2, 2)
    assert row["fetch"]["stop_reason"] == "exhausted" and row["fetch"]["complete"] is True
    assert len(events(case)) == 1 and set(row) == {"ts", "event", "scanned", "passed", "fetch"}
    assert row["event"] == "scanned" and row["ts"] == m2_runner._stamp(case.now)
    assert observed.order == ["candidates", "scanned"] and len(urls) == len(observed.after) == 2
    assert set(case.state.markets) == {rig.MARKET_ID, "900002"}
    assert case.state.markets[rig.MARKET_ID]["question"] == first["question"]
    assert case.state.scan_at is case.now
    assert case.state.counters == {**case.counters, "scans_total": 8}
    assert len(rig.read_jsonl(case.state.paths.candidates)) == 2


def test_real_given_rows_keep_missing_base_url_nulls_and_row_not_unique_counts(case, monkeypatch):
    raw = [rig.gamma_row(case.now), rig.gamma_row(case.now, id="900002"),
           rig.gamma_row(case.now, question="last raw duplicate")]

    def never_get(url):
        pytest.fail("given/raw scan must not fetch")

    observed = run_real_scan(case, monkeypatch, never_get, raw=raw)
    row = events(case)[0]
    assert row["fetch"] == observed.results[0]["fetch"]
    assert set(row["fetch"]) == set(KEYS) - {"base_url"}
    assert row["fetch"]["stop_reason"] == "given" and row["fetch"]["complete"] is True
    assert row["fetch"]["pages"] == 0 and row["fetch"]["fetched"] == 3
    assert row["fetch"]["max_markets"] is None and row["fetch"]["server_min_liquidity"] is None
    assert (row["scanned"], row["passed"], len(case.state.markets)) == (3, 3, 2)
    assert len(rig.read_jsonl(case.state.paths.candidates)) == 3
    assert case.state.markets[rig.MARKET_ID]["question"] == "last raw duplicate"
    assert observed.after == [] and observed.order == ["candidates", "scanned"]


@pytest.mark.parametrize("reason", ["cursor_repeat", "no_new_rows", "max_markets", "max_pages", "network_error"])
def test_real_incomplete_fetch_metadata_keeps_previous_scan(case, monkeypatch, reason):
    one, two = rig.gamma_row(case.now), rig.gamma_row(case.now, id="900002")
    pages = {"": ([one], "c1"), "c1": ([two], "c1")}
    bounds = None
    if reason == "no_new_rows":
        pages["c1"] = ([one], "c2")
    elif reason == "max_markets":
        bounds = {"max_markets": 1}
    elif reason == "max_pages":
        bounds = {"max_pages": 1}
    elif reason == "network_error":
        pages["c1"] = "network_error"
    get, urls = pager(case, pages)
    observed = run_real_scan(case, monkeypatch, get, bounds=bounds)
    row, = events(case)
    meta = observed.results[0]["fetch"]
    assert row["fetch"] == {key: meta[key] for key in KEYS if key in meta}
    assert set(row) == {"ts", "event", "stop_reason", "fetch"}
    assert row["event"] == "scan_incomplete" and row["stop_reason"] == meta["stop_reason"] == reason
    assert row["fetch"]["complete"] is False and "error" not in row["fetch"]
    assert case.state.markets is case.previous and case.state.scan_at is case.old_scan_at
    assert case.state.counters == {**case.counters, "scan_failures_total": 3}
    assert observed.order == ["scan_incomplete"] and not case.state.paths.candidates.exists()
    assert len(observed.after) == len(urls)
    if reason == "network_error":
        assert (len(urls), meta["pages"], meta["fetched"]) == (2, 1, 1)
        assert "error" in meta  # optional ninth producer key is intentionally not copied
    elif reason in {"max_markets", "max_pages"}:
        assert (len(urls), meta["pages"], meta["fetched"], meta["cap_hit"]) == (1, 1, 1, True)


def test_page_internal_cap_without_cursor_preserves_complete_true_limitation(case, monkeypatch):
    get, urls = pager(case, {"": ([rig.gamma_row(case.now), rig.gamma_row(case.now, id="900002")], "")})
    observed = run_real_scan(case, monkeypatch, get, bounds={"max_markets": 1})
    row, = events(case)
    assert row["event"] == "scanned" and row["fetch"] == observed.results[0]["fetch"]
    assert (row["fetch"]["fetched"], row["fetch"]["pages"], row["fetch"]["max_markets"]) == (1, 1, 1)
    assert row["fetch"]["stop_reason"] == "exhausted"
    assert row["fetch"]["complete"] is True and row["fetch"]["cap_hit"] is False
    assert len(urls) == 1 and len(case.state.markets) == 1
    # The second input row was not consumed: this flag cannot certify no truncation/coverage.


def test_pure_helper_copies_only_present_raw_subset_without_mutation():
    meta = {"pages": False, "fetched": 0, "stop_reason": None, "complete": "truthy",
            "cap_hit": False, "max_markets": None, "server_min_liquidity": 0.0, "base_url": "literal",
            "error": "not forwarded", "other": 123}
    before = copy.deepcopy(meta)
    result = m2_runner._scan_fetch_facts(meta)
    assert result == {"pages": False, "fetched": 0, "stop_reason": None, "complete": "truthy",
                      "cap_hit": False, "max_markets": None, "server_min_liquidity": 0.0, "base_url": "literal"}
    assert tuple(result) == KEYS and result is not meta and meta == before
    result["pages"] = 99
    assert meta["pages"] is False
    assert m2_runner._scan_fetch_facts({"pages": None, "extra": 1}) == {"pages": None}
    assert m2_runner._scan_fetch_facts({}) == {}


@pytest.mark.parametrize("meta", [None, [], 0, "unknown", False])
def test_non_dict_helper_is_unknown_observation_only(meta):
    assert m2_runner._scan_fetch_facts(meta) is None


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_empty_fetch_keeps_original_incomplete_and_no_default_facts(case, monkeypatch, missing):
    result = {} if missing else {"fetch": {}}
    monkeypatch.setattr(scanner, "scan", lambda **kwargs: result)
    m2_runner._scan(case.state, case.now, m2_runner.GuardedGetter(lambda url: pytest.fail("no GET")))
    row, = events(case)
    assert row == {"ts": m2_runner._stamp(case.now), "event": "scan_incomplete", "stop_reason": None, "fetch": {}}
    assert case.state.markets is case.previous and case.state.scan_at is case.old_scan_at
    assert case.state.counters == {**case.counters, "scan_failures_total": 3}


@pytest.mark.parametrize("meta", [None, [], 0, "invalid", False])
def test_explicit_non_dict_fetch_preserves_original_outside_try_error(case, monkeypatch, meta):
    monkeypatch.setattr(scanner, "scan", lambda **kwargs: {"fetch": meta})
    with pytest.raises(AttributeError):
        m2_runner._scan(case.state, case.now, m2_runner.GuardedGetter(lambda url: pytest.fail("no GET")))
    assert events(case) == [] and case.state.counters == case.counters
    assert case.state.markets is case.previous and case.state.scan_at is case.old_scan_at


@pytest.mark.parametrize("value", [None, [], 0, "invalid"])
def test_non_dict_result_is_not_recategorized_as_scan_failed(case, monkeypatch, value):
    monkeypatch.setattr(scanner, "scan", lambda **kwargs: value)
    with pytest.raises(AttributeError):
        m2_runner._scan(case.state, case.now, m2_runner.GuardedGetter(lambda url: pytest.fail("no GET")))
    assert events(case) == [] and case.state.counters == case.counters


def test_complete_retains_original_truthiness_and_raw_values(case, monkeypatch):
    meta = {"complete": "truthy", "pages": None, "fetched": False, "stop_reason": "literal"}
    result = {"fetch": meta, "rows": [], "scanned": 0, "passed": 0}
    monkeypatch.setattr(scanner, "scan", lambda **kwargs: result)
    m2_runner._scan(case.state, case.now, m2_runner.GuardedGetter(lambda url: pytest.fail("no GET")))
    row, = events(case)
    assert row["event"] == "scanned" and row["fetch"] == meta and set(row["fetch"]) == set(meta)
    assert row["fetch"]["fetched"] is False and row["fetch"]["pages"] is None
    assert case.state.markets == {} and case.state.scan_at is case.now
    assert case.state.counters == {**case.counters, "scans_total": 8}


def test_scanner_exception_keeps_original_scan_failed_without_fetch(case, monkeypatch):
    def fail(**kwargs):
        raise RuntimeError("owned scan failure, no returned metadata")

    monkeypatch.setattr(scanner, "scan", fail)
    m2_runner._scan(case.state, case.now, m2_runner.GuardedGetter(lambda url: pytest.fail("no GET")))
    assert events(case) == [{"ts": m2_runner._stamp(case.now), "event": "scan_failed", "error": "RuntimeError"}]
    assert case.state.markets is case.previous and case.state.scan_at is case.old_scan_at
    assert case.state.counters == {**case.counters, "scan_failures_total": 3}


def test_candidates_write_failure_still_precedes_scanned_event(case, monkeypatch):
    get, _ = pager(case, {"": ([rig.gamma_row(case.now)], "")})

    def fail(rows, path):
        raise OSError("owned candidates failure")

    monkeypatch.setattr(m2_runner.forecast_inbox, "write_candidates", fail)
    with pytest.raises(m2_runner.WriteFailed, match="candidates: OSError"):
        run_real_scan(case, monkeypatch, get)
    assert case.state.scan_at is case.now and rig.MARKET_ID in case.state.markets
    assert case.state.counters == {**case.counters, "scans_total": 8}
    assert case.observed.order == ["candidates"] and events(case) == []


@pytest.mark.parametrize("complete", [True, False])
def test_real_event_fsync_failure_preserves_original_adoption_and_counter_position(case, monkeypatch, complete):
    active = [False]
    real_append, real_fsync = m2_runner.append_synced, m2_runner.os.fsync
    failure = OSError("owned scan-event fsync failure")

    def fsync(fd):
        if active[0]:
            raise failure
        return real_fsync(fd)

    def append(path, row):
        active[0] = path == case.state.paths.events
        try:
            return real_append(path, row)
        finally:
            active[0] = False

    monkeypatch.setattr(m2_runner.os, "fsync", fsync)
    monkeypatch.setattr(m2_runner, "append_synced", append)
    pages = {"": ([rig.gamma_row(case.now)], "" if complete else "c1"), "c1": "network_error"}
    get, _ = pager(case, pages)
    with pytest.raises(m2_runner.WriteFailed) as caught:
        run_real_scan(case, monkeypatch, get)
    assert caught.value.__cause__ is failure
    row, = events(case)  # flush occurred before fsync failed: bytes can remain
    assert row["event"] == ("scanned" if complete else "scan_incomplete") and "fetch" in row
    if complete:
        assert case.state.scan_at is case.now and rig.MARKET_ID in case.state.markets
        assert case.state.counters == {**case.counters, "scans_total": 8}
        assert case.observed.order == ["candidates", "scanned"] and case.state.paths.candidates.exists()
    else:
        assert case.state.scan_at is case.old_scan_at and case.state.markets is case.previous
        assert case.state.counters == {**case.counters, "scan_failures_total": 3}
        assert case.observed.order == ["scan_incomplete"] and not case.state.paths.candidates.exists()


def test_real_iteration_preserves_et_fee_and_es_book_evidence(case, monkeypatch):
    now = case.now
    monkeypatch.setattr(m2_runner.forecaster, "utc_now", lambda: rig.stamp(now - timedelta(seconds=1)))
    case.state.clock = lambda: now
    rig.deliver(case.paths["run"], now)
    fake = rig.FakeGetters(now)
    report = m2_runner.iteration(case.state, now=now, getters=fake.getters())
    assert report.action == m2_runner.ACT_RUN and (report.built, report.allows, report.vetoes) == (1, 1, 0)
    row = next(row for row in events(case) if row["event"] == "scanned")
    assert (row["fetch"]["pages"], row["fetch"]["fetched"], row["fetch"]["complete"]) == (1, 1, True)
    journal = rig.read_jsonl(case.state.paths.journal)
    assert [row["step"] for row in journal] == ["sealed", "fee_quote_evaluated", "built", "committed"]
    assert journal[1]["cache_hit"] == "UNKNOWN" and journal[1]["fee_available"] is True
    assert journal[2]["book_fetch_evidence"] == {
        token: {"fetched_at": now.isoformat(timespec="microseconds"), "source": "clob_public_book"}
        for token in rig.TOKENS}
    fills = rig.read_jsonl(case.paths["paper"])
    assert len(fills) == 1 and fills[0]["risk_decision"] == "allow"
    assert fills[0]["decision_id"] == journal[2]["decision_id"] and fills[0]["forecast_id"] == journal[1]["forecast_id"]
    assert (len(fake.gamma_urls), len(fake.book_urls), len(fake.time_calls)) == (2, 2, 1)
    _, ledger = ledger_store.read_ledger(case.paths["paper"])
    assert ledger_store.verify(ledger) == []
