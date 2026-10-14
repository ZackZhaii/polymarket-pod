"""Issue #305 (Task DR): paper_session runs the pin check first; kill_alert turns every Exception into exit 4.

Tightening only (Poly风控 non-blocking items from the #292 / #295 reviews). paper_session.run checks the risk file
with ``pins.refuse_start`` before it reads risk.toml at all, so a missing file, a directory, or a sha computation
that raises is ``risk_toml_pin_drift`` with a global halt record and ``ok=False`` -- never an uncaught exception --
and the drift check and the unchanged-file guard read the same path. ``m2_runner.kill_alert`` converts any
``Exception`` (not KeyboardInterrupt / SystemExit) into ``WriteFailed`` (exit 4). Offline: getters fail the test.

Task DS adds section 3: the reread-then-refuse branch. The real pin check passes, then the file is deleted,
edited, swapped for a directory, or the reread raises, before ``_pinned_snapshot`` reads it again: still
``risk_toml_pin_drift`` with one global halt, ``ok=False``, nothing written, no getter called.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.halt_store as halt_store
import src.heartbeat as heartbeat
import src.paper_session as paper_session
from src import m2_runner, pins, podlib
from src.podlib import RISK_PATH
from tests import m2_runner_rig as rig

_REAL_RISK = (Path(__file__).resolve().parents[1] / "config" / "risk.toml").read_bytes().replace(b"\r\n", b"\n")

RAW = {
    "id": "m1",
    "question": "Paper session fixture",
    "slug": "paper-session-fixture",
    "conditionId": "cond-1",
    "clobTokenIds": json.dumps(["yes-token", "no-token"]),
    "outcomes": json.dumps(["Yes", "No"]),
    "outcomePrices": json.dumps(["0.40", "0.40"]),
    "liquidityNum": 25000,
    "volumeNum": 1000,
    "endDate": "2026-12-01T00:00:00Z",
    "bestBid": 0.39,
    "bestAsk": 0.41,
    "spread": 0.02,
    "feesEnabled": False,
    "closed": False,
    "acceptingOrders": True,
    "version": "v1",
}
REJECTED = [
    dict(RAW, id="r-closed", slug="r-closed", closed=True),
    dict(RAW, id="r-wide", slug="r-wide", spread=0.30, bestBid=0.20, bestAsk=0.50),
    {key: value for key, value in dict(RAW, id="r-nover", slug="r-nover").items() if key != "version"},
    dict(RAW, id="r-soon", slug="r-soon", endDate="2026-10-08T12:00:00Z"),
]
CLOCK = "2026-10-08T06:00:00.500Z"


def _book(url: str) -> dict:
    assert url.startswith("https://clob.polymarket.com/book?")
    return {
        "bids": [{"price": "0.30", "size": "10"}],
        "asks": [{"price": "0.40", "size": "12"}],
        "tick_size": "0.01",
        "min_order_size": "5",
        "market": "cond-1",
        "timestamp": "1791439200000",
    }


class _Counting:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self, url: str, *args, **kwargs):
        self.n += 1
        raise AssertionError(f"public read after pin drift: {url}")


def _refused(tmp_path, *, risk_path) -> tuple[dict, _Counting, _Counting]:
    books, markets = _Counting(), _Counting()
    result = paper_session.run(
        raw_markets=None,
        book_getter=books,
        market_getter=markets,
        markets_path=tmp_path / "markets.jsonl",
        books_path=tmp_path / "books.jsonl",
        bus_path=tmp_path / "bus.jsonl",
        sessions_path=tmp_path / "sessions.jsonl",
        risk_path=risk_path,
    )
    return result, books, markets


def _assert_refused_with_halt(tmp_path, result, books, markets, problem_prefix: str) -> None:
    assert result["ok"] is False
    assert result["reason"] == "risk_toml_pin_drift"
    assert result["markets"] == 0 and result["books"] == 0 and result["fills"] == 0
    assert books.n == 0 and markets.n == 0
    for name in ("markets.jsonl", "books.jsonl", "sessions.jsonl"):
        assert not (tmp_path / name).exists(), name
    halts = halt_store.active_halts()
    assert [row["reason"] for row in halts] == ["risk_toml_pin_drift"]
    assert halts[0]["scope"] == "global"
    # The halt bus row names the detector and the problems (the stored halt keeps only reason / scope).
    bus = [row for row in podlib.read_jsonl(tmp_path / "bus.jsonl") if "risk_toml_pin_drift" in json.dumps(row)]
    assert len(bus) == 1
    text = json.dumps(bus[0], default=str)
    assert "paper_session" in text and problem_prefix in text
    assert RISK_PATH.read_bytes().replace(b"\r\n", b"\n") == _REAL_RISK


# ---------------------------------------------------------------- 1. paper_session: pin check first


def test_missing_risk_file_is_pin_drift_with_halt_and_writes_nothing(tmp_path):
    result, books, markets = _refused(tmp_path, risk_path=tmp_path / "absent.toml")
    _assert_refused_with_halt(tmp_path, result, books, markets, "risk_toml_unreadable")


def test_risk_path_that_is_a_directory_is_pin_drift(tmp_path):
    folder = tmp_path / "risk_dir"
    folder.mkdir()
    result, books, markets = _refused(tmp_path, risk_path=folder)
    _assert_refused_with_halt(tmp_path, result, books, markets, "risk_toml_unreadable")


def test_sha_computation_error_is_pin_drift(tmp_path, monkeypatch):
    class _BrokenHashlib:
        @staticmethod
        def sha256(*_a, **_k):
            raise RuntimeError("sha256 unavailable")

    monkeypatch.setattr(pins, "hashlib", _BrokenHashlib)
    pinned = tmp_path / "risk.toml"
    pinned.write_bytes(_REAL_RISK)
    result, books, markets = _refused(tmp_path, risk_path=pinned)
    _assert_refused_with_halt(tmp_path, result, books, markets, "pin_check_failed:RuntimeError")


def test_default_risk_path_missing_is_pin_drift(tmp_path, monkeypatch):
    """No risk_path: the module's default file is the one checked (and it is missing): drift, not an exception."""
    monkeypatch.setattr(paper_session, "RISK_PATH", tmp_path / "gone" / "risk.toml")
    result, books, markets = _refused(tmp_path, risk_path=None)
    _assert_refused_with_halt(tmp_path, result, books, markets, "risk_toml_unreadable")


