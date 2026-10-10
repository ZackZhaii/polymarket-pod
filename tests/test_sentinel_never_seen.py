"""Issue #129: a resident Sentinel that has never run here is fail closed, not "no halt".

No Sentinel heartbeat file and no ``.seen`` marker used to read as "nothing to lose yet", so RiskGate,
the Maker and paper_cycle acted with no Sentinel at all. Now it is ``sentinel_heartbeat_never_seen``:
a global halt for every consumer. It is synthetic (never stored), so it needs no Operator resume and
ends with the Sentinel's first fresh heartbeat. A fresh, stale, corrupt or stopped Sentinel behaves as
before. conftest.py gives every test a fresh resident Sentinel; ``no_sentinel`` removes it.
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone

import pytest

import src.halt_store as halt_store
import src.heartbeat as heartbeat
import src.maker as maker
from src.podlib import RISK_PATH, read_jsonl
from tests.test_halt_consumer import _cycle, _veto

NEVER = halt_store.SENTINEL_NEVER_SEEN


def _write_sentinel(*, ts: datetime | None = None, state: str = "running", exit_code: int | None = None) -> None:
    beat = heartbeat.Heartbeat("sentinel", every_s=30.0, now=lambda: ts or datetime.now(timezone.utc))
    assert beat.beat(state, exit_code=exit_code, force=True) is None


# ---------------------------------------------------------------- the never-started Sentinel

def test_the_fixture_starts_every_test_with_a_fresh_sentinel():
    assert heartbeat.beat_path("sentinel").exists()
    assert halt_store.sentinel_heartbeat_problem() is None
    assert halt_store.halt_problems("m1") == []


def test_no_heartbeat_and_no_marker_is_never_seen(no_sentinel):
    assert NEVER == "sentinel_heartbeat_never_seen"
    assert halt_store.sentinel_heartbeat_problem() == NEVER
    assert halt_store.halt_problems("m1") == [NEVER]
    assert halt_store.halt_problems("") == [NEVER]
    assert halt_store.global_halt_active() == [NEVER]


def test_never_seen_is_a_global_halt_that_is_not_stored(no_sentinel):
    active = halt_store.active_halts()
    assert [(h["scope"], h["reason"], h["source"]) for h in active] == [("global", NEVER, "halt_store")]
    assert not halt_store.HALT_STATE_PATH.exists()
    assert not halt_store.state_marker(halt_store.HALT_STATE_PATH).exists()
    assert not halt_store.HALT_FALLBACK_PATH.exists()
    assert halt_store.has_active_halt(scope="global", reason_prefix="sentinel_heartbeat_") is False


def test_riskgate_vetoes_every_market(no_sentinel):
    for market in ("m1", "m2"):
        assert NEVER in _veto(market)


def test_maker_refuses(tmp_path, no_sentinel, fresh_book):
    fresh_book("m1", "t")
    with pytest.raises(maker.HaltActive):
        maker.quote(market="m1", token_id="t", side="buy", price=0.4, size=5,
                    bus_path=tmp_path / "bus.jsonl", quotes_path=tmp_path / "q.jsonl")
    assert read_jsonl(tmp_path / "q.jsonl") == []
    assert not (tmp_path / "bus.jsonl").exists()


def test_paper_cycle_emits_no_gap(tmp_path, no_sentinel):
    result = _cycle(tmp_path)
    assert result["gaps"] == 0
    assert result["halted_markets"] == ["m1", "m2"]
    assert result["skips"][0]["halt_reasons"] == [NEVER]


def test_the_first_fresh_beat_ends_it_without_a_resume(tmp_path, no_sentinel, sentinel_beat):
    assert NEVER in _veto("m1")
    sentinel_beat()
    assert halt_store.halt_problems("m1") == []
    assert _veto("m1") == []
    assert _cycle(tmp_path)["gaps"] == 2
    assert not halt_store.HALT_STATE_PATH.exists()  # nothing was stored, so nothing needs resuming


def test_a_marker_without_a_heartbeat_is_still_a_lost_sentinel_and_stored(no_sentinel):
    path = heartbeat.beat_path("sentinel")
    path.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.state_marker(path).write_text("1\n", encoding="utf-8")
    assert halt_store.sentinel_heartbeat_problem() == "sentinel_heartbeat_missing"
    assert [h["reason"] for h in halt_store.active_halts()] == ["sentinel_heartbeat_missing"]
    assert halt_store.has_active_halt(scope="global", reason_prefix="sentinel_heartbeat_missing") is True


# ---------------------------------------------------------------- existing behaviour unchanged

@pytest.mark.parametrize("state", ["starting", "running", "paused"])
def test_a_fresh_beat_is_unchanged(state):
    _write_sentinel(state=state)
    assert halt_store.sentinel_heartbeat_problem() is None
    assert halt_store.halt_problems("m1") == []


def test_a_stale_beat_is_unchanged():
    _write_sentinel(ts=datetime.now(timezone.utc) - timedelta(seconds=120))
    assert halt_store.sentinel_heartbeat_problem() == "sentinel_heartbeat_stale"
    assert any(r.startswith("halt_global:") or r == "sentinel_heartbeat_stale" for r in halt_store.halt_problems("m1"))
    records = json.loads(halt_store.HALT_STATE_PATH.read_text(encoding="utf-8"))["halts"]
    assert [r["reason"] for r in records] == ["sentinel_heartbeat_stale"]  # stored, as before


@pytest.mark.parametrize("state,code", [("finished", 0), ("crashed", None)])
def test_a_stopped_sentinel_is_unchanged(state, code):
    _write_sentinel(state=state, exit_code=code)
    assert halt_store.sentinel_heartbeat_problem() == "sentinel_heartbeat_stopped"


def test_a_corrupt_heartbeat_is_unchanged():
    heartbeat.beat_path("sentinel").write_text("{oops", encoding="utf-8")
    assert halt_store.sentinel_heartbeat_problem() == "sentinel_heartbeat_corrupt"


def test_a_broken_check_is_still_check_failed(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("boom")

    monkeypatch.setattr(heartbeat, "read_beat", boom)
    assert halt_store.sentinel_heartbeat_problem() == "sentinel_heartbeat_check_failed"


# ---------------------------------------------------------------- no switch

def test_there_is_no_switch_that_turns_it_off(no_sentinel, monkeypatch):
    source = inspect.getsource(halt_store.sentinel_heartbeat_problem) + inspect.getsource(halt_store.active_halts)
    assert "environ" not in source and "getenv" not in source
    for name in ("POD_SENTINEL_OPTIONAL", "SENTINEL_OPTIONAL", "POD_TEST", "PYTEST_CURRENT_TEST"):
        monkeypatch.setenv(name, "1")
    assert halt_store.halt_problems("m1") == [NEVER]


def test_risk_file_untouched(no_sentinel):
    before = RISK_PATH.read_bytes()
    _veto("m1")
    assert RISK_PATH.read_bytes() == before
