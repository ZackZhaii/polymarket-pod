"""Issue #131: a market's category is fail closed, from its Gamma tags (src/category.py).

Missing, empty or unusable tags veto missing_category in intent_builder and RiskGate alike. A Gamma
category=None market with tags is classified (``other`` with the minimal mapping), not blocked. A banned
bucket / tag / category vetoes category_banned. The banned-category source is explicit
(category.BANNED_CATEGORIES), and an unreadable one vetoes banned_categories_unreadable instead of reading
as empty. Fills record the buckets (category_buckets). #135's bucket table, caps and sizing are not here.

Offline. config/risk.toml is read, never written.
"""
from __future__ import annotations

import argparse

import pytest

import src.category as category
import src.paper_ledger as paper_ledger
from src.podlib import RISK_PATH, load_risk, read_jsonl
from src.risk_gate import evaluate
from tests.test_calibration_ids import STATE, _ok_intent
from tests.test_intent_builder import build, market

CATEGORY_REASONS = {"missing_category", "category_banned", "banned_categories_unreadable", "invalid_category_buckets"}
UNUSABLE_TAGS = [None, [], (), "politics", ["", "  "], [1, None], {"politics"}]


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", tmp_path / "paper.jsonl")
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: None)
    return tmp_path / "paper.jsonl"


def _gate(**kw):
    return evaluate(_ok_intent(**kw), load_risk(), dict(STATE))


def _cat(reasons) -> set[str]:
    return set(reasons) & CATEGORY_REASONS


# ---------------------------------------------------------------- classify

@pytest.mark.parametrize("tags", UNUSABLE_TAGS)
def test_unusable_tags_are_missing_category(tags):
    assert category.classify(tags) == ((), "missing_category")
    assert category.bucket_label(tags) is None


def test_tags_without_a_bucket_are_other():
    assert category.classify(["US", " elections ", "us"]) == (("other",), None)
    assert category.tag_slugs(["US", " elections ", "us"]) == ("us", "elections")
    assert category.bucket_label(("test-market",)) == ["other"]


def test_the_minimal_mapping_is_empty_and_banned_is_explicitly_empty():
    assert dict(category.CATEGORY_BUCKETS) == {}  # the real table is KEY #135
    assert category.BANNED_CATEGORIES == frozenset()
    assert category.banned_categories() == (frozenset(), None)


def test_a_bucket_table_classifies_by_tag_slug():
    table = {"politics": frozenset({"elections"}), "macro": frozenset({"fed"})}
    assert category.classify(["Elections", "x"], table) == (("politics",), None)
    assert category.classify(["fed", "elections"], table) == (("macro", "politics"), None)
    assert category.classify(["x"], table) == (("other",), None)


@pytest.mark.parametrize("table", [["politics"], {"": frozenset({"a"})}, {"other": frozenset({"a"})},
                                   {"politics": ["a"]}, {"politics": frozenset({1})}])
def test_a_bad_bucket_table_fails_closed(table):
    assert category.classify(["a"], table) == ((), "invalid_category_buckets")


@pytest.mark.parametrize("source", [["x"], "x", ("x",), {"x": 1}, frozenset({""}), frozenset({1}), 0])
def test_an_unreadable_banned_source_is_never_empty(source):
    assert category.banned_categories(source) == (None, "banned_categories_unreadable")


@pytest.mark.parametrize("label", ["other", "test-market", "TEST-MARKET", "gamma-cat"])
def test_a_banned_bucket_tag_or_category_vetoes(label):
    assert category.category_problems("gamma-cat", ("test-market",), banned=frozenset({label})) == ["category_banned"]


def test_unbanned_labels_pass():
    assert category.category_problems(None, ("test-market",), banned=frozenset({"politics"})) == []


# ---------------------------------------------------------------- RiskGate

@pytest.mark.parametrize("tags", UNUSABLE_TAGS)
def test_riskgate_vetoes_missing_category(tags):
    decision = _gate(tags=tags)
    assert decision.allow is False
    assert decision.reasons == ["missing_category"]


def test_riskgate_allows_a_tagged_market_without_a_gamma_category():
    decision = _gate(category=None, tags=("us", "elections"))
    assert decision.allow is True, decision.reasons


def test_riskgate_vetoes_a_banned_category(monkeypatch):
    monkeypatch.setattr(category, "BANNED_CATEGORIES", frozenset({"elections"}))
    assert _gate(tags=("us", "elections")).reasons == ["category_banned"]
    assert _gate(tags=("us",)).allow is True