def test_pin_check_runs_before_any_risk_read(tmp_path, monkeypatch):
    """Nothing reads risk.toml (load_risk / read_bytes on the default path) before the pin check refuses."""
    monkeypatch.setattr(paper_session, "load_risk", lambda: pytest.fail("load_risk before the pin check"))
    result, books, markets = _refused(tmp_path, risk_path=tmp_path / "absent.toml")
    _assert_refused_with_halt(tmp_path, result, books, markets, "risk_toml_unreadable")


def test_before_snapshot_and_pin_check_use_the_same_path(tmp_path, monkeypatch):
    """A pinned copy passed as risk_path runs even when the module default path is missing (same file for both)."""
    pinned = tmp_path / "pinned.toml"
    pinned.write_bytes(_REAL_RISK)
    monkeypatch.setattr(paper_session, "RISK_PATH", tmp_path / "not-there.toml")
    result = paper_session.run(
        raw_markets=[RAW, *REJECTED],
        book_getter=_book,
        markets_path=tmp_path / "markets.jsonl",
        books_path=tmp_path / "books.jsonl",
        bus_path=tmp_path / "bus.jsonl",
        sessions_path=tmp_path / "sessions.jsonl",
        now="2026-10-08T06:00:00Z",
        clock=lambda: CLOCK,
        risk_path=pinned,
    )
    assert result["ok"] is True and result["reason"] == "signals_only" and result["fills"] == 0
    assert halt_store.active_halts() == []


