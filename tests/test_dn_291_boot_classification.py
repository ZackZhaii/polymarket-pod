"""Task DN (#291 acceptance, Refs #137): category optional, sports tag blocklist / gameId, classifier fail-safe.

Regression fixture: top 30 scanner rows (by 24h volume) from Poly扫描's real Gamma keyset snapshot
accept291_20261014_ (2026-10-14T01:22:52Z), run through src.scanner.filter_markets. Every row has
category None, as the keyset returns it. Offline; no network, no orders.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.m2_start as m2_start

FIXTURE = Path(__file__).parent / "fixtures" / "accept291_20261014_top30_markets.jsonl"
NOW = datetime(2026, 10, 14, 1, 22, 52, tzinfo=timezone.utc)
END = datetime(2026, 10, 21, tzinfo=timezone.utc)
TOP5 = ["2589812", "560317", "5126779", "4641065", "2589813"]


def _write(tmp_path, rows):
    path = tmp_path / "data" / "markets.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return tmp_path


def _market_ids(rows, assets):
    owner = {t: str(r["id"]) for r in rows for t in r["token_ids"]}
    out = []
    for a in assets:
        if owner[a] not in out:
            out.append(owner[a])
    return out


def _real_rows():
    return [json.loads(line) for line in FIXTURE.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_fixture_has_no_category_like_the_real_keyset():
    rows = _real_rows()
    assert rows and all(r.get("category") is None for r in rows)


@pytest.mark.parametrize("m2_end", [datetime(2026, 10, 21, tzinfo=timezone.utc), datetime(2026, 10, 22, tzinfo=timezone.utc)])
def test_real_snapshot_top5(tmp_path, m2_end):
    rows = _real_rows()
    assets, why = m2_start.boot_selection(_write(tmp_path, rows), NOW, m2_end)
    assert why is None
    assert _market_ids(rows, assets) == TOP5


def _row(i, **kw):
    r = {"id": str(i), "ts": "2026-10-14T00:00:00Z", "version": "v1", "token_ids": [f"t{i}a", f"t{i}b"],
         "token_ids_problem": None, "end_date": "2099-01-01T00:00:00Z", "tags": ["politics"], "category": None,
         "volume_24h_usd": 1000.0 - i}
    for k, v in kw.items():
        if v is KeyError:
            r.pop(k, None)
        else:
            r[k] = v
    return r


def _ids(tmp_path, rows, end=END):
    assets, why = m2_start.boot_selection(_write(tmp_path, rows), NOW, end)
    return [t[1:-1] for t in assets[::2]], why


@pytest.mark.parametrize("category", [KeyError, None, "politics", " Politics "])
def test_category_missing_none_or_text_is_ok(tmp_path, category):
    assert m2_start.classification_ok(_row(1, category=category))
    assert _ids(tmp_path, [_row(1, category=category)]) == (["1"], None)


@pytest.mark.parametrize("category", ["", "   ", 3, 0, ["politics"], {"x": 1}, True])
def test_bad_category_dropped(tmp_path, category):
    assert not m2_start.classification_ok(_row(1, category=category))
    assert _ids(tmp_path, [_row(1, category=category), _row(2)])[0] == ["2"]


@pytest.mark.parametrize("tags", [KeyError, None, "politics", {"politics": 1}, ["politics", 7], [None], 5])
def test_tags_not_list_of_str_dropped(tmp_path, tags):
    assert not m2_start.classification_ok(_row(1, tags=tags))
    assert _ids(tmp_path, [_row(1, tags=tags), _row(2)])[0] == ["2"]


def test_blocklist_is_a_frozen_lowercase_constant_with_the_basics():
    block = m2_start.SPORTS_TAG_BLOCKLIST
    assert isinstance(block, frozenset) and all(t == t.strip().lower() for t in block)
    assert {"sports", "esports", "tennis", "nfl", "nba", "soccer", "mlb", "nhl", "mma", "ufc", "cricket", "golf",
            "f1", "boxing", "football", "games"} <= block


@pytest.mark.parametrize("tag", sorted(m2_start.SPORTS_TAG_BLOCKLIST))
def test_each_blocklist_tag_dropped(tmp_path, tag):
    for variant in (tag, f"  {tag.upper()} "):
        rows = [_row(1, tags=["politics", variant]), _row(2)]
        assert _ids(tmp_path / variant.strip().replace(" ", "_") / str(variant == tag), rows)[0] == ["2"]


@pytest.mark.parametrize("key, value", [("gameId", "12345"), ("gameId", 12345), ("game_id", "g1")])
def test_game_id_counts_as_sports(tmp_path, key, value):
    assert m2_start.is_sports(_row(1, **{key: value}))
    assert _ids(tmp_path, [_row(1, **{key: value}), _row(2)])[0] == ["2"]


@pytest.mark.parametrize("value", [None, "", "  ", 0])
def test_empty_game_id_is_not_sports(value):
    assert not m2_start.is_sports(_row(1, gameId=value))


def test_classifier_exception_counted_and_dropped(tmp_path, monkeypatch, capsys):
    real = m2_start.is_sports

    def boom(row):
        if row.get("id") == "1":
            raise RuntimeError("classifier boom")
        return real(row)

    monkeypatch.setattr(m2_start, "is_sports", boom)
    root = _write(tmp_path, [_row(1), _row(2)])
    detail = m2_start.boot_selection_detail(root, NOW, END)
    assert detail["classifier_errors"] == 1 and detail["why"] is None and detail["assets"] == ["t2a", "t2b"]
    assert "classifier error" in capsys.readouterr().err
    assert m2_start.boot_selection(root, NOW, END) == (["t2a", "t2b"], None)


def test_classifier_exception_on_every_row_refuses_without_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(m2_start, "is_sports", lambda row: (_ for _ in ()).throw(ValueError("x")))
    detail = m2_start.boot_selection_detail(_write(tmp_path, [_row(1), _row(2)]), NOW, END)
    assert detail["assets"] == [] and detail["classifier_errors"] == 2 and "no usable assets" in detail["why"]
