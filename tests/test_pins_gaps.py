"""Gaps in src/pins.py (Task AS pin checks): the fail-closed halves of ``record_drift``'s dedupe, the
explicit ``risk_path``/``stream`` arguments of ``refuse_start``, ``PinWatch``'s runtime halt and its
interval boundary, and the value/type boundaries of ``_same`` (what counts as a changed pin).

Everything here is offline: temp files only, no network, no orders, the real logs/ bus and halt state
are never reached (tests/conftest.py isolates the halt state; the bus is redirected below).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.halt_store as halt_store
import src.podlib as podlib
from src import pins

ROOT = Path(__file__).resolve().parents[1]
REAL = (ROOT / "config" / "risk.toml").read_bytes().replace(b"\r\n", b"\n")  # LF form


@pytest.fixture(autouse=True)
def _bus(tmp_path, monkeypatch):
    """Halt bus lines go to the test's folder: never the real logs/bus.jsonl."""
    monkeypatch.setattr(podlib, "BUS_PATH", tmp_path / "bus.jsonl")
    return tmp_path / "bus.jsonl"


def risk_file(tmp_path, data: bytes | str, name="risk.toml") -> Path:
    path = tmp_path / name
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def drifted(tmp_path, old: str, new: str, name="drift.toml") -> Path:
    text = REAL.decode("utf-8")
    assert text.count(old) == 1
    return risk_file(tmp_path, text.replace(old, new), name)


def halts():
    return [r for r in halt_store.active_halts() if r["reason"] == pins.PIN_HALT_REASON]