@pytest.mark.parametrize("source", [None, ["elections"], "elections", {"": 1}])
def test_riskgate_vetoes_an_unreadable_banned_source(monkeypatch, source):
    monkeypatch.setattr(category, "BANNED_CATEGORIES", source)
    assert "banned_categories_unreadable" in _gate().reasons


def test_riskgate_vetoes_a_bad_bucket_table(monkeypatch):
    monkeypatch.setattr(category, "CATEGORY_BUCKETS", ["politics"])
    assert "invalid_category_buckets" in _gate().reasons


# ---------------------------------------------------------------- intent_builder

@pytest.mark.parametrize("tags", [None, (), ("",)])
def test_intent_builder_vetoes_missing_category(tags):
    result = build(mkt=market(tags=tags))
    assert result.intent is None
    assert "missing_category" in result.reasons


def test_intent_builder_classifies_a_tagged_market_without_a_gamma_category():
    result = build(mkt=market(tags=("us",), gamma_category=None, category=None))
    assert not _cat(result.reasons), result.reasons


def test_intent_builder_banned_by_explicit_source_tag_or_gamma_category(monkeypatch):
    assert "category_banned" in build(mkt=market(category="x"), banned_categories=frozenset({"x"})).reasons
    assert "category_banned" in build(mkt=market(gamma_category="Sports"), banned_categories=frozenset({"sports"})).reasons
    assert "category_banned" in build(mkt=market(tags=("crypto",)), banned_categories=frozenset({"crypto"})).reasons
    monkeypatch.setattr(category, "BANNED_CATEGORIES", frozenset({"test-market"}))
    assert "category_banned" in build().reasons  # the default source is the module's list


@pytest.mark.parametrize("source", [["x"], "x", frozenset({""})])
def test_intent_builder_vetoes_an_unreadable_banned_source(source):
    assert "banned_categories_unreadable" in build(banned_categories=source).reasons


def test_intent_builder_vetoes_an_unreadable_module_source(monkeypatch):
    monkeypatch.setattr(category, "BANNED_CATEGORIES", None)
    assert "banned_categories_unreadable" in build().reasons


def test_the_new_reasons_are_listed():
    from src import intent_builder as ib
    text = (ib.__file__ and open(ib.__file__, encoding="utf-8").read())
    for reason in CATEGORY_REASONS:
        assert f'"{reason}"' in text


# ---------------------------------------------------------------- the fill records the buckets

def test_a_fill_records_its_category_buckets(ledger):
    result = paper_ledger.commit_if_allowed(_ok_intent(category=None, tags=("us",)), load_risk())
    assert result["allow"] is True, result
    fill = read_jsonl(ledger)[0]
    assert fill["category_buckets"] == ["other"] and fill["tags"] == ["us"] and fill["category"] is None


def test_a_fill_records_the_mapped_buckets(ledger, monkeypatch):
    monkeypatch.setattr(category, "CATEGORY_BUCKETS", {"politics": frozenset({"us"})})
    paper_ledger.commit_if_allowed(_ok_intent(tags=("us",)), load_risk())
    assert read_jsonl(ledger)[0]["category_buckets"] == ["politics"]


def test_an_untagged_intent_books_nothing(ledger):
    result = paper_ledger.commit_if_allowed(_ok_intent(tags=None), load_risk())
    assert result["allow"] is False and result["decision"]["reasons"] == ["missing_category"]
    assert not ledger.exists() or read_jsonl(ledger) == []


def _cli(**kw):
    base = dict(strategy="manual", market="m1", side="buy", model_prob=0.7, market_price=0.4, slippage=0.005,
                fee_rate=0.0, liquidity=20000.0, spread=0.01, token_id="t", book_ts=paper_ledger.utc_now(),
                note="", tags=None, min_order_size=5.0)  # issue #226: the book minimum (missing vetoes)
    base.update(kw)
    return argparse.Namespace(**base)


def test_the_operator_cli_needs_tags(ledger):
    vetoed = paper_ledger.run_cli_intent(_cli(), load_risk())
    assert vetoed["allow"] is False and vetoed["decision"]["reasons"] == ["missing_category"]
    allowed = paper_ledger.run_cli_intent(_cli(tags="us, elections"), load_risk())
    assert allowed["allow"] is True, allowed
    assert read_jsonl(ledger)[0]["tags"] == ["us", "elections"]


def test_risk_file_is_untouched(ledger):
    before = RISK_PATH.read_bytes()
    _gate(tags=None)
    paper_ledger.commit_if_allowed(_ok_intent(), load_risk())
    assert RISK_PATH.read_bytes() == before
