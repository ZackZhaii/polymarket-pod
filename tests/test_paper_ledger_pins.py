"""Issue #63: the paper_ledger CLI checks the whole risk file before it reads it.

``paper_ledger.main`` used to read config/risk.toml and only compare its bytes before and after the run,
so a file that had already drifted was accepted as long as it stayed byte-identical while the run was on.
RiskGate covers just the five in-memory ``podlib.PINS``, so a changed ``max_open_positions`` (5 -> 6)
still booked a sixth leg on a full book. Both entry points (an operator intent and the default
self-check) now call ``pins.refuse_start`` right after argument parsing: the existing ``EXIT_FAILED``
refusal, one recorded global halt, no fill and no self-check. ``evaluate(cfg)`` still takes an in-memory
config, so the pure-RiskGate tests keep injecting one.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.halt_store as halt_store
import src.paper_ledger as paper_ledger
import src.podlib as podlib
from src import pins

ROOT = Path(__file__).resolve().parents[1]
REAL = (ROOT / "config" / "risk.toml").read_bytes().replace(b"\r\n", b"\n")  # LF form (Windows checkouts are CRLF)


@pytest.fixture(autouse=True)
def ledger_paths(monkeypatch, tmp_path, bus):
    """No test may reach the real logs/ ledger, quotes or bus through paper_ledger's own names."""
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", tmp_path / "paper.jsonl")
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")
    monkeypatch.setattr(paper_ledger, "BOOKS_PATH", tmp_path / "books.jsonl")
    monkeypatch.setattr(paper_ledger, "MARKETS_PATH", tmp_path / "markets.jsonl")
    monkeypatch.setattr(paper_ledger, "bus", lambda *args, **kwargs: None)
    return tmp_path


@pytest.fixture
def bus(monkeypatch, tmp_path):
    path = tmp_path / "bus.jsonl"
    monkeypatch.setattr(podlib, "BUS_PATH", path)
    return path


@pytest.fixture
def booked(monkeypatch):
    """Tripwire: issue #63 asserts the ledger write (ledger_store.book) is never called."""
    fills: list[dict] = []

    def record(path, fill):
        fills.append(fill)
        return fill

    monkeypatch.setattr(paper_ledger.ledger_store, "book", record)
    return fills


def drift(tmp_path, old: str, new: str, name: str = "drift.toml") -> Path:
    text = REAL.decode("utf-8")
    assert text.count(old) == 1, old
    path = tmp_path / name
    path.write_bytes(text.replace(old, new).encode("utf-8"))
    return path


def operator_args() -> list[str]:
    return ["--strategy", "issue63", "--market", "m", "--token-id", "t", "--model-prob", "0.7",
            "--market-price", "0.4", "--liquidity", "20000", "--spread", "0.01", "--book-ts", podlib.utc_now(),
            "--tags", "test-market",  # issue #131: an untagged market vetoes missing_category
            "--min-order-size", "5"]  # issue #226: no book minimum vetoes missing_min_order_size


def halts() -> list[dict]:
    return [row for row in halt_store.active_halts() if row["reason"] == pins.PIN_HALT_REASON]


def bus_lines(bus: Path) -> list[dict]:
    return [json.loads(row) for row in bus.read_text(encoding="utf-8").splitlines()] if bus.exists() else []


# ---------------------------------------------------------------- the issue's case: operator intent, full book

def test_a_cap_drift_refuses_the_intent_before_a_sixth_leg(tmp_path, monkeypatch, capsys, bus, booked):
    """max_open_positions 5 -> 6 in the file with five legs already open: no sixth leg is booked."""
    monkeypatch.setattr(podlib, "RISK_PATH", drift(tmp_path, "max_open_positions = 5", "max_open_positions = 6"))
    monkeypatch.setattr(paper_ledger, "load_state", lambda market, token_id="": {
        "open_positions": 5, "daily_loss_usd": 0.0, "leg_already_open": False, "recent_fills": [], "own_quotes": []})

    code = paper_ledger.main(operator_args())

    assert code == paper_ledger.EXIT_FAILED == 1
    assert booked == [] and not paper_ledger.PAPER_PATH.exists()
    captured = capsys.readouterr()
    assert "max_open_positions=6 expected 5" in captured.err and "risk_sha256_not_pinned" in captured.err
    assert "ALLOW" not in captured.out
    (record,) = halts()
    assert record["scope"] == halt_store.SCOPE_GLOBAL and record["source"] == "Sentinel"
    (line,) = bus_lines(bus)
    assert line["type"] == "halt" and line["payload"]["detected_by"] == "paper_ledger"
    assert line["payload"]["phase"] == "start" and line["payload"]["exchange_action"] == "none"


