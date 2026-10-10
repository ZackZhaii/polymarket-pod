"""Read-side fail-closed branches of src/halt_store.py that the halt-consumer suite does not pin.

test_halt_consumer.py covers the veto path (halt_problems / active_halts) and the write path
(record_halt, resume). The reading primitives underneath them were never called by name:
``validate_state`` (the strict record rule), ``read_state``'s marker semantics, the tri-state
``has_active_halt`` (True / False / None — a bad state file must never read as "no halt"),
``global_halt_active`` (global reasons only, never a market halt) and ``fallback_active``'s
fail-closed stat. Also pins that the module defaults are read at call time, which is what
tests/conftest.py and the e2e gate rely on to keep the real logs/ untouched.

Offline: temp paths only, no bus, no network, no real logs/.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

import src.halt_store as halt_store

VALID: dict[str, Any] = {"id": "H0001-abc", "scope": "global", "market": "", "reason": "drill",
                         "source": "Sentinel", "ts": "2026-10-10T12:00:00Z", "active": True}
RESOLVED: dict[str, Any] = {"ts": "2026-10-10T13:00:00Z", "approved_by": "op", "root_cause": "x",
                            "postmortem": "y"}


def write_state(path: Path, data: object) -> Path:
    import json

    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------- read_state (marker semantics)


def test_a_first_run_is_clean_but_a_lost_file_after_the_marker_is_not(tmp_path):
    path = tmp_path / "halt_state.json"
    fallback = tmp_path / "HALT"
    assert halt_store.read_state(path) == ([], None)  # nothing has ever been written here
    assert not halt_store.state_marker(path).exists()

    record = halt_store.record_halt(scope="global", reason="drill", source="Sentinel",
                                    path=path, fallback=fallback)
    assert record["persisted"] == halt_store.PERSISTED_STATE
    assert halt_store.state_marker(path).exists()

    path.unlink()  # the marker stays: this is a lost file, not a first run
    assert halt_store.read_state(path) == ([], halt_store.STATE_MISSING)
    assert halt_store.halt_problems("m1", path, fallback) == [halt_store.STATE_MISSING]


@pytest.mark.parametrize(("content", "problem"), [
    (b"{", halt_store.STATE_CORRUPT),
    (b"", halt_store.STATE_CORRUPT),
    (b"\xff\xfe not utf-8", halt_store.STATE_UNREADABLE),
    (b"[]", halt_store.STATE_MALFORMED),
    (b'{"version": 2, "halts": []}', halt_store.STATE_MALFORMED),
    (b'{"version": 1}', halt_store.STATE_MALFORMED),
    (b'{"version": 1, "halts": {}}', halt_store.STATE_MALFORMED),
    (b'{"version": 1, "halts": [{"id": "H1"}]}', halt_store.STATE_MALFORMED),
    (b'{"version": 1, "halts": [{"id": "halt-file", "scope": "global", "market": "", "reason": "r",'
     b' "source": "s", "ts": "t", "active": true}]}', halt_store.STATE_MALFORMED),
])
def test_every_unreadable_state_shape_gets_its_own_problem(tmp_path, content, problem):
    path = tmp_path / "halt_state.json"
    path.write_bytes(content)
    assert halt_store.read_state(path) == ([], problem)
    # and it is a global veto, never a silent "no halt"
    assert halt_store.halt_problems("m1", path, tmp_path / "HALT") == [problem]


def test_a_directory_state_path_is_unreadable(tmp_path):
    path = tmp_path / "halt_state.json"
    path.mkdir()
    assert halt_store.read_state(path) == ([], halt_store.STATE_UNREADABLE)


# ---------------------------------------------------------------- validate_state (strict records)


@pytest.mark.parametrize("record", [
    {"id": "H1"},                                                              # no scope at all
    dict(VALID, scope="planet"),                                               # unknown scope
    dict(VALID, scope="market", market=""),                                    # market halt, no market
    dict(VALID, scope="market", market=None),                                  # market halt, non-string
    dict(VALID, market="m1"),                                                  # global halt with a market
    dict(VALID, active="true"),                                                # non-bool active
    dict(VALID, active=False),                                                 # released without a record
    dict(VALID, active=False, resolved="op"),                                  # released, resolved not a dict
    dict(VALID, id=""),                                                        # blank id
    dict(VALID, id=halt_store.FALLBACK_ID),                                    # a synthetic id is never stored
    dict(VALID, id=halt_store.STATE_PROBLEM_ID),
    dict(VALID, id=halt_store.SENTINEL_LOST_ID),
    dict(VALID, reason=""),                                                    # blank reason
    dict(VALID, source=None),
    dict(VALID, ts=None),
    "not-a-record",
])
def test_validate_state_rejects_any_bad_record(record):
    with pytest.raises(ValueError):
        halt_store.validate_state({"version": 1, "halts": [record]})


@pytest.mark.parametrize("data", [
    None,
    [],
    "version 1",
    {},
    {"version": 0, "halts": []},
    {"version": "1", "halts": []},
    {"version": 1, "halts": None},
])
def test_validate_state_rejects_any_bad_envelope(data):
    with pytest.raises(ValueError):
        halt_store.validate_state(data)


def test_validate_state_rejects_a_repeated_id_and_keeps_a_valid_one():
    with pytest.raises(ValueError):
        halt_store.validate_state({"version": 1, "halts": [VALID, dict(VALID)]})
    released = dict(VALID, id="H0002-def", active=False, resolved=RESOLVED)
    assert halt_store.validate_state({"version": 1, "halts": [VALID, released]}) == [VALID, released]


# ---------------------------------------------------------------- has_active_halt (tri-state)


def test_has_active_halt_is_tri_state_and_a_bad_state_is_not_no_halt(tmp_path):
    path = tmp_path / "halt_state.json"
    fallback = tmp_path / "HALT"
    assert halt_store.has_active_halt(scope="global", path=path) is False  # a true first run

    halt_store.record_halt(scope="global", reason="risk_toml_pin_drift", source="Sentinel",
                           path=path, fallback=fallback)
    assert halt_store.has_active_halt(scope="global", reason_prefix="risk_toml_pin_drift",
                                      path=path) is True
    assert halt_store.has_active_halt(scope="global", reason_prefix="stale_book", path=path) is False

    halt_store.record_halt(scope="market", market="m1", reason="stale_book", source="Sentinel",
                           path=path, fallback=fallback)
    assert halt_store.has_active_halt(scope="market", market="m1", path=path) is True
    assert halt_store.has_active_halt(scope="market", market="m2", path=path) is False
    assert halt_store.has_active_halt(scope="market", path=path) is False  # no market: nothing matches

    path.write_text("{oops", encoding="utf-8")
    assert halt_store.has_active_halt(scope="global", path=path) is None  # unreadable: not False
    path.unlink()  # the marker exists, so this is a lost file: still not "no halt"
    assert halt_store.has_active_halt(scope="global", path=path) is None


def test_has_active_halt_ignores_a_released_record(tmp_path):
    path = write_state(tmp_path / "halt_state.json",
                       {"version": 1, "halts": [VALID, dict(VALID, id="H0002-def", active=False,
                                                            resolved=RESOLVED)]})
    assert halt_store.read_state(path) == ([VALID, dict(VALID, id="H0002-def", active=False,
                                                        resolved=RESOLVED)], None)
    assert halt_store.has_active_halt(scope="global", reason_prefix="drill", path=path) is True  # VALID
    assert halt_store.has_active_halt(scope="global", reason_prefix="", path=path) is True
    # only the released record is left: nothing is in force
    write_state(path, {"version": 1, "halts": [dict(VALID, active=False, resolved=RESOLVED)]})
    assert halt_store.has_active_halt(scope="global", path=path) is False


# ---------------------------------------------------------------- global_halt_active


def test_global_halt_active_never_reports_a_market_halt(tmp_path):
    path = tmp_path / "halt_state.json"
    fallback = tmp_path / "HALT"
    record = halt_store.record_halt(scope="market", market="m1", reason="stale_book",
                                    source="Sentinel", path=path, fallback=fallback)
    assert halt_store.halt_problems("m1", path, fallback) == [f"halt_market:{record['id']}"]
    assert halt_store.halt_problems("m2", path, fallback) == []
    assert halt_store.global_halt_active(path, fallback) == []

    path.write_text("{oops", encoding="utf-8")
    assert halt_store.global_halt_active(path, fallback) == [halt_store.STATE_CORRUPT]
    assert halt_store.halt_problems("m1", path, fallback) == [halt_store.STATE_CORRUPT]

    assert halt_store.write_fallback("bus down", fallback)
    assert halt_store.global_halt_active(path, fallback) == [halt_store.STATE_CORRUPT,
                                                             halt_store.FALLBACK_ACTIVE]


# ---------------------------------------------------------------- fallback_active and the call-time paths


def test_fallback_active_fails_closed_when_the_check_itself_breaks(tmp_path, monkeypatch):
    target = tmp_path / "HALT"
    assert halt_store.fallback_active(target) is False
    target.write_text("bus down\n", encoding="utf-8")
    assert halt_store.fallback_active(target) is True

    real_stat = os.stat

    def broken(path, *args, **kwargs):
        if str(path) == str(target):
            raise PermissionError("denied")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(halt_store.os, "stat", broken)
    assert halt_store.fallback_active(target) is True  # cannot be checked: fail closed


def test_the_module_paths_are_read_at_call_time(tmp_path, monkeypatch):
    base = tmp_path / "logs"
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", base / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", base / "HALT")
    assert halt_store.state_path() == base / "halt_state.json"
    assert halt_store.fallback_path() == base / "HALT"
    assert halt_store.state_lock_path() == base / "halt_state.json.lock"
    given = tmp_path / "given.json"
    assert halt_store.state_path(given) == given
    assert halt_store.fallback_path(given) == given
    assert halt_store.state_lock_path(given) == tmp_path / "given.json.lock"
