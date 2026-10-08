"""Role stub tests. Bus lines only. No network and no order SDK."""
from __future__ import annotations

import inspect

import src.arb_binary as arb_binary
import src.forecaster as forecaster
import src.maker as maker
import src.sentinel as sentinel
from src.podlib import read_jsonl


def test_modules_import_and_do_not_call_the_network():
    for module in (arb_binary, maker, forecaster, sentinel):
        source = inspect.getsource(module)
        assert "urlopen" not in source
        assert "clob.polymarket" not in source
        assert "py_clob_client" not in source


def test_arb_binary_signals_a_gap_and_never_fills(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    skipped = arb_binary.emit_gap(0.50, 0.50, "m-no", bus_path=bus_path)
    assert skipped is None
    assert not bus_path.exists()
    message = arb_binary.emit_gap(0.40, 0.40, "m-gap", bus_path=bus_path)
    assert message is not None
    assert message["from"] == "ArbBinary"
    assert message["type"] == "signal"
    assert message["payload"]["locked_profit"] is False
    assert message["payload"]["one_leg_meaning"] == "directional"
    assert read_jsonl(bus_path)[0]["type"] != "fill"


def test_forecaster_emits_probability_only(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    message = forecaster.emit_probability(market="m1", model_prob=0.42, bus_path=bus_path)
    assert message["from"] == "Forecaster"
    assert message["type"] == "signal"
    assert message["payload"]["order"] is False
    try:
        forecaster.emit_probability(market="m1", model_prob=1.0, bus_path=bus_path)
    except ValueError:
        pass
    else:
        raise AssertionError("probability of 1 should be rejected")
    assert len(read_jsonl(bus_path)) == 1
    assert "\"fill\"" not in inspect.getsource(forecaster)


def test_maker_records_a_paper_quote_and_rejects_size_up(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    quotes_path = tmp_path / "quotes.jsonl"
    message = maker.quote(
        market="m1",
        token_id="token",
        side="buy",
        price=0.40,
        size=10,
        bus_path=bus_path,
        quotes_path=quotes_path,
    )
    assert message["from"] == "Maker"
    assert message["type"] == "signal"
    assert message["payload"]["venue"] == "paper"
    assert message["payload"]["exchange_action"] == "none"
    assert message["payload"]["status"] == "open"
    try:
        maker.quote(
            market="m1",
            token_id="token",
            side="buy",
            price=0.50,
            size=100,
            bus_path=bus_path,
            quotes_path=quotes_path,
        )
    except ValueError as exc:
        assert "1%" in str(exc)
    else:
        raise AssertionError("oversized quote should be rejected")
    assert len(read_jsonl(quotes_path)) == 1


def test_sentinel_halt_cancels_paper_quotes_only(tmp_path):
    quotes_path = tmp_path / "quotes.jsonl"
    bus_path = tmp_path / "bus.jsonl"
    maker.quote(
        market="m1",
        token_id="a",
        side="buy",
        price=0.40,
        size=5,
        bus_path=bus_path,
        quotes_path=quotes_path,
    )
    maker.quote(
        market="m2",
        token_id="b",
        side="sell",
        price=0.60,
        size=5,
        bus_path=bus_path,
        quotes_path=quotes_path,
    )
    message = sentinel.halt(reason="stale book", market="m1", bus_path=bus_path, quotes_path=quotes_path)
    assert message["from"] == "Sentinel"
    assert message["type"] == "halt"
    assert message["payload"]["exchange_action"] == "none"
    assert message["payload"]["paper_quotes_cancelled"] == 1
    rows = read_jsonl(quotes_path)
    by_market = {row["market"]: row["status"] for row in rows}
    assert by_market["m1"] == "cancelled"
    assert by_market["m2"] == "open"
    kinds = {row["type"] for row in read_jsonl(bus_path)}
    assert "fill" not in kinds
    assert "halt" in kinds
