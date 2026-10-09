"""Task AS (Poly哨兵 design gaps 5 and 6): runtime pin checks of config/risk.toml."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import src.halt_store as halt_store
import src.podlib as podlib
from src import pins
from src.thresholds import PIN_RECHECK_S, SENTINEL_HEARTBEAT_TIMEOUT_S

ROOT = Path(__file__).resolve().parents[1]
REAL = (ROOT / "config" / "risk.toml").read_bytes().replace(b"\r\n", b"\n")  # LF form (Windows checkouts are CRLF)


@pytest.fixture(autouse=True)
def _bus(tmp_path, monkeypatch):
    """Halt bus lines go to the test's folder (the halt state already does, tests/conftest.py)."""
    monkeypatch.setattr(podlib, "BUS_PATH", tmp_path / "bus.jsonl")
    return tmp_path / "bus.jsonl"


def risk_file(tmp_path, data: bytes | str, name="risk.toml") -> Path:
    path = tmp_path / name
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    return path


def drifted(tmp_path, old: str, new: str) -> Path:
    text = REAL.decode("utf-8")
    assert text.count(old) == 1
    return risk_file(tmp_path, text.replace(old, new), "drift.toml")


def halts():
    return [r for r in halt_store.active_halts() if r["reason"] == pins.PIN_HALT_REASON]


# ---------------------------------------------------------------- the file pins (gap 6)

def test_the_real_file_is_pinned():
    assert pins.risk_file_problems() == []
    assert pins.risk_file_problems(ROOT / "config" / "risk.toml") == []


def test_every_key_of_the_file_is_pinned():
    cfg = podlib.parse_risk_toml(REAL.decode("utf-8"))
    assert set(cfg) == set(pins.RISK_FILE_PINS) and len(cfg) == 9
    assert pins.RISK_FILE_PINS["live_enabled"] is False and pins.RISK_FILE_PINS["mode"] == "paper"
    assert pins.RISK_FILE_PINS["require_human_gate"] is True
    for key, value in podlib.PINS.items():
        assert pins.RISK_FILE_PINS[key] == value


def test_a_crlf_checkout_is_pinned(tmp_path):
    assert pins.risk_file_problems(risk_file(tmp_path, REAL.replace(b"\n", b"\r\n"))) == []


@pytest.mark.parametrize("old,new,problem", [
    ("live_enabled = false", "live_enabled = true", "live_enabled=True expected False"),
    ('mode = "paper"', 'mode = "live"', "mode='live' expected 'paper'"),
    ("require_human_gate = true", "require_human_gate = false", "require_human_gate=False expected True"),
    ("max_open_positions = 5", "max_open_positions = 6", "max_open_positions=6 expected 5"),
    ("min_liquidity_usd = 10000", "min_liquidity_usd = 5000", "min_liquidity_usd=5000 expected 10000"),
    ("max_spread = 0.04", "max_spread = 0.05", "max_spread=0.05 expected 0.04"),
    ("kelly_fraction = 0.15", "kelly_fraction = 0.25", "kelly_fraction=0.25 expected 0.15"),
    ("max_order_pct = 0.01", "max_order_pct = 0.02", "max_order_pct=0.02 expected 0.01"),
    ("require_human_gate = true", "require_human_gate = 1", "require_human_gate=1 expected True"),
    ("kelly_fraction = 0.15", "kelly_fraction = true", "kelly_fraction=True expected 0.15"),
    ("max_spread = 0.04", 'max_spread = "0.04"', "max_spread='0.04' expected 0.04"),
])
def test_a_changed_value_or_type_is_drift(tmp_path, old, new, problem):
    problems = pins.risk_file_problems(drifted(tmp_path, old, new))
    assert problem in problems and "risk_sha256_not_pinned" in problems


def test_a_missing_key_is_drift(tmp_path):
    problems = pins.risk_file_problems(drifted(tmp_path, "kelly_fraction = 0.15\n", ""))
    assert "kelly_fraction_missing" in problems


def test_an_unknown_key_is_drift(tmp_path):
    problems = pins.risk_file_problems(drifted(tmp_path, "kelly_fraction = 0.15\n", "kelly_fraction = 0.15\nlive_mode = 1\n"))
    assert "unknown_key:live_mode" in problems


def test_a_comment_only_change_is_still_drift_by_sha(tmp_path):
    assert pins.risk_file_problems(risk_file(tmp_path, REAL + b"# note\n")) == ["risk_sha256_not_pinned"]


def test_unreadable_and_unparseable_files_are_drift(tmp_path):
    assert pins.risk_file_problems(tmp_path / "missing.toml") == ["risk_toml_unreadable:FileNotFoundError"]
    problems = pins.risk_file_problems(risk_file(tmp_path, b"\xff\xfe not utf-8"))
    assert problems[0] == "risk_sha256_not_pinned" and problems[1].startswith("risk_toml_unparseable:")


def test_a_broken_check_is_drift(monkeypatch):
    monkeypatch.setattr(pins, "risk_file_problems", lambda path=None: 1 / 0)
    assert pins._checked(None) == ["pin_check_failed:ZeroDivisionError"]


def test_the_recheck_interval_is_registered():
    assert 0 < PIN_RECHECK_S <= SENTINEL_HEARTBEAT_TIMEOUT_S


# ---------------------------------------------------------------- refuse_start and PinWatch (gap 5)

def test_refuse_start_passes_on_the_pinned_file(capsys):
    assert pins.refuse_start("scanner") == []
    assert halts() == [] and capsys.readouterr().err == ""