# ---------------------------------------------------------------- the default self-check entry point

def test_the_default_self_check_refuses_on_drift(tmp_path, monkeypatch, booked):
    monkeypatch.setattr(podlib, "RISK_PATH", drift(tmp_path, "kelly_fraction = 0.15", "kelly_fraction = 0.25"))

    def boom(*args, **kwargs):
        raise AssertionError("the self-check ran on a drifted risk file")

    monkeypatch.setattr(paper_ledger, "run_self_check", boom)
    monkeypatch.setattr(paper_ledger, "realistic_no_edge", boom)

    assert paper_ledger.main([]) == paper_ledger.EXIT_FAILED == 1
    assert booked == [] and len(halts()) == 1


# ---------------------------------------------------------------- every other file key

@pytest.mark.parametrize("old,new,problem", [
    ("min_liquidity_usd = 10000", "min_liquidity_usd = 5000", "min_liquidity_usd=5000 expected 10000"),
    ("max_spread = 0.04", "max_spread = 0.05", "max_spread=0.05 expected 0.04"),
    ("kelly_fraction = 0.15", "kelly_fraction = 0.25", "kelly_fraction=0.25 expected 0.15"),
    ("max_open_positions = 5", "max_open_positions = 5.5", "max_open_positions=5.5 expected 5"),
])
def test_any_other_file_key_drift_refuses(tmp_path, monkeypatch, capsys, booked, old, new, problem):
    monkeypatch.setattr(podlib, "RISK_PATH", drift(tmp_path, old, new))
    assert paper_ledger.main(operator_args()) == paper_ledger.EXIT_FAILED == 1
    assert problem in capsys.readouterr().err
    assert booked == [] and len(halts()) == 1


# ---------------------------------------------------------------- missing and corrupt files

@pytest.mark.parametrize("kind,problem", [
    ("missing", "risk_toml_unreadable:FileNotFoundError"),
    ("corrupt", "risk_toml_unparseable:UnicodeDecodeError"),
])
def test_a_missing_or_corrupt_file_refuses(kind, problem, tmp_path, monkeypatch, capsys, booked):
    path = tmp_path / "risk.toml"
    if kind == "corrupt":
        path.write_bytes(b"\xff\xfe not utf-8")
    monkeypatch.setattr(podlib, "RISK_PATH", path)

    assert paper_ledger.main(operator_args()) == paper_ledger.EXIT_FAILED == 1
    assert problem in capsys.readouterr().err
    assert booked == [] and len(halts()) == 1


def test_a_corrupt_file_refuses_the_self_check_too(tmp_path, monkeypatch, booked):
    path = tmp_path / "risk.toml"
    path.write_bytes(b"\xff\xfe not utf-8")
    monkeypatch.setattr(podlib, "RISK_PATH", path)
    monkeypatch.setattr(paper_ledger, "run_self_check",
                        lambda cfg: (_ for _ in ()).throw(AssertionError("the self-check ran")))
    assert paper_ledger.main([]) == paper_ledger.EXIT_FAILED == 1
    assert booked == [] and len(halts()) == 1


# ---------------------------------------------------------------- the pinned file keeps both flows

def test_the_pinned_file_keeps_both_flows(capsys):
    # Issue #210: operator CLI without --fee-rate / book levels refuses (VETO), still EXIT_OK with reasons.
    assert paper_ledger.main(operator_args()) == paper_ledger.EXIT_OK
    out = capsys.readouterr().out
    assert "ALLOW" not in out
    assert "VETO" in out and "missing_book_levels" in out
    assert paper_ledger.main([]) == paper_ledger.EXIT_OK
    out = capsys.readouterr().out
    assert "self_check pin_blocked=True" in out and "risk_toml_unchanged=true" in out
    assert halts() == []