def test_risk_file_changed_during_session_against_the_checked_path_still_raises(tmp_path, monkeypatch):
    """The unchanged-file guard compares the checked path (risk_path), not the module default."""
    pinned = tmp_path / "pinned.toml"
    pinned.write_bytes(_REAL_RISK)
    real_record = paper_session.record_books

    def tamper(*args, **kwargs):
        count = real_record(*args, **kwargs)
        pinned.write_bytes(_REAL_RISK + b"# edited mid-session\n")
        return count

    monkeypatch.setattr(paper_session, "record_books", tamper)
    with pytest.raises(RuntimeError, match="risk.toml changed during paper session"):
        paper_session.run(
            raw_markets=[RAW, *REJECTED],
            book_getter=_book,
            markets_path=tmp_path / "markets.jsonl",
            books_path=tmp_path / "books.jsonl",
            bus_path=tmp_path / "bus.jsonl",
            sessions_path=tmp_path / "sessions.jsonl",
            now="2026-10-08T06:00:00Z",
            clock=lambda: CLOCK,
            risk_path=pinned,
        )
    assert not (tmp_path / "sessions.jsonl").exists()



# ---------------------------------------------------------------- 3. Task DS: pin check passes, reread refuses


def _race_after_real_check(monkeypatch, pinned: Path, change) -> dict:
    """Run the real pins.refuse_start on ``pinned``; once it has passed, ``change`` races the file."""
    real = paper_session.refuse_start
    seen: dict = {}

    def check_then_race(proc, *, risk_path=None, bus_path=None, stream=None):
        problems = real(proc, risk_path=risk_path, bus_path=bus_path, stream=stream)
        seen["problems"], seen["risk_path"] = problems, risk_path
        seen["halts_after_check"] = list(halt_store.active_halts())
        change()
        return problems

    monkeypatch.setattr(paper_session, "refuse_start", check_then_race)
    return seen


def _assert_check_passed_first(seen: dict, pinned: Path) -> None:
    assert seen["problems"] == []  # the real pin check passed: the refusal comes from the reread
    assert Path(seen["risk_path"]) == pinned
    assert seen["halts_after_check"] == []  # no halt before the reread found the drift


def _pinned_copy(tmp_path) -> Path:
    pinned = tmp_path / "risk.toml"
    pinned.write_bytes(_REAL_RISK)
    return pinned


def test_reread_refuses_when_file_deleted_after_check(tmp_path, monkeypatch):
    pinned = _pinned_copy(tmp_path)
    seen = _race_after_real_check(monkeypatch, pinned, pinned.unlink)
    result, books, markets = _refused(tmp_path, risk_path=pinned)
    _assert_check_passed_first(seen, pinned)
    _assert_refused_with_halt(tmp_path, result, books, markets, "risk_toml_unreadable")


def test_reread_refuses_when_sha_no_longer_pinned_after_check(tmp_path, monkeypatch):
    pinned = _pinned_copy(tmp_path)
    seen = _race_after_real_check(monkeypatch, pinned,
                                  lambda: pinned.write_bytes(_REAL_RISK + b"# edited after the pin check\n"))
    result, books, markets = _refused(tmp_path, risk_path=pinned)
    _assert_check_passed_first(seen, pinned)
    _assert_refused_with_halt(tmp_path, result, books, markets, "risk_sha256_not_pinned")


def test_reread_refuses_when_file_replaced_by_directory_after_check(tmp_path, monkeypatch):
    pinned = _pinned_copy(tmp_path)

    def swap() -> None:
        pinned.unlink()
        pinned.mkdir()

    seen = _race_after_real_check(monkeypatch, pinned, swap)
    result, books, markets = _refused(tmp_path, risk_path=pinned)
    _assert_check_passed_first(seen, pinned)
    _assert_refused_with_halt(tmp_path, result, books, markets, "risk_toml_unreadable")