def test_refuse_start_records_one_global_halt(tmp_path, monkeypatch, capsys, _bus):
    monkeypatch.setattr(podlib, "RISK_PATH", drifted(tmp_path, "live_enabled = false", "live_enabled = true"))
    problems = pins.refuse_start("scanner")
    assert "live_enabled=True expected False" in problems
    assert "refused to start" in capsys.readouterr().err
    (record,) = halts()
    assert record["scope"] == halt_store.SCOPE_GLOBAL and record["source"] == "Sentinel"
    (line,) = [json.loads(x) for x in _bus.read_text(encoding="utf-8").splitlines()]
    assert line["type"] == "halt" and line["payload"]["detected_by"] == "scanner"
    assert line["payload"]["phase"] == "start" and line["payload"]["severity"] == "sev1"
    assert line["payload"]["live_enabled"] is False and line["payload"]["exchange_action"] == "none"
    # a second process seeing the same drift adds no second halt
    assert pins.refuse_start("book")
    assert len(halts()) == 1 and len(_bus.read_text(encoding="utf-8").splitlines()) == 1


def test_a_failed_halt_record_falls_back_to_the_halt_file(tmp_path, monkeypatch):
    import src.sentinel as sentinel

    monkeypatch.setattr(podlib, "RISK_PATH", tmp_path / "gone.toml")
    monkeypatch.setattr(sentinel, "halt", lambda **kw: 1 / 0)
    assert pins.refuse_start("scanner") == ["risk_toml_unreadable:FileNotFoundError"]
    assert halt_store.fallback_active()


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_pinwatch_checks_first_then_throttles_and_sticks(tmp_path, monkeypatch):
    clock = Clock()
    path = risk_file(tmp_path, REAL)
    watch = pins.PinWatch("ws_recorder", every_s=60, monotonic=clock, risk_path=path)
    assert watch.drifted() is False and watch.checks == 1
    clock.t = 59.9
    assert watch.drifted() is False and watch.checks == 1
    path.write_bytes(REAL.replace(b"live_enabled = false", b"live_enabled = true"))
    clock.t = 30 + 29.9
    assert watch.drifted() is False and watch.checks == 1  # throttled: not re-read yet
    clock.t = 60.0
    assert watch.drifted() is True and watch.checks == 2
    assert "live_enabled=True expected False" in watch.problems
    path.write_bytes(REAL)  # putting the file back does not lift the drift (only the Operator resumes)
    clock.t = 500
    assert watch.drifted() is True and watch.checks == 2
    (record,) = halts()
    assert record["scope"] == halt_store.SCOPE_GLOBAL


def test_pinwatch_default_interval_is_the_threshold():
    assert pins.PinWatch("m1_run").every_s == PIN_RECHECK_S


# ---------------------------------------------------------------- every entry point refuses with its code

@pytest.fixture
def drift(tmp_path, monkeypatch):
    path = drifted(tmp_path, "kelly_fraction = 0.15\n", "kelly_fraction = 0.15\nextra = 1\n")
    monkeypatch.setattr(podlib, "RISK_PATH", path)
    return path


def _no_network(url):
    raise AssertionError(f"network touched: {url}")


def test_scanner_refuses_with_6(drift, tmp_path):
    from src import scanner

    out = tmp_path / "markets.jsonl"
    assert scanner.main(getter=_no_network, markets_path=out) == scanner.EXIT_PIN_DRIFT == 6
    assert not out.exists() and len(halts()) == 1


def test_book_refuses_with_6(drift, monkeypatch):
    from src import book

    monkeypatch.setattr(book, "read_jsonl", lambda *a, **k: (_ for _ in ()).throw(AssertionError("read markets")))
    assert book.main() == book.EXIT_PIN_DRIFT == 6
    assert len(halts()) == 1


def test_endgame_scan_refuses_with_4(drift):
    from src import endgame_scan

    assert endgame_scan.main(getter=_no_network) == endgame_scan.EXIT_NOT_PAPER == 4
    assert len(halts()) == 1


def test_resolutions_refuses_with_4(drift, tmp_path):
    from src import resolutions

    out = tmp_path / "resolutions.jsonl"
    assert resolutions.main(getter=_no_network, resolutions_path=out) == resolutions.EXIT_PIN == 4
    assert not out.exists() and len(halts()) == 1


def test_loop_claim_refuses_with_2(drift, tmp_path):
    from src import loop_claim

    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    assert loop_claim.main(["status", "--state", str(state)]) == loop_claim.EXIT_PIN == 2
    assert state.read_text(encoding="utf-8") == "{}" and len(halts()) == 1


def test_paper_cycle_refuses_with_1(drift, monkeypatch):
    from src import paper_cycle

    monkeypatch.setattr(paper_cycle, "run", lambda **kw: (_ for _ in ()).throw(AssertionError("cycle ran")))
    assert paper_cycle.main([]) == paper_cycle.EXIT_PIN == 1
    assert len(halts()) == 1


def test_ws_recorder_refuses_with_6(drift, tmp_path):
    from src import ws_recorder

    class NoTransport:
        def connect(self, url, timeout):
            raise AssertionError("connected")

    out = tmp_path / "books.jsonl"
    code = ws_recorder.main(["--assets", "123", "--max-runtime", "5", "--out", str(out)], transport=NoTransport())
    assert code == ws_recorder.EXIT_PIN_DRIFT == 6 and not out.exists() and len(halts()) == 1


def test_m1_run_refuses_with_6_before_the_run_dir_exists(drift, tmp_path):
    from src import m1_run

    class NoTransport:
        def connect(self, url, timeout):
            raise AssertionError("connected")

    out = tmp_path / "run"
    code = m1_run.main(["--out", str(out), "--assets", "123", "--max-runtime", "5"], transport=NoTransport(),
                       rest_getter=_no_network)
    assert code == m1_run.EXIT_PIN_DRIFT == 6 and not out.exists() and len(halts()) == 1

