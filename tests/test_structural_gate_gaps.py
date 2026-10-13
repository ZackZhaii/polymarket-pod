"""Gap-fill tests for the #141 risk gate (src/structural_gate.py): the outcome-set-detail and
key-of-record branches the reviewed vectors (tests/fixtures/issue141_vectors.json) do not reach.

Every vector's ``closed`` leg settled NO (``outcomePrices`` ["0", "1"]), so ``closed_not_resolved_no``
is never populated there, and no vector has a live "Other" leg, so an augmented event that *is*
complete is never exercised. Same for the strict two-element payout rule, a loosely flagged
``negRiskAugmented``, the multi-leg-only detail, the Gamma / scanner key reads, and the fail-closed
shapes of the JSON ``umaResolutionStatuses`` / end-date inputs.

Offline and pure: each call passes its own dicts and an aware datetime. No network, no files, no
ledger, no bus, no config. Nothing here re-enables a multi-leg signal: a complete set still rejects.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.structural_gate import (
    MARKET_EXPIRED_OR_RESOLVING,
    OUTCOME_SET_INCOMPLETE,
    REASON_ORDER,
    STRUCTURAL_ARB_DISABLED,
    market_uma_resolving,
    outcome_set_detail,
    resolution_problems,
    structural_reasons,
    uma_resolving,
)

NOW = datetime(2026, 10, 10, 12, 58, 19, tzinfo=timezone.utc)  # the vectors' snapshot start
FUTURE = "2027-01-01T04:59:00Z"
PAST = "2026-10-10T03:59:00Z"


def leg(title: str, **flags: object) -> dict:
    """A live Gamma market with the flags outcome_set_detail reads, overridable per test."""
    market: dict = {"id": title, "closed": False, "active": True, "acceptingOrders": True}
    market.update(flags)
    return market


# --- outcome_set_detail: a closed leg completes the set only when it settled NO --------------------

@pytest.mark.parametrize(("prices", "resolved_no"), [
    (["0", "1"], True),          # the Gamma list form (what every vector carries)
    ('["0", "1"]', True),        # the same mapping as the JSON string Gamma sometimes sends
    (["0.0", "1.0"], True),
    (["1", "0"], False),         # settled YES: this leg can still win, so the set is not complete
    (["0.5", "0.5"], False),     # not a settled payout at all
    (["0", "1", "0"], False),    # three prices: not a two-outcome leg, never assumed NO
    ([], False),
    (None, False),               # no prices: unproven, fail closed
    ("[0, 1", False),            # unparseable JSON: fail closed
    ("0,1", False),
    (0.0, False),                # a bare number is not a price vector
])
def test_a_closed_leg_counts_as_complete_only_when_it_settled_no(prices, resolved_no):
    detail = outcome_set_detail({"negRiskAugmented": False},
                                [leg("closed-leg", closed=True, outcomePrices=prices)])
    assert detail["closed_not_resolved_no"] == ([] if resolved_no else ["closed-leg"])
    assert detail["live_leg_count"] == 0
    assert detail["incomplete"] is (not resolved_no)


# --- outcome_set_detail: negRiskAugmented needs a live "Other" --------------------------------------

def test_a_live_other_completes_an_augmented_set():
    event = {"negRiskAugmented": True}
    markets = [leg("a"), leg("other", negRiskOther=True)]
    detail = outcome_set_detail(event, markets)
    assert (detail["negRiskAugmented"], detail["has_live_other"], detail["live_leg_count"]) == (True, True, 2)
    assert (detail["augmented_without_live_other"], detail["incomplete"]) == (False, False)
    # the multi-leg reject is unchanged: a complete, live set is still a structural complete set
    assert structural_reasons(legs=2, now=NOW, event=event, event_markets=markets) == [STRUCTURAL_ARB_DISABLED]


@pytest.mark.parametrize("flags", [
    {"active": False},                              # an inactive placeholder
    {"acceptingOrders": False},                     # live but not accepting orders
    {"closed": True, "outcomePrices": ["1", "0"]},  # closed, settled YES
    {"closed": True},                               # closed with no prices at all
])
def test_an_other_leg_that_is_not_live_does_not_complete_the_set(flags):
    detail = outcome_set_detail({"negRiskAugmented": True}, [leg("a"), leg("other", negRiskOther=True, **flags)])
    assert detail["has_live_other"] is False
    assert detail["augmented_without_live_other"] is True
    assert detail["incomplete"] is True


@pytest.mark.parametrize("event", [{}, {"negRiskAugmented": False}, {"negRiskAugmented": "true"},
                                   {"negRiskAugmented": 1}, {"negRiskAugmented": None}])
def test_only_a_strict_boolean_true_marks_a_set_as_augmented(event):
    detail = outcome_set_detail(event, [leg("a"), leg("b")])
    assert detail["negRiskAugmented"] is False
    assert (detail["augmented_without_live_other"], detail["incomplete"]) == (False, False)


# --- structural_reasons: the detail is a multi-leg-only, event-scoped add-on ------------------------

def test_the_outcome_set_detail_is_reported_only_for_multi_leg_candidates():
    event = {"negRiskAugmented": True}
    markets = [leg("a"), leg("b", active=False)]
    assert outcome_set_detail(event, markets)["incomplete"] is True
    # a single-leg candidate gets no complete-set detail: it is not a complete set in the first place
    assert structural_reasons(legs=1, now=NOW, markets=[{"id": "a", "endDate": FUTURE}],
                              event=event, event_markets=markets) == []
    # no event_markets: the reject stands, the detail cannot be computed and is not guessed
    assert structural_reasons(legs=2, now=NOW, event=event) == [STRUCTURAL_ARB_DISABLED]
    assert structural_reasons(legs=2, now=NOW, event=event, event_markets=markets) == [
        STRUCTURAL_ARB_DISABLED, OUTCOME_SET_INCOMPLETE]


# --- uma_resolving: the Gamma key, the scanner key, and fail-closed JSON ---------------------------

@pytest.mark.parametrize(("market", "resolving"), [
    ({"umaResolutionStatus": "disputed"}, True),            # a raw Gamma market
    ({"umaResolutionStatus": "  proposed  "}, True),
    ({"umaResolutionStatus": ""}, False),
    ({"uma_resolution_status": "disputed"}, True),          # a scanner row
    ({"uma_resolution_statuses": "[]"}, False),
    ({"umaResolutionStatuses": "[]"}, False),
    ({"uma_resolution_statuses": "[oops"}, True),           # unparseable: fail closed
    ({"umaResolutionStatuses": '["proposed"]'}, True),
    ({"uma_resolution_statuses": "null"}, True),            # JSON null is not a list: fail closed
    ({"uma_resolution_statuses": " [] "}, False),           # surrounding space is still an empty list
    ({"uma_resolution_status": "", "uma_resolution_statuses": "[]"}, False),
])
def test_market_uma_resolving_reads_a_gamma_or_a_scanner_key(market, resolving):
    assert market_uma_resolving(market) is resolving


@pytest.mark.parametrize(("statuses", "resolving"), [
    ((), False),                 # an empty tuple is an empty list
    (("proposed",), True),
    ("[]", False),
    ('"[]"', True),              # a JSON string, not a list: fail closed
    ("null", True),
    ("{}", True),
    ({"proposed": True}, True),  # neither a text nor a sequence: fail closed
    (0, True),
    (False, True),
])
def test_uma_resolving_json_and_sequence_shapes(statuses, resolving):
    assert uma_resolving(None, statuses) is resolving


# --- resolution_problems: the end date and the clock ------------------------------------------------

@pytest.mark.parametrize("market", [{}, {"end_date": ""}, {"end_date": "   "}, {"endDate": ""},
                                    {"endDate": None}, {"end_date": None}])
def test_a_blank_or_missing_end_date_adds_no_expiry_reason(market):
    assert resolution_problems(market, NOW) == []


def test_a_blank_end_date_never_hides_a_resolving_market_and_the_gamma_key_is_read():
    assert resolution_problems({"end_date": "   ", "uma_resolution_status": "disputed"},
                               NOW) == [MARKET_EXPIRED_OR_RESOLVING]
    assert resolution_problems({"endDate": FUTURE}, NOW) == []
    assert resolution_problems({"endDate": "2026-10-10T12:58:18Z"}, NOW) == ["expired"]


@pytest.mark.parametrize("now", [datetime(2026, 10, 10, 12, 58, 19),  # naive: never guessed as UTC
                                 "2026-10-10T12:58:19Z", None, 0])
def test_an_unusable_now_is_reported_when_there_is_an_end_date(now):
    assert resolution_problems({"endDate": FUTURE}, now) == ["invalid_now"]
    # without an end date the clock is not consulted at all: a resolving market is still reported
    assert resolution_problems({}, now) == []
    assert resolution_problems({"umaResolutionStatus": "disputed"}, now) == [MARKET_EXPIRED_OR_RESOLVING]


# --- reason list shape -----------------------------------------------------------------------------

def test_repeated_markets_and_reasons_are_reported_once_in_the_canonical_order():
    market = {"endDate": PAST, "umaResolutionStatus": "proposed"}
    reasons = structural_reasons(legs=2, now=NOW, markets=[market, dict(market)])
    assert reasons == ["expired", MARKET_EXPIRED_OR_RESOLVING, STRUCTURAL_ARB_DISABLED]
    assert reasons == sorted(set(reasons), key=REASON_ORDER.index)
