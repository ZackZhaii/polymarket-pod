"""Shared offline rig for the M2 runner tests (issue #119): one Gamma market, its two CLOB books, one inbox file.

Not a test module. Every path is in the test's tmp dir (conftest isolates halts, heartbeats and forecasts; this
adds the paper ledger, quotes and bus). Getters are fakes that record each URL; nothing touches the network.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import src.paper_ledger as paper_ledger
import src.podlib as podlib
from src import m2_runner

MARKET_ID = "m2-alpha"
TOKENS = ("81001", "81002")


def stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def gamma_row(now: datetime, **changes) -> dict:
    row = {"id": MARKET_ID, "slug": MARKET_ID, "question": "Synthetic M2: will it happen?",
           "conditionId": "0xm2alpha", "clobTokenIds": json.dumps(list(TOKENS)), "outcomes": '["Yes", "No"]',
           "outcomePrices": '["0.4", "0.6"]', "liquidityNum": 52000, "volume24hr": 41000, "bestBid": 0.39,
           "bestAsk": 0.41, "spread": 0.02, "lastTradePrice": 0.4,
           "endDate": (now + timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ"), "active": True, "version": "v1",
           "closed": False, "acceptingOrders": True, "feesEnabled": False,
           "tags": [{"id": "9001", "slug": "e2e-synthetic", "label": "E2E synthetic"}]}
    row.update(changes)
    return row


def clob_book(token: str, now: datetime) -> dict:
    yes = token == TOKENS[0]
    bids = [("0.38", "900"), ("0.39", "500")] if yes else [("0.58", "900"), ("0.59", "500")]
    asks = [("0.42", "800"), ("0.41", "400")] if yes else [("0.62", "800"), ("0.61", "400")]
    return {"market": "0xm2alpha", "asset_id": token, "timestamp": str(int(now.timestamp() * 1000)),
            "bids": [{"price": p, "size": s} for p, s in bids], "asks": [{"price": p, "size": s} for p, s in asks],
            "tick_size": "0.01", "min_order_size": "5", "neg_risk": False}


class FakeGetters:
    """Records Gamma, independent /time and /book reads separately; no network."""

    def __init__(self, now: datetime, *, gamma_row_changes: dict | None = None, fail: bool = False) -> None:
        self.now = now
        self.row = gamma_row(now, **(gamma_row_changes or {}))
        self.gamma_urls: list[str] = []
        self.book_urls: list[str] = []
        self.time_calls: list[str] = []
        self.fail = fail

    def gamma(self, url: str):
        self.gamma_urls.append(url)
        if self.fail:
            raise AssertionError(f"gamma getter called: {url}")
        parts = urlsplit(url)
        if parts.path.endswith("/keyset"):
            return {"markets": [self.row], "next_cursor": ""}
        ids = parse_qs(parts.query).get("id") or []
        return [self.row] if ids == [MARKET_ID] else []

    def book(self, url: str):
        if url == "https://clob.polymarket.com/time":
            self.time_calls.append(url)
            if self.fail:
                raise AssertionError(f"time getter called: {url}")
            return self.now.timestamp()
        self.book_urls.append(url)
        if self.fail:
            raise AssertionError(f"book getter called: {url}")
        token = (parse_qs(urlsplit(url).query).get("token_id") or [""])[0]
        return clob_book(token, self.now)

    def getters(self) -> m2_runner.Getters:
        return m2_runner.Getters(gamma=self.gamma, book=self.book)

    @property
    def calls(self) -> int:
        return len(self.gamma_urls) + len(self.book_urls) + len(self.time_calls)


def isolate(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    paths = {"paper": tmp_path / "logs" / "paper.jsonl", "quotes": tmp_path / "logs" / "quotes.jsonl",
             "bus": tmp_path / "logs" / "bus.jsonl", "run": tmp_path / "runs" / "m2"}
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", paths["paper"])
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", paths["quotes"])
    monkeypatch.setattr(podlib, "BUS_PATH", paths["bus"])
    return paths


def inbox_row(now: datetime, **changes) -> dict:
    row = {"schema": "poly_forecast_inbox/1", "author": "Poly研判", "model_version": "poly-yanpan/2026-10-a",
           "market_id": MARKET_ID, "condition_id": "0xm2alpha", "outcome_index": 0, "outcome": "Yes",
           "token_id": TOKENS[0], "p": 0.62, "p_low": 0.55, "p_high": 0.68,
           "as_of": stamp(now - timedelta(minutes=10)),
           "sources": [{"url": "https://www.reuters.com/world/x", "title": "t", "kind": "rss"}],
           "rationale": "Blind estimate from public sources.", "market_price_seen": False}
    row.update(changes)
    return row


def deliver(run_dir: Path, now: datetime, name: str = "0001.json", **changes) -> Path:
    folder = run_dir / "inbox" / "pending" / now.strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps(inbox_row(now, **changes)), encoding="utf-8")
    return path


def run_once(run_dir: Path, fake: FakeGetters, **kwargs) -> int:
    return m2_runner.main(["--run-dir", str(run_dir), "--once"], getters=fake.getters(), **kwargs)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
