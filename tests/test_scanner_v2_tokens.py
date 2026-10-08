"""Task Q: the scanner reads V2 outcome IDs from positionIds.

Per the Polymarket Protocol V2 docs, Gamma's `version` selects the ID field even
when both are present: "v2" -> positionIds (array of decimal strings), "v1" ->
clobTokenIds (JSON-encoded array). A missing or unrecognized version rejects the
market (fail closed); the field is never guessed from which IDs are present.
Mocked Gamma payloads only. No network, no orders, no risk-file edits.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.book as book
import src.scanner as scanner
from src.podlib import RISK_PATH, read_jsonl

TS = "2026-10-08T08:00:00Z"
NOW = datetime(2026, 10, 8, 8, 0, 0, tzinfo=timezone.utc)
LIMITS = {"min_liquidity_usd": 10000.0, "max_spread": 0.04, "min_price": 0.03, "max_price": 0.97}
P_YES = "651150819117105875331414918119047680898421632356043490229292782433651916800"
P_NO = "651150819117105875331414918119047680898421632356043490229292782433651916801"
C_YES = "71321045679252212594626385532706912750332728571942532289631379312455583992563"
C_NO = "52114319501245915516055106046884209969926127482827954674443846427813813222426"


def _market(market_id: str = "m", volume: float = 1.0, **ids: object) -> dict:
    row: dict[str, object] = {
        "id": market_id,
        "slug": market_id,
        "question": f"Synthetic market {market_id}",
        "outcomes": json.dumps(["Yes", "No"]),
        "liquidityNum": 20000,
        "volume24hr": volume,
        "bestBid": 0.49,
        "bestAsk": 0.51,
        "spread": 0.02,
        "endDate": "2026-12-31T00:00:00Z",
        "closed": False,
        "acceptingOrders": True,
    }
    row.update(ids)
    return row


def _select(**ids: object):
    return scanner.select_token_ids(_market(**ids))


# --- version selects the field ----------------------------------------------


def test_v2_reads_position_ids_list():
    assert _select(version="v2", clobTokenIds=None, positionIds=[P_YES, P_NO]) == (
        [P_YES, P_NO], "positionIds", None)


def test_v2_position_ids_json_string_is_decoded_like_clob_token_ids():
    assert _select(version="v2", positionIds=json.dumps([P_YES, P_NO])) == ([P_YES, P_NO], "positionIds", None)


def test_v1_reads_clob_token_ids_json_string():
    assert _select(version="v1", clobTokenIds=json.dumps([C_YES, C_NO])) == ([C_YES, C_NO], "clobTokenIds", None)


def test_v1_clob_token_ids_list_is_accepted():
    assert _select(version="v1", clobTokenIds=[C_YES, C_NO]) == ([C_YES, C_NO], "clobTokenIds", None)


def test_version_decides_when_both_fields_are_present_and_differ():
    both = {"clobTokenIds": json.dumps([C_YES, C_NO]), "positionIds": [P_YES, P_NO]}
    assert _select(version="v2", **both)[0] == [P_YES, P_NO]
    assert _select(version="v1", **both)[0] == [C_YES, C_NO]


def test_live_v1_row_with_differing_position_ids_uses_clob_token_ids():
    # Shape seen on live Gamma rows: version "v1" with both fields populated and different.
    assert _select(version="v1", clobTokenIds=json.dumps([C_YES, C_NO]), positionIds=[P_YES, P_NO]) == (
        [C_YES, C_NO], "clobTokenIds", None)


def test_v2_never_falls_back_to_clob_token_ids():
    ids, source, problem = _select(version="v2", clobTokenIds=json.dumps([C_YES, C_NO]), positionIds=None)
    assert (ids, source, problem) == ([], "positionIds", "missing_token_ids")


def test_v1_never_falls_back_to_position_ids():
    ids, _, problem = _select(version="v1", clobTokenIds=None, positionIds=[P_YES, P_NO])
    assert ids == [] and problem == "missing_token_ids"


@pytest.mark.parametrize("version", ["v3", "V0", "V2", " v2", "v1 ", "   ", "2", 2, True, ["v2"], {"v": 2}])
def test_unsupported_version_gets_no_ids(version):
    ids, source, problem = _select(version=version, clobTokenIds=json.dumps([C_YES, C_NO]),
                                   positionIds=[P_YES, P_NO])
    assert (ids, source, problem) == ([], None, "unsupported_version")


# --- no version: reject, never guess from the fields present ----------------


@pytest.mark.parametrize("version", ["absent", None, ""])
@pytest.mark.parametrize(
    "ids",
    [
        {"clobTokenIds": json.dumps([C_YES, C_NO])},
        {"positionIds": [P_YES, P_NO]},
        {"clobTokenIds": json.dumps([C_YES, C_NO]), "positionIds": [P_YES, P_NO]},
        {"clobTokenIds": json.dumps([P_YES, P_NO]), "positionIds": [P_YES, P_NO]},
        {},
    ],
)
def test_missing_version_gets_no_ids_whatever_fields_exist(version, ids):
    market = _market(**ids)
    if version != "absent":
        market["version"] = version
    assert scanner.select_token_ids(market) == ([], None, "missing_version")


@pytest.mark.parametrize(
    "version, reason",
    [("absent", "missing_version"), (None, "missing_version"), ("", "missing_version"),
     ("v3", "unsupported_version"), ("V2", "unsupported_version"), (2, "unsupported_version")],
)
def test_missing_or_unknown_version_is_a_rejection_reason(version, reason):
    market = _market(clobTokenIds=json.dumps([C_YES, C_NO]), positionIds=[P_YES, P_NO])
    if version != "absent":
        market["version"] = version
    row = scanner.normalize(market, TS)
    assert reason in scanner.REASONS
    assert scanner.rejection_reasons(row, LIMITS, NOW) == [reason]


def test_version_reason_ranks_right_after_closed():
    row = scanner.normalize(_market(closed=True, liquidityNum=1, clobTokenIds=json.dumps([C_YES, C_NO])), TS)
    assert scanner.rejection_reasons(row, LIMITS, NOW)[:3] == ["closed", "missing_version", "low_liquidity"]


def test_valid_version_adds_no_rejection_reason():
    for market in (_market(version="v2", positionIds=[P_YES, P_NO]),
                   _market(version="v1", clobTokenIds=json.dumps([C_YES, C_NO]))):
        assert scanner.rejection_reasons(scanner.normalize(market, TS), LIMITS, NOW) == []


# --- malformed or missing IDs fail closed (no IDs, so no book downstream) ---


@pytest.mark.parametrize("value", [None, "", "[]", []])
def test_v2_missing_or_empty_position_ids(value):
    assert _select(version="v2", positionIds=value) == ([], "positionIds", "missing_token_ids")


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        json.dumps({"yes": P_YES}),
        {"yes": P_YES},
        123,
        [P_YES, None],
        [P_YES, ""],
        [P_YES, True],
        [P_YES, 1.5],
        [P_YES, [P_NO]],
        [P_YES, "0x1f"],
        [P_YES, "-5"],
        [P_YES, "１２"],  # full-width digits are not ASCII decimal
    ],
)
def test_v2_malformed_position_ids(value):
    assert _select(version="v2", positionIds=value) == ([], "positionIds", "invalid_token_ids")


@pytest.mark.parametrize("value", ["not json", json.dumps({"a": 1}), [C_YES, None], [C_YES, ""], [C_YES, False]])
def test_v1_malformed_clob_token_ids(value):
    assert _select(version="v1", clobTokenIds=value) == ([], "clobTokenIds", "invalid_token_ids")


def test_v2_integer_ids_are_kept_as_decimal_strings():
    assert _select(version="v2", positionIds=[int(P_YES), int(P_NO)])[0] == [P_YES, P_NO]


def test_id_count_must_match_outcomes():
    assert _select(version="v2", positionIds=[P_YES]) == ([], "positionIds", "token_count_mismatch")
    assert _select(version="v1", clobTokenIds=json.dumps([C_YES, C_NO, "3"])) == (
        [], "clobTokenIds", "token_count_mismatch")


def test_missing_outcomes_skip_the_count_check():
    market = _market(version="v2", positionIds=[P_YES, P_NO])
    del market["outcomes"]
    assert scanner.select_token_ids(market)[0] == [P_YES, P_NO]


# --- normalize / filter / main / book ----------------------------------------


def test_normalize_records_version_source_and_problem():
    row = scanner.normalize(_market(version="v2", positionIds=[P_YES, P_NO]), TS)
    assert row["token_ids"] == [P_YES, P_NO]
    assert row["version"] == "v2"
    assert row["token_id_source"] == "positionIds"
    assert row["token_ids_problem"] is None
    bad = scanner.normalize(_market(version="v9", positionIds=[P_YES, P_NO]), TS)
    assert bad["token_ids"] == [] and bad["token_ids_problem"] == "unsupported_version"


MIXED = [
    _market("v2-list", volume=5, version="v2", clobTokenIds=None, positionIds=[P_YES, P_NO]),
    _market("v2-both", volume=4, version="v2", clobTokenIds=json.dumps([C_YES, C_NO]), positionIds=[P_YES, P_NO]),
    _market("v1", volume=3, version="v1", clobTokenIds=json.dumps([C_YES, C_NO])),
    _market("no-version", volume=9, clobTokenIds=json.dumps([C_YES, C_NO])),
    _market("v3", volume=8, version="v3", positionIds=[P_YES, P_NO]),
    _market("v2-missing", volume=1, version="v2", clobTokenIds=json.dumps([C_YES, C_NO])),
]


def test_filter_keeps_v2_rejects_unknown_versions_and_counts_problems():
    result = scanner.filter_markets(MIXED, LIMITS, TS, now=NOW)
    ids = {row["id"]: row["token_ids"] for row in result["rows"]}
    assert ids["v2-list"] == [P_YES, P_NO]
    assert ids["v2-both"] == [P_YES, P_NO]
    assert ids["v1"] == [C_YES, C_NO]
    # Fail closed on version: neither field is guessed, the market is rejected.
    assert "no-version" not in ids and "v3" not in ids
    assert result["first_reason"] == {"missing_version": 1, "unsupported_version": 1}
    # Valid version but no IDs: existing behaviour, not a rejection; the row just has no tokens to book.
    assert ids["v2-missing"] == []
    assert result["passed"] == 4
    assert result["token_id_problems"] == {"missing_token_ids": 1}


def _pager(rows: list[dict]):
    def getter(url: str) -> dict:
        assert url.startswith("https://gamma-api.polymarket.com/markets/keyset?")
        return {"markets": rows, "next_cursor": ""}

    return getter


def test_main_writes_v2_position_ids(tmp_path: Path, capsys):
    before = RISK_PATH.read_bytes()
    markets = tmp_path / "markets.jsonl"
    code = scanner.main(getter=_pager(MIXED), markets_path=markets, bus_path=tmp_path / "bus.jsonl", ts=TS)
    assert code == scanner.EXIT_OK
    written = {row["id"]: row for row in read_jsonl(markets)}
    assert written["v2-list"]["token_ids"] == [P_YES, P_NO]
    assert written["v2-list"]["token_id_source"] == "positionIds"
    assert "no-version" not in written and "v3" not in written
    out = capsys.readouterr().out
    assert "token_id_problems=missing_token_ids:1" in out
    assert "missing_version" not in out.split("token_id_problems=")[1]
    assert RISK_PATH.read_bytes() == before


def test_book_record_asks_for_the_v2_position_id(tmp_path: Path):
    row = scanner.normalize(_market(version="v2", positionIds=[P_YES, P_NO]), TS)
    asked: list[str] = []

    def getter(url: str) -> dict:
        assert url.startswith("https://clob.polymarket.com/book?")
        asked.append(url)
        return {"bids": [{"price": "0.49", "size": "10"}], "asks": [{"price": "0.51", "size": "10"}]}

    written = book.record([row], TS, books_path=tmp_path / "books.jsonl", bus_path=tmp_path / "bus.jsonl",
                          getter=getter)
    assert written == 2
    assert [url.split("token_id=")[1] for url in asked] == [P_YES, P_NO]


def test_main_fails_closed_when_no_market_has_a_version(tmp_path: Path):
    rows = [_market(f"m{i}", clobTokenIds=json.dumps([C_YES, C_NO]), positionIds=[P_YES, P_NO]) for i in range(5)]
    markets = tmp_path / "markets.jsonl"
    code = scanner.main(getter=_pager(rows), markets_path=markets, bus_path=tmp_path / "bus.jsonl", ts=TS)
    assert code == scanner.EXIT_NONE_PASSED
    assert not markets.exists()


def test_v2_market_without_ids_gets_no_book(tmp_path: Path):
    row = scanner.normalize(_market(version="v2", clobTokenIds=json.dumps([C_YES, C_NO])), TS)

    def getter(url: str) -> dict:
        raise AssertionError("must not fetch a book without a V2 position id")

    assert book.record([row], TS, books_path=tmp_path / "books.jsonl", bus_path=tmp_path / "bus.jsonl",
                       getter=getter) == 0