def bus_lines(bus) -> list[dict]:
    if not bus.exists():
        return []
    return [json.loads(line) for line in bus.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- _same: value and type boundaries

@pytest.mark.parametrize("old,new", [
    ("max_open_positions = 5", "max_open_positions = 5.0"),  # a float spelling of the pinned int is the same value
    ("max_spread = 0.04", "max_spread = 4e-2"),  # and so is an exponent spelling of the pinned float
])
def test_a_number_that_equals_the_pin_is_only_sha_drift(tmp_path, old, new):
    problems = pins.risk_file_problems(drifted(tmp_path, old, new))
    # the file is off its bytes, so it is drift -- but the key itself is not reported as changed
    assert problems == ["risk_sha256_not_pinned"]


@pytest.mark.parametrize("old,new,problem", [
    ("max_open_positions = 5", "max_open_positions = true", "max_open_positions=True expected 5"),
    ("max_open_positions = 5", "max_open_positions = 5.0000001", "max_open_positions=5.0000001 expected 5"),
    ("max_daily_loss_pct = 0.03", "max_daily_loss_pct = 0.05", "max_daily_loss_pct=0.05 expected 0.03"),
])
def test_a_bool_or_a_different_number_is_key_drift(tmp_path, old, new, problem):
    problems = pins.risk_file_problems(drifted(tmp_path, old, new))
    assert problem in problems and "risk_sha256_not_pinned" in problems


def test_a_renamed_key_is_missing_and_unknown_and_unknown_keys_are_sorted(tmp_path):
    path = drifted(tmp_path, "kelly_fraction = 0.15\n", "kelly = 0.15\nzebra = 1\nalpha = 1\n")
    problems = pins.risk_file_problems(path)
    assert "kelly_fraction_missing" in problems
    assert [item for item in problems if item.startswith("unknown_key:")] == ["unknown_key:alpha", "unknown_key:kelly",
                                                                              "unknown_key:zebra"]


def test_a_path_that_is_not_a_file_is_unreadable_not_a_crash(tmp_path):
    problems = pins.risk_file_problems(tmp_path)  # a directory, not a risk file
    assert len(problems) == 1 and problems[0].startswith("risk_toml_unreadable:")


# ---------------------------------------------------------------- refuse_start: explicit path and stream

def test_refuse_start_honours_the_explicit_path_and_stream(tmp_path, capsys):
    buf = tmp_path / "start.txt"
    # podlib.RISK_PATH still points at the pinned file: only the explicit path may be read
    with buf.open("w", encoding="utf-8") as handle:
        assert pins.refuse_start("scanner", risk_path=drifted(tmp_path, "mode = \"paper\"", "mode = \"live\""),
                                 stream=handle) != []
    assert "mode='live' expected 'paper'" in buf.read_text(encoding="utf-8")
    assert "refused to start" in buf.read_text(encoding="utf-8")
    assert capsys.readouterr().err == ""  # the given stream, not stderr
    assert len(halts()) == 1

    quiet = tmp_path / "quiet.txt"
    with quiet.open("w", encoding="utf-8") as handle:
        assert pins.refuse_start("scanner", risk_path=ROOT / "config" / "risk.toml", stream=handle) == []
    assert quiet.read_text(encoding="utf-8") == "" and len(halts()) == 1


# ---------------------------------------------------------------- PinWatch: runtime halt and interval

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_pinwatch_runtime_drift_records_phase_runtime_on_the_given_bus(tmp_path, capsys, _bus):
    path = drifted(tmp_path, "live_enabled = false", "live_enabled = true")
    watch = pins.PinWatch("ws_recorder", every_s=60, monotonic=Clock(), risk_path=path, bus_path=_bus)
    assert watch.drifted() is True and watch.checks == 1
    assert "live_enabled=True expected False" in watch.problems
    assert "stopping" in capsys.readouterr().err
    (record,) = halts()
    assert record["scope"] == halt_store.SCOPE_GLOBAL and record["reason"] == pins.PIN_HALT_REASON
    (line,) = bus_lines(_bus)
    assert line["type"] == "halt" and line["from"] == "Sentinel" and line["to"] == "Executor"
    assert line["payload"]["detected_by"] == "ws_recorder" and line["payload"]["phase"] == "runtime"
    assert line["payload"]["live_enabled"] is False and line["payload"]["exchange_action"] == "none"
    assert "live_enabled=True expected False" in line["payload"]["problems"]
    # one drift is one halt, however often the run asks again
    assert watch.drifted() is True and watch.checks == 1
    assert len(halts()) == 1 and len(bus_lines(_bus)) == 1


def test_pinwatch_zero_interval_still_rechecks_every_call(tmp_path):
    path = risk_file(tmp_path, REAL)
    watch = pins.PinWatch("m1_run", every_s=0, monotonic=Clock(), risk_path=path)
    assert [watch.drifted() for _ in range(3)] == [False, False, False]
    assert watch.checks == 3  # a zero interval checks every call, it never means "stop checking"
    path.write_bytes(REAL.replace(b"live_enabled = false", b"live_enabled = true"))
    assert watch.drifted() is True and watch.problems  # caught on the next call, no clock advance needed
    assert len(halts()) == 1


# ---------------------------------------------------------------- record_drift: the other two halts win

def test_record_drift_defers_to_an_active_fallback_halt(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", tmp_path / "HALT")
    halt_store.HALT_FALLBACK_PATH.write_text("earlier global halt\n", encoding="utf-8")
    problems = pins.refuse_start("scanner", risk_path=drifted(tmp_path, "max_spread = 0.04", "max_spread = 0.05"))
    assert "max_spread=0.05 expected 0.04" in problems
    assert "refused to start" in capsys.readouterr().err
    assert halt_store.fallback_active() is True
    assert halts() == []  # logs/HALT is already a global halt: no second record for the same drift
    assert halt_store.halt_problems("m1") == [halt_store.FALLBACK_ACTIVE]


def test_record_drift_defers_to_an_unreadable_halt_state(tmp_path, monkeypatch, capsys, _bus):
    state = tmp_path / "halt_state.json"
    state.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", state)
    problems = pins.refuse_start("scanner", risk_path=drifted(tmp_path, "max_order_pct = 0.01", "max_order_pct = 0.02"))
    assert "max_order_pct=0.02 expected 0.01" in problems
    assert "refused to start" in capsys.readouterr().err
    assert halts() == []  # the state file cannot be read: the drift is not recorded twice
    assert bus_lines(_bus) == []  # and no halt line is emitted over a state that is already a global halt
    assert halt_store.halt_problems("m1")  # fail closed: the unreadable state still halts everything
