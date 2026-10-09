"""Issue #55: a shared-bus write failure records one global halt at once. Offline, paper only.

A serialization or append failure on the bus (mkdir / open / write / close of the JSONL append) is
never published and never swallowed: podlib.bus records a global halt through halt_store.record_halt
(a direct call, never through the bus: no recursion) and raises the sanitized BusWriteError to the
caller. The diagnostic carries only the reason code, the error type and the role / message type -
never the payload, never the raw error text - and says truthfully where the halt landed ("none" when
even logs/HALT failed). An allow (signal) failure books nothing; a fill failure keeps the booked
fill (no rollback, no re-booking) and the global halt stops every later fill. conftest.py points the
halt paths at the test's own folder.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import src.halt_store as halt_store
import src.paper_ledger as paper_ledger
import src.podlib as podlib
from src.podlib import load_risk, read_jsonl, utc_now

RAW_ERROR_MARKER = "raw-error-marker-9d2c"
PAYLOAD_MARKER = "payload-marker-7a1f"


def _intent(market="m1", **kw):
    return paper_ledger._intent(market=market, model_prob=0.70, market_price=0.40, book_ts=utc_now(), **kw)


class _BrokenHandle:
    """A file object whose write or close fails (one bus-append step); otherwise proxies the real one."""

    def __init__(self, handle, fail_on):
        self._handle, self._fail_on = handle, fail_on

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def write(self, text):
        if self._fail_on == "write":
            raise OSError(RAW_ERROR_MARKER)
        return self._handle.write(text)

    def close(self):
        if self._fail_on == "close":
            self._handle.close()
            raise OSError(RAW_ERROR_MARKER)
        return self._handle.close()


def _fail_append_at(monkeypatch, bus_path: Path, step: str) -> None:
    """Fail exactly one step of the bus append (mkdir / open / write / close) and nothing else."""
    real_mkdir, real_open = Path.mkdir, Path.open

    def mkdir(self, *args, **kwargs):
        if step == "mkdir" and self == bus_path.parent:
            raise OSError(RAW_ERROR_MARKER)
        return real_mkdir(self, *args, **kwargs)

    def open(self, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if self == bus_path and "a" in mode and step in ("open", "write", "close"):
            if step == "open":
                raise OSError(RAW_ERROR_MARKER)
            return _BrokenHandle(real_open(self, *args, **kwargs), step)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "open", open)


# ---------------------------------------------------------------- the bus append step

@pytest.mark.parametrize("step", ["mkdir", "open", "write", "close"])
def test_a_bus_append_failure_halts_globally_and_raises(step, tmp_path, monkeypatch):
    bus_path = tmp_path / "busdir" / "bus.jsonl"
    _fail_append_at(monkeypatch, bus_path, step)
    with pytest.raises(podlib.BusWriteError) as info:
        podlib.bus("Executor", "Sentinel", "fill", "m1",
                   {"risk_decision": "allow", "note": PAYLOAD_MARKER}, path=bus_path)
    err = info.value
    assert (err.code, err.error_type, err.role, err.kind) == (
        podlib.BUS_WRITE_HALT_CODE, "OSError", "Executor->Sentinel", "fill")
    assert err.halt_persisted == halt_store.PERSISTED_STATE  # the state write took it
    halts = halt_store.active_halts()
    assert len(halts) == 1
    assert halts[0]["scope"] == "global" and halts[0]["market"] == "" and halts[0]["active"] is True
    assert halts[0]["source"] == "Executor"
    assert halts[0]["reason"] == f"{podlib.BUS_WRITE_HALT_CODE} error=OSError role=Executor->Sentinel type=fill"
    if step != "close":  # a close failure can leave the flushed line behind; the caller is told anyway
        assert read_jsonl(bus_path) == []  # nothing published, nothing pretended


def test_a_serialization_failure_halts_and_raises(tmp_path):
    class PayloadMarker7a1f:
        pass

    bus_path = tmp_path / "bus.jsonl"
    with pytest.raises(podlib.BusWriteError) as info:
        podlib.bus("Quant", "RiskGate", "signal", "m1", {"bad": PayloadMarker7a1f()}, path=bus_path)
    err = info.value
    assert (err.error_type, err.role, err.kind) == ("TypeError", "Quant->RiskGate", "signal")
    assert err.halt_persisted == halt_store.PERSISTED_STATE
    assert read_jsonl(bus_path) == []
    assert [h["reason"] for h in halt_store.active_halts()] == [
        f"{podlib.BUS_WRITE_HALT_CODE} error=TypeError role=Quant->RiskGate type=signal"]


def test_the_diagnostic_never_echoes_the_payload_or_the_raw_error(tmp_path, monkeypatch):
    bus_path = tmp_path / "busdir" / "bus.jsonl"
    _fail_append_at(monkeypatch, bus_path, "write")
    with pytest.raises(podlib.BusWriteError) as info:
        podlib.bus("Executor", "Sentinel", "fill", "m1",
                   {"risk_decision": "allow", "note": PAYLOAD_MARKER}, path=bus_path)
    err = info.value
    assert str(err) == ("bus_write_failed error=OSError role=Executor->Sentinel type=fill halt_persisted=state")
    assert err.__cause__ is None and err.__suppress_context__ is True  # the raw error is never chained
    record = halt_store.active_halts()[0]
    assert record["reason"] == "bus_write_failed error=OSError role=Executor->Sentinel type=fill"
    stored = halt_store.HALT_STATE_PATH.read_text(encoding="utf-8")
    for text in (str(err), record["reason"], stored):
        assert PAYLOAD_MARKER not in text and RAW_ERROR_MARKER not in text


def test_a_state_write_failure_falls_back_to_the_halt_file(tmp_path, monkeypatch):
    bus_path = tmp_path / "busdir" / "bus.jsonl"
    _fail_append_at(monkeypatch, bus_path, "open")
    halt_store.HALT_STATE_PATH.write_text("{bad", encoding="utf-8")  # the state file cannot take it
    with pytest.raises(podlib.BusWriteError) as info:
        podlib.bus("Executor", "Sentinel", "fill", "m1", {"risk_decision": "allow"}, path=bus_path)
    assert info.value.halt_persisted == halt_store.PERSISTED_FALLBACK  # the existing logs/HALT fallback
    assert halt_store.fallback_active()
    assert "bus_write_failed error=OSError role=Executor->Sentinel type=fill" in \
        halt_store.HALT_FALLBACK_PATH.read_text(encoding="utf-8")


def test_nothing_persisted_is_never_reported_as_a_stored_halt(tmp_path, monkeypatch):
    bus_path = tmp_path / "busdir" / "bus.jsonl"
    _fail_append_at(monkeypatch, bus_path, "open")
    halt_store.HALT_STATE_PATH.write_text("{bad", encoding="utf-8")
    monkeypatch.setattr(halt_store, "write_fallback", lambda *a, **k: False)  # even logs/HALT fails
    with pytest.raises(podlib.BusWriteError) as info:
        podlib.bus("Executor", "Sentinel", "fill", "m1", {"risk_decision": "allow"}, path=bus_path)
    assert info.value.halt_persisted == "none"
    assert str(info.value).endswith("halt_persisted=none")
    assert "halt_persisted=state" not in str(info.value)
    assert "halt_persisted=fallback" not in str(info.value)


def test_a_broken_halt_recorder_still_raises_with_halt_persisted_none(tmp_path, monkeypatch):
    bus_path = tmp_path / "busdir" / "bus.jsonl"
    _fail_append_at(monkeypatch, bus_path, "open")

    def broken_record(**kwargs):
        raise OSError(RAW_ERROR_MARKER)

    monkeypatch.setattr(halt_store, "record_halt", broken_record)
    with pytest.raises(podlib.BusWriteError) as info:
        podlib.bus("Executor", "Sentinel", "fill", "m1", {"risk_decision": "allow"}, path=bus_path)
    assert info.value.halt_persisted == "none"
    assert RAW_ERROR_MARKER not in str(info.value)


def test_the_halt_is_recorded_without_re_entering_the_bus(tmp_path, monkeypatch):
    attempts, recorded = [], []
    real_record = halt_store.record_halt

    def spy(**kwargs):
        recorded.append(kwargs)
        return real_record(**kwargs)

    def broken(path, row):
        attempts.append(row.get("type"))
        raise OSError(RAW_ERROR_MARKER)

    monkeypatch.setattr(halt_store, "record_halt", spy)
    monkeypatch.setattr(podlib, "append_jsonl", broken)
    with pytest.raises(podlib.BusWriteError):
        podlib.bus("Executor", "Sentinel", "fill", "m1", {"risk_decision": "allow"}, path=tmp_path / "bus.jsonl")
    assert attempts == ["fill"]  # the halt never goes through bus(): one append attempt, no recursion
    assert [(r["scope"], r["source"]) for r in recorded] == [("global", "Executor")]
    assert halt_store.halt_problems("m2")  # and the recorded halt is in force


def test_the_success_path_is_unchanged(tmp_path):
    bus_path = tmp_path / "bus.jsonl"
    message = podlib.bus("Executor", "Sentinel", "fill", "m1", {"risk_decision": "allow"}, path=bus_path)
    assert message["type"] == "fill" and message["market"] == "m1"
    assert read_jsonl(bus_path) == [message]
    assert halt_store.active_halts() == []


# ---------------------------------------------------------------- the Executor's allow and fill

def _ledger_env(tmp_path, monkeypatch):
    """The real bus and the real paper ledger, both under tmp_path."""
    monkeypatch.setattr(podlib, "BUS_PATH", tmp_path / "bus.jsonl")
    monkeypatch.setattr(paper_ledger, "PAPER_PATH", tmp_path / "paper.jsonl")
    monkeypatch.setattr(paper_ledger, "QUOTES_PATH", tmp_path / "quotes.jsonl")


def _fail_kind(monkeypatch, kind: str):
    """The real append, except one message type fails like a dead disk. Returns the types seen."""
    calls, real = [], podlib.append_jsonl

    def append(path, row):
        calls.append(row.get("type"))
        if row.get("type") == kind:
            raise OSError(RAW_ERROR_MARKER)
        return real(path, row)

    monkeypatch.setattr(podlib, "append_jsonl", append)
    return calls


def test_a_failed_allow_message_halts_and_books_nothing(tmp_path, monkeypatch):
    _ledger_env(tmp_path, monkeypatch)
    _fail_kind(monkeypatch, "signal")
    with pytest.raises(podlib.BusWriteError) as info:
        paper_ledger.commit_if_allowed(_intent("m1"), load_risk())
    assert (info.value.role, info.value.kind) == ("RiskGate->Executor", "signal")
    assert info.value.halt_persisted == halt_store.PERSISTED_STATE
    assert read_jsonl(podlib.BUS_PATH) == []  # no allow was published
    assert read_jsonl(paper_ledger.PAPER_PATH) == []  # and nothing was booked
    assert [h["scope"] for h in halt_store.active_halts()] == ["global"]
    # the global halt rejects a later intent on another market
    later = paper_ledger.commit_if_allowed(_intent("m2"), load_risk())
    assert later["allow"] is False
    assert any(r.startswith("halt_global:") for r in later["decision"]["reasons"])
    assert read_jsonl(paper_ledger.PAPER_PATH) == []


def test_a_failed_fill_message_keeps_the_booking_and_blocks_later_fills(tmp_path, monkeypatch):
    _ledger_env(tmp_path, monkeypatch)
    _fail_kind(monkeypatch, "fill")
    with pytest.raises(podlib.BusWriteError) as info:
        paper_ledger.commit_if_allowed(_intent("m1"), load_risk())
    assert (info.value.role, info.value.kind) == ("Executor->Sentinel", "fill")
    booked = read_jsonl(paper_ledger.PAPER_PATH)
    assert len(booked) == 1 and booked[0]["market"] == "m1"  # kept exactly as booked: no rollback
    assert [row.get("type") for row in read_jsonl(podlib.BUS_PATH)] == ["signal"]  # the fill was not published
    # the global halt stops every later fill: another market, then this one again (never re-booked)
    for market in ("m2", "m1"):
        later = paper_ledger.commit_if_allowed(_intent(market), load_risk())
        assert later["allow"] is False
        assert any(r.startswith("halt_global:") for r in later["decision"]["reasons"])
    assert read_jsonl(paper_ledger.PAPER_PATH) == booked
    assert [row.get("type") for row in read_jsonl(podlib.BUS_PATH)].count("fill") == 0