def test_reread_that_raises_after_check_is_pin_drift(tmp_path, monkeypatch):
    """A non-OSError in the reread (here its sha256) is drift too, never an uncaught exception."""
    pinned = _pinned_copy(tmp_path)

    class _BrokenHashlib:
        @staticmethod
        def sha256(*_a, **_k):
            raise RuntimeError("sha256 unavailable on reread")

    # Only paper_session's reread binding breaks; the real pin check (pins.hashlib) still passes.
    seen = _race_after_real_check(monkeypatch, pinned,
                                  lambda: monkeypatch.setattr(paper_session, "hashlib", _BrokenHashlib))
    result, books, markets = _refused(tmp_path, risk_path=pinned)
    _assert_check_passed_first(seen, pinned)
    _assert_refused_with_halt(tmp_path, result, books, markets, "pin_check_failed:RuntimeError")


def test_reread_refusal_never_parses_or_reads_config_further(tmp_path, monkeypatch):
    """After the reread refuses, nothing parses the risk file or builds scanner thresholds."""
    pinned = _pinned_copy(tmp_path)
    monkeypatch.setattr(paper_session, "parse_risk_toml", lambda *_a, **_k: pytest.fail("parsed after drift"))
    monkeypatch.setattr(paper_session.scanner, "thresholds_from", lambda *_a, **_k: pytest.fail("thresholds"))
    monkeypatch.setattr(paper_session, "record_books", lambda *_a, **_k: pytest.fail("record_books"))
    seen = _race_after_real_check(monkeypatch, pinned, pinned.unlink)
    result, books, markets = _refused(tmp_path, risk_path=pinned)
    _assert_check_passed_first(seen, pinned)
    _assert_refused_with_halt(tmp_path, result, books, markets, "risk_toml_unreadable")

# ---------------------------------------------------------------- 2. m2_runner.kill_alert: Exception -> exit 4


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    return {**paths, "now": datetime.now(timezone.utc), "tmp": tmp_path}


def _kill(env) -> None:
    env["run"].mkdir(parents=True, exist_ok=True)
    (env["run"] / "KILL").write_text("stop", encoding="utf-8")


@pytest.mark.parametrize("exc", [ValueError("bad row"), TypeError("not str"), RuntimeError("encoder")])
def test_non_oserror_alert_write_failure_exits_4(env, monkeypatch, exc):
    def broken(path, row):
        raise exc

    monkeypatch.setattr(podlib, "append_jsonl", broken)
    fake = rig.FakeGetters(env["now"], fail=True)
    _kill(env)
    assert rig.run_once(env["run"], fake) == m2_runner.EXIT_WRITE_FAILED
    assert fake.calls == 0
    record, problem = heartbeat.read_beat(heartbeat.beat_path(m2_runner.PROC), m2_runner.PROC)
    assert problem is None and record is not None
    assert record["state"] == heartbeat.STATE_FINISHED and record["exit_code"] == m2_runner.EXIT_WRITE_FAILED


def test_kill_alert_converts_value_error_to_write_failed(env, monkeypatch):
    monkeypatch.setattr(podlib, "append_jsonl", lambda path, row: (_ for _ in ()).throw(ValueError("x")))
    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(env["run"]))
    with pytest.raises(m2_runner.WriteFailed, match="alerts.jsonl: ValueError"):
        m2_runner.kill_alert(state, env["now"])
    assert state.kill_alerted is False


@pytest.mark.parametrize("exc_type", [KeyboardInterrupt, SystemExit])
def test_kill_alert_does_not_swallow_keyboard_interrupt_or_system_exit(env, monkeypatch, exc_type):
    def broken(path, row):
        raise exc_type()

    monkeypatch.setattr(podlib, "append_jsonl", broken)
    state = m2_runner.RunnerState(paths=m2_runner.RunnerPaths.under(env["run"]))
    with pytest.raises(exc_type):
        m2_runner.kill_alert(state, env["now"])
    assert state.kill_alerted is False