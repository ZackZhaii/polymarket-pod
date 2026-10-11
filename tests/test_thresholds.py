"""Task AC: src/thresholds.py, docs/THRESHOLDS.md and the old constant names stay in sync. Offline."""
from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

import src.thresholds as thresholds
from scripts import guard
from src.podlib import RISK_PATH, parse_risk_toml

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "THRESHOLDS.md"
SOURCE = ROOT / "src" / "thresholds.py"

# Frozen values: exactly what the modules defined before Task AC. A change here is a threshold change.
EXPECTED = {
    "MAX_BOOK_AGE_S": 120.0,
    "MIN_EDGE": 0.02,
    "EDGE_TOLERANCE": 1e-12,
    "PAPER_EQUITY_USD": 1000.0,
    "PAPER_CYCLE_STALE_AFTER_S": 120,
    "SENTINEL_STALE_S": 120.0,
    "SENTINEL_STALE_CONSECUTIVE": 2,
    "WASH_WINDOW_S": 300.0,
    "PAIR_CROSS_TOLERANCE": 1e-9,  # Task CG (#145)
    "MIN_HOURS_TO_EXPIRY": 24.0,
    "SCANNER_MIN_PRICE": 0.03,
    "SCANNER_MAX_PRICE": 0.97,
    "MIN_SCANNED_MARKETS": 5,
    "ENDGAME_PRICE_MIN": 0.98,
    "ENDGAME_PRICE_MAX": 0.99,
    "ENDGAME_WINDOW_HOURS": 48.0,
    "SPORTS_DEVIG_ASSUMED_SLIPPAGE": 0.005,
    # Task AT: the one assumed slippage; SPORTS_DEVIG_/INTENT_ASSUMED_SLIPPAGE are aliases of it.
    "ASSUMED_SLIPPAGE": 0.005,
    "LEDGER_OPEN_EPS": 1e-9,
    "REFLECTOR_MAX_PER_UTC_DAY": 3,
    # Task AG: new WebSocket recorder timing (not moved from anywhere; first values).
    "WS_BACKOFF_BASE_S": 1.0,
    "WS_BACKOFF_CAP_S": 60.0,
    "WS_RESYNC_TIMEOUT_S": 30.0,
    "WS_PING_INTERVAL_S": 10.0,
    "WS_PONG_TIMEOUT_S": 30.0,  # Task AW
    "WS_SILENCE_TIMEOUT_S": 60.0,  # Task BI
    "WS_SILENCE_OBSERVED_HEALTHY_MAX_S": 38.7,  # Task BI (evidence)
    "WS_LATENCY_SUMMARY_PINGS": 60,  # Task BI (operational)
    "WS_DATA_SILENCE_S": 90.0,  # Task BK
    "WS_RAW_BOOK_FILE_MAX_BYTES": 16777216,  # Task AY
    "WS_RAW_BOOK_RUN_MAX_BYTES": 268435456,  # Task AY
    "WS_ROW_REFRESH_S": 30.0,
    "WS_GROUP_FLUSH_S": 0.25,  # Task CC (#182): same-ts group window
    # Task AI: M1 acceptance (first values; the criteria are M2 pending).
    "WS_STOP_POLL_S": 1.0,
    "M1_TOP_MARKETS": 5,
    "M1_ROTATE_BYTES": 67108864,
    "M1_ROTATE_S": 3600.0,
    "M1_REST_EVERY_S": 600.0,
    "M1_REST_ASSETS": 4,
    "M1_MANIFEST_PROGRESS_S": 60.0,  # Task BT (M2 pending)
    "M1_CONSOLE_CLOSE_WAIT_S": 2.0,  # Task BT (M2 pending)
    "M1_REPORT_STEP_S": 1.0,
    "FEE_CACHE_TTL_S": 900.0,  # Task BZ #117 (M2 pending)
    "FEE_NEG_CACHE_S": 60.0,  # Task BZ #117 (M2 pending)
    "FEE_RATE_MAX": 0.25,  # Task BZ #117 (M2 pending)
    "FORECAST_MAX_ASOF_LAG_S": 21600.0,  # Task CD #118 (M2 pending)
    "FORECAST_MIN_HALF_WIDTH": 0.03,  # Task CD #118 (M2 pending)
    "RUNNER_LOOP_S": 60.0,  # Task CE #119 (M2 pending)
    "HEARTBEAT_RUNNER_EVERY_S": 30.0,  # Task CE #119 (M2 pending)
    "HEARTBEAT_RUNNER_TIMEOUT_S": 120.0,  # Task CE #119 (M2 pending)
    "RUNNER_PAUSED_ALERT_S": 21600.0,  # Task CE #119 (M2 pending)
    "RUNNER_SCAN_EVERY_S": 1800.0,  # Task CE #119 (M2 pending)
    "RUNNER_SCAN_MAX_AGE_S": 7200.0,  # Task CE #119 (M2 pending)
    "RUNNER_MAX_INGEST_PER_ITER": 20,  # Task CE #119 (M2 pending)
    "RUNNER_MAX_EVALS_PER_SIGNAL": 10,  # Task CE #119 (M2 pending)
    "DISK_MIN_FREE_GB": 2.0,  # Task CE #119 (M2 pending)
    "RUNNER_DISK_BUDGET_GB": 5.0,  # Task CE #119 (M2 pending)
    "M1_RUN_TARGET_S": 21600.0,  # KEY-approved 2026-10-11 家伟批准
    "M1_MIN_RUN_S": 21540.0,  # KEY-approved 2026-10-11 家伟批准 (was 86340.0)
    "M1_MIN_COVERAGE": 0.99,
    "M1_MAX_RECONNECTS": 48,
    "M1_MAX_GAP_RESYNCS_PER_ASSET": 24,
    "M1_REST_MATCH_WINDOW_S": 30.0,
    "M1_MIN_REST_MATCHED": 50,
    "M1_MAX_TOP_MISMATCH_RATE": 0.02,
    "M1_MAX_REST_UNMATCHED_RATE": 0.1,
    # Task AK: Data API v2 reconciliation (tolerance M2 pending; the two floors are Data API contract facts).
    "RECONCILE_SHARES_TOL": 1e-6,
    "DATA_API_POSITIONS_DUST_SHARES": 0.1,
    "DATA_API_TRADES_MIN_SHARES": 0.01,
    "DATA_API_MAX_LAG_S": 120.0,
    "DATA_API_IDENTITY_TOL_USDC": 1e-6,
    # Task AO: signal -> OrderIntent (first values; all but the share step are M2 pending).
    "INTENT_ASSUMED_SLIPPAGE": 0.005,
    "INTENT_SHARE_STEP": 0.01,
    "INTENT_TTL_S": 60.0,
    "MAX_SIGNAL_AGE_NEWS_S": 3600.0,
    "MAX_SIGNAL_AGE_SPORTS_DEVIG_S": 300.0,
    "MAX_SIGNAL_AGE_ENDGAME_CONFIRM_S": 600.0,
    "MAX_SOURCE_LOGIT_GAP": 1.0,
    # Task AN: M3 settlement snapshot hold (not coupled to ENDGAME_WINDOW_HOURS).
    "RESOLUTION_HOLD_HOURS": 48.0,
    # Task AQ: secret-string scan of the bus and logs (M2 pending).
    "SECRET_VALUE_MIN_LEN": 16,
    "MNEMONIC_MIN_WORDS": 12,
    "SECRET_HASH_PREFIX_CHARS": 8,
    # Task AP: heartbeats and the resident Sentinel (first values from the Sentinel design; M2 pending).
    "HEARTBEAT_RECORDER_EVERY_S": 10.0,
    "HEARTBEAT_RECORDER_TIMEOUT_S": 120.0,
    "HEARTBEAT_REST_TIMEOUT_S": 900.0,
    "SCANNER_HANG_S": 1800.0,
    "SCANNER_MAX_SUCCESS_AGE_S": 86400.0,
    "SCANNER_INCOMPLETE_CONSECUTIVE": 2,
    "HEARTBEAT_PAPER_CYCLE_EVERY_S": 60.0,
    "HEARTBEAT_PAPER_CYCLE_TIMEOUT_S": 180.0,
    "HEARTBEAT_PAPER_CYCLE_HALT_FACTOR": 2,
    "HEARTBEAT_CONSUMER_EVERY_S": 5.0,
    "HEARTBEAT_CONSUMER_TIMEOUT_S": 30.0,
    "SENTINEL_TICK_S": 30.0,
    "SENTINEL_HEARTBEAT_TIMEOUT_S": 90.0,
    "RECORDER_ESCALATION_WINDOW_S": 600.0,
    "RECORDER_RECONNECT_ALERT_COUNT": 4,
    "RECORDER_GAP_RESYNC_HALT_COUNT": 3,
    "ALL_STALE_CONSECUTIVE_CYCLES": 2,
    # Task AS: runtime pin re-check (M2 pending).
    "PIN_RECHECK_S": 60.0,
    # Task AZ: M3 calibration report (M3 pending).
    "CALIBRATION_MIN_SAMPLES": 30,
    "CALIBRATION_MIN_MARKETS": 20,
    "CALIBRATION_MIN_RESOLUTION_DAYS": 5,
    "CALIBRATION_GROUP_MIN_SAMPLES": 10,
    "CALIBRATION_GROUP_MIN_MARKETS": 5,
    "CALIBRATION_FIXED_BINS_MIN_SAMPLES": 100,
    "CALIBRATION_EQUAL_FREQ_BINS": 5,
    "CALIBRATION_EQUAL_FREQ_MIN_PER_BIN": 6,
    "CALIBRATION_MAX_DECISIONS_PER_MARKET": 2,
    "CALIBRATION_BOOTSTRAP_RESAMPLES": 2000,
    "CALIBRATION_BOOTSTRAP_SEED": 20261009,
    "CALIBRATION_CI_LEVEL": 0.95,
    "CALIBRATION_LOGLOSS_CLIP": 0.01,
    # Task BD: largest event-cluster share (M3 pending).
    "CALIBRATION_MAX_CLUSTER_SHARE": 0.30,
    # Task BB: M3 Reflector proposal rules (M3 pending).
    "REFLECTOR_DISABLE_REALIZATION_MAX": 0.25,
    "REFLECTOR_DISABLE_SLOPE_MAX": 0.5,
    "REFLECTOR_DISABLE_SLOPE_CI_HIGH_MAX": 0.8,
    "REFLECTOR_TIGHTEN_SLOPE_MAX": 0.8,
    "REFLECTOR_TIGHTEN_SLOPE_CI_HIGH_MAX": 1.0,
    "REFLECTOR_ECE_MAX": 0.05,
    "REFLECTOR_MIN_EDGE_STEP": 0.01,
    "REFLECTOR_REJECT_COOLDOWN_DAYS": 7,
    "REFLECTOR_REJECT_NEW_SAMPLES": 15,
    # Task BE: edge-tier realization rule (M3 pending).
    "REFLECTOR_EDGE_TIER_REALIZATION_MAX": 0.25,
    # issue #52: the halt-state read-modify-write mutex (M2 pending).
    "HALT_STATE_LOCK_TIMEOUT_S": 5.0,
    "HALT_STATE_LOCK_POLL_S": 0.05,
}

# Old import paths that callers and tests use; each must stay the very same object.
REEXPORTS = {
    "book_staleness.MAX_BOOK_AGE_S": "MAX_BOOK_AGE_S",
    "risk_gate.MAX_BOOK_AGE_S": "MAX_BOOK_AGE_S",
    "sentinel.MAX_BOOK_AGE_S": "MAX_BOOK_AGE_S",
    "risk_gate.MIN_EDGE": "MIN_EDGE",
    "risk_gate.EDGE_TOLERANCE": "EDGE_TOLERANCE",
    "podlib.PAPER_EQUITY_USD": "PAPER_EQUITY_USD",
    "risk_gate.PAPER_EQUITY_USD": "PAPER_EQUITY_USD",
    "maker.PAPER_EQUITY_USD": "PAPER_EQUITY_USD",
    "paper_cycle.STALE_AFTER_S": "PAPER_CYCLE_STALE_AFTER_S",
    "sentinel.SENTINEL_STALE_S": "SENTINEL_STALE_S",
    "sentinel.SENTINEL_STALE_CONSECUTIVE": "SENTINEL_STALE_CONSECUTIVE",
    "wash_detect.WASH_WINDOW_S": "WASH_WINDOW_S",
    "wash_detect.PAIR_CROSS_TOLERANCE": "PAIR_CROSS_TOLERANCE",
    "expiry_rule.MIN_HOURS_TO_EXPIRY": "MIN_HOURS_TO_EXPIRY",
    "scanner.MIN_HOURS_TO_EXPIRY": "MIN_HOURS_TO_EXPIRY",
    "endgame_scan.MIN_HOURS_TO_EXPIRY": "MIN_HOURS_TO_EXPIRY",
    "scanner.MIN_PRICE": "SCANNER_MIN_PRICE",
    "scanner.MAX_PRICE": "SCANNER_MAX_PRICE",
    "scanner.MIN_SCANNED_MARKETS": "MIN_SCANNED_MARKETS",
    "endgame_scan.MIN_SCANNED_MARKETS": "MIN_SCANNED_MARKETS",
    "paper_session.SESSION_MIN_SCANNED": "MIN_SCANNED_MARKETS",
    "endgame.PRICE_MIN": "ENDGAME_PRICE_MIN",
    "endgame.PRICE_MAX": "ENDGAME_PRICE_MAX",
    "endgame.WINDOW_HOURS": "ENDGAME_WINDOW_HOURS",
    "sports_devig.ASSUMED_SLIPPAGE": "SPORTS_DEVIG_ASSUMED_SLIPPAGE",
    "paper_ledger.ASSUMED_SLIPPAGE": "ASSUMED_SLIPPAGE",
    "risk_gate.ASSUMED_SLIPPAGE": "ASSUMED_SLIPPAGE",  # Task AU: the slippage floor
    "paper_ledger.OPEN_EPS": "LEDGER_OPEN_EPS",
    "reflector.MAX_PER_UTC_DAY": "REFLECTOR_MAX_PER_UTC_DAY",
    "resolutions.RESOLUTION_HOLD_HOURS": "RESOLUTION_HOLD_HOURS",
}

M2_PENDING = {"MAX_BOOK_AGE_S", "MIN_EDGE", "SENTINEL_STALE_S", "SENTINEL_STALE_CONSECUTIVE", "WASH_WINDOW_S",
              "PAIR_CROSS_TOLERANCE",  # Task CG (#145)
              "MIN_HOURS_TO_EXPIRY", "WS_ROW_REFRESH_S",
              # Task AI: the M1 acceptance criteria (conservative first values for the user at M2).
              "M1_MIN_COVERAGE", "M1_MAX_RECONNECTS", "M1_MAX_GAP_RESYNCS_PER_ASSET",
              "M1_REST_MATCH_WINDOW_S", "M1_MIN_REST_MATCHED", "M1_MAX_TOP_MISMATCH_RATE", "M1_MAX_REST_UNMATCHED_RATE",
              # Task BT: manifest progress interval and the console-close finish wait.
              "M1_MANIFEST_PROGRESS_S", "M1_CONSOLE_CLOSE_WAIT_S",
              # Task BZ (#117, M2-A Gamma-only slice): per-market fee source.
              "FEE_CACHE_TTL_S", "FEE_NEG_CACHE_S", "FEE_RATE_MAX",
              # Task CD (#118, M2-B): the forecast inbox.
              "FORECAST_MAX_ASOF_LAG_S", "FORECAST_MIN_HALF_WIDTH",
              # Task CE (#119, M2-C1): the paper runner.
              "RUNNER_LOOP_S", "HEARTBEAT_RUNNER_EVERY_S", "HEARTBEAT_RUNNER_TIMEOUT_S", "RUNNER_PAUSED_ALERT_S",
              "RUNNER_SCAN_EVERY_S", "RUNNER_SCAN_MAX_AGE_S", "RUNNER_MAX_INGEST_PER_ITER",
              "RUNNER_MAX_EVALS_PER_SIGNAL", "DISK_MIN_FREE_GB", "RUNNER_DISK_BUDGET_GB",
              # Task AK: Data API v2 reconciliation.
              "RECONCILE_SHARES_TOL", "DATA_API_POSITIONS_DUST_SHARES", "DATA_API_TRADES_MIN_SHARES",
              "DATA_API_MAX_LAG_S", "DATA_API_IDENTITY_TOL_USDC",
              # Task AO: signal -> OrderIntent.
              "INTENT_ASSUMED_SLIPPAGE", "INTENT_TTL_S", "MAX_SIGNAL_AGE_NEWS_S", "MAX_SIGNAL_AGE_SPORTS_DEVIG_S",
              "MAX_SIGNAL_AGE_ENDGAME_CONFIRM_S", "MAX_SOURCE_LOGIT_GAP",
              # Task AT: the one assumed slippage (INTENT_ASSUMED_SLIPPAGE is its alias).
              "ASSUMED_SLIPPAGE",
              # Task AQ: secret scan.
              "SECRET_VALUE_MIN_LEN", "MNEMONIC_MIN_WORDS", "SECRET_HASH_PREFIX_CHARS",
              # Task AW: WS heartbeat liveness.
              "WS_PONG_TIMEOUT_S",
              # Task BI: inbound silence watchdog (and the healthy silence it must stay above).
              "WS_SILENCE_TIMEOUT_S", "WS_SILENCE_OBSERVED_HEALTHY_MAX_S",
              # Task BK: market-data silence (Poly风控 F6).
              "WS_DATA_SILENCE_S",
              # Task CC (#182): same-timestamp price_change group window.
              "WS_GROUP_FLUSH_S",
              # Task AY: raw book snapshot caps.
              "WS_RAW_BOOK_FILE_MAX_BYTES", "WS_RAW_BOOK_RUN_MAX_BYTES",
              # Task AP: heartbeats, the resident Sentinel and its escalations.
              "HEARTBEAT_RECORDER_EVERY_S", "HEARTBEAT_RECORDER_TIMEOUT_S", "HEARTBEAT_REST_TIMEOUT_S",
              "SCANNER_HANG_S", "SCANNER_MAX_SUCCESS_AGE_S", "SCANNER_INCOMPLETE_CONSECUTIVE",
              "HEARTBEAT_PAPER_CYCLE_EVERY_S", "HEARTBEAT_PAPER_CYCLE_TIMEOUT_S", "HEARTBEAT_PAPER_CYCLE_HALT_FACTOR",
              "HEARTBEAT_CONSUMER_EVERY_S", "HEARTBEAT_CONSUMER_TIMEOUT_S", "SENTINEL_TICK_S",
              "SENTINEL_HEARTBEAT_TIMEOUT_S", "RECORDER_ESCALATION_WINDOW_S", "RECORDER_RECONNECT_ALERT_COUNT",
              "RECORDER_GAP_RESYNC_HALT_COUNT", "ALL_STALE_CONSECUTIVE_CYCLES",
              # Task AS: runtime pin re-check.
              "PIN_RECHECK_S",
              # Task AZ: M3 calibration minimums and report settings (M3 pending).
              "CALIBRATION_MIN_SAMPLES", "CALIBRATION_MIN_MARKETS", "CALIBRATION_MIN_RESOLUTION_DAYS",
              "CALIBRATION_GROUP_MIN_SAMPLES", "CALIBRATION_GROUP_MIN_MARKETS", "CALIBRATION_FIXED_BINS_MIN_SAMPLES",
              "CALIBRATION_EQUAL_FREQ_BINS", "CALIBRATION_EQUAL_FREQ_MIN_PER_BIN", "CALIBRATION_BOOTSTRAP_RESAMPLES",
              "CALIBRATION_MAX_DECISIONS_PER_MARKET",
              "CALIBRATION_BOOTSTRAP_SEED", "CALIBRATION_CI_LEVEL", "CALIBRATION_LOGLOSS_CLIP",
              # Task BD: largest event-cluster share.
              "CALIBRATION_MAX_CLUSTER_SHARE",
              # Task BB: M3 Reflector proposals; the daily cap is now M3 pending too.
              "REFLECTOR_MAX_PER_UTC_DAY", "REFLECTOR_DISABLE_REALIZATION_MAX", "REFLECTOR_DISABLE_SLOPE_MAX",
              "REFLECTOR_DISABLE_SLOPE_CI_HIGH_MAX", "REFLECTOR_TIGHTEN_SLOPE_MAX", "REFLECTOR_TIGHTEN_SLOPE_CI_HIGH_MAX",
              "REFLECTOR_ECE_MAX", "REFLECTOR_MIN_EDGE_STEP", "REFLECTOR_REJECT_COOLDOWN_DAYS",
              "REFLECTOR_REJECT_NEW_SAMPLES",
              # Task BE: edge-tier realization rule.
              "REFLECTOR_EDGE_TIER_REALIZATION_MAX",
              # issue #52: the halt-state read-modify-write mutex.
              "HALT_STATE_LOCK_TIMEOUT_S", "HALT_STATE_LOCK_POLL_S"}
HEADER = ["Name", "Value", "Unit", "Meaning", "Where", "Source", "M2 pending"]


def _resolve(dotted: str):
    module, name = dotted.split(".")
    return getattr(importlib.import_module(f"src.{module}"), name)


def _tables() -> dict[str, list[dict[str, str]]]:
    """Section heading -> rows of the markdown table under it.

    Task AH: the parser lives in scripts/guard.py (markdown_tables) so the guard and this test read
    the doc the same way; it raises on a wrong header or a row with the wrong number of cells.
    """
    assert guard.THRESHOLDS_HEADER == HEADER
    return guard.markdown_tables(DOC.read_text(encoding="utf-8"), HEADER)


def _table(prefix: str) -> list[dict[str, str]]:
    matches = [rows for name, rows in _tables().items() if name.startswith(prefix)]
    assert len(matches) == 1, prefix
    return matches[0]


def _code(cell: str) -> str:
    match = re.fullmatch(r"`([^`]+)`", cell)
    assert match, cell
    return match.group(1)


def _module_constants() -> dict[str, object]:
    return {
        name: value
        for name, value in vars(thresholds).items()
        if name.isupper() and not name.startswith("_")
        and isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def test_values_equal_the_constants_they_replace():
    assert _module_constants() == EXPECTED
    for name, value in EXPECTED.items():
        assert type(getattr(thresholds, name)) is type(value), name


@pytest.mark.parametrize("dotted", sorted(REEXPORTS))
def test_old_names_still_work_and_are_the_same_value(dotted):
    name = REEXPORTS[dotted]
    assert _resolve(dotted) is getattr(thresholds, name)
    assert _resolve(dotted) == EXPECTED[name]


def test_doc_lists_every_module_constant_once_with_its_value():
    rows = _table("Code constants")
    names = [_code(row["Name"]) for row in rows]
    assert len(names) == len(set(names))
    assert set(names) == set(_module_constants())
    for row in rows:
        name = _code(row["Name"])
        value = ast.literal_eval(_code(row["Value"]))
        assert value == getattr(thresholds, name) and type(value) is type(getattr(thresholds, name)), name
        assert row["Unit"] and row["Meaning"] and row["Source"], name
        assert row["Where"].startswith("`src/thresholds.py`"), name


def test_doc_reexports_match_the_code():
    documented = {}
    for row in _table("Code constants"):
        name = _code(row["Name"])
        for dotted in re.findall(r"`([a-z_]+\.[A-Z_0-9]+)`", row["Where"]):
            documented[dotted] = name
    assert documented == REEXPORTS


def test_doc_m2_column_matches_the_pending_list():
    rows = _table("Code constants")
    pending = {_code(row["Name"]) for row in rows if row["M2 pending"] == "yes"}
    assert pending == M2_PENDING
    for row in rows:
        assert row["M2 pending"] == "yes" or row["M2 pending"].startswith("no"), row["Name"]


def test_operational_rows_match_their_modules():
    rows = _table("Operational limits")
    assert rows
    for row in rows:
        dotted = _code(row["Name"])
        value = ast.literal_eval(_code(row["Value"]))
        actual = _resolve(dotted)
        assert actual == value and type(actual) is type(value), dotted
        assert row["Where"] == f"`src/{dotted.split('.')[0]}.py`", dotted
        assert not hasattr(thresholds, dotted.split(".")[1]), dotted


def test_risk_toml_rows_match_the_file_and_are_read_only():
    before = RISK_PATH.read_bytes()
    parsed = parse_risk_toml(before.decode("utf-8"))
    rows = _table("config/risk.toml")
    names = [_code(row["Name"]) for row in rows]
    assert sorted(names) == sorted(parsed)
    for row in rows:
        name = _code(row["Name"])
        documented = parse_risk_toml(f"{name} = {_code(row['Value'])}\n")[name]
        assert documented == parsed[name] and type(documented) is type(parsed[name]), name
        assert row["Where"] == f"`risk.toml: {name}`"
        assert row["M2 pending"] == "file-governed"
        assert not hasattr(thresholds, name.upper()), name  # risk.toml parameters are not moved into code
    assert parsed["live_enabled"] is False
    assert RISK_PATH.read_bytes() == before


def test_doc_names_the_pinned_risk_sha():
    text = DOC.read_text(encoding="utf-8")
    assert "0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04" in text


def test_relations_hold():
    assert thresholds.consistency_problems() == []
    assert thresholds.SENTINEL_STALE_S == thresholds.MAX_BOOK_AGE_S
    assert thresholds.PAPER_CYCLE_STALE_AFTER_S == thresholds.MAX_BOOK_AGE_S


@pytest.mark.parametrize(
    ("line", "replacement"),
    [
        ("SENTINEL_STALE_S = 120.0", "SENTINEL_STALE_S = 60.0"),
        ("MAX_BOOK_AGE_S = 120.0", "MAX_BOOK_AGE_S = 300.0"),
        ("PAPER_CYCLE_STALE_AFTER_S = 120", "PAPER_CYCLE_STALE_AFTER_S = 300"),
        ("SENTINEL_STALE_CONSECUTIVE = 2", "SENTINEL_STALE_CONSECUTIVE = 0"),
        ("SENTINEL_STALE_CONSECUTIVE = 2", "SENTINEL_STALE_CONSECUTIVE = 1.5"),
        ("SCANNER_MIN_PRICE = 0.03", "SCANNER_MIN_PRICE = 0.97"),
        ("ENDGAME_PRICE_MIN = 0.98", "ENDGAME_PRICE_MIN = 0.96"),
        ("ENDGAME_PRICE_MAX = 0.99", "ENDGAME_PRICE_MAX = 1.0"),
        ("MIN_HOURS_TO_EXPIRY = 24.0", "MIN_HOURS_TO_EXPIRY = 48.0"),
        ("WS_BACKOFF_CAP_S = 60.0", "WS_BACKOFF_CAP_S = 0.5"),
        ("WS_BACKOFF_BASE_S = 1.0", "WS_BACKOFF_BASE_S = 0.0"),
        ("WS_PING_INTERVAL_S = 10.0", "WS_PING_INTERVAL_S = 30.0"),
        ("WS_PONG_TIMEOUT_S = 30.0", "WS_PONG_TIMEOUT_S = 10.0"),
        ("WS_SILENCE_TIMEOUT_S = 60.0", "WS_SILENCE_TIMEOUT_S = 30.0"),  # Task BI: not above the PONG timeout
        ("WS_SILENCE_TIMEOUT_S = 60.0", "WS_SILENCE_TIMEOUT_S = 35.0"),  # Task BI: under the healthy 38.7 s
        ("WS_LATENCY_SUMMARY_PINGS = 60", "WS_LATENCY_SUMMARY_PINGS = 0"),
        ("WS_DATA_SILENCE_S = 90.0", "WS_DATA_SILENCE_S = 121.0"),  # Task BK: above MAX_BOOK_AGE_S
        ("WS_DATA_SILENCE_S = 90.0", "WS_DATA_SILENCE_S = 59.0"),  # Task BK: below WS_SILENCE_TIMEOUT_S
        ("WS_RAW_BOOK_FILE_MAX_BYTES = 16 * 1024 * 1024", "WS_RAW_BOOK_FILE_MAX_BYTES = 512 * 1024 * 1024"),
        ("WS_RAW_BOOK_RUN_MAX_BYTES = 256 * 1024 * 1024", "WS_RAW_BOOK_RUN_MAX_BYTES = 0"),
        ("WS_ROW_REFRESH_S = 30.0", "WS_ROW_REFRESH_S = 120.0"),
        ("FORECAST_MAX_ASOF_LAG_S = 21600.0", "FORECAST_MAX_ASOF_LAG_S = 0.0"),  # Task CD
        ("FORECAST_MAX_ASOF_LAG_S = 21600.0", "FORECAST_MAX_ASOF_LAG_S = 604800.0"),  # Task CD: above one day
        ("FORECAST_MIN_HALF_WIDTH = 0.03", "FORECAST_MIN_HALF_WIDTH = 0.0"),  # Task CD
        ("FORECAST_MIN_HALF_WIDTH = 0.03", "FORECAST_MIN_HALF_WIDTH = 0.5"),  # Task CD
        ("RUNNER_LOOP_S = 60.0", "RUNNER_LOOP_S = 0.0"),  # Task CE
        ("RUNNER_LOOP_S = 60.0", "RUNNER_LOOP_S = 120.0"),  # Task CE: not below the heartbeat timeout
        ("HEARTBEAT_RUNNER_EVERY_S = 30.0", "HEARTBEAT_RUNNER_EVERY_S = 120.0"),  # Task CE
        ("HEARTBEAT_RUNNER_TIMEOUT_S = 120.0", "HEARTBEAT_RUNNER_TIMEOUT_S = 120"),  # Task CE: a float
        ("RUNNER_PAUSED_ALERT_S = 21600.0", "RUNNER_PAUSED_ALERT_S = 60.0"),  # Task CE
        ("RUNNER_SCAN_MAX_AGE_S = 7200.0", "RUNNER_SCAN_MAX_AGE_S = 1800.0"),  # Task CE
        ("RUNNER_MAX_INGEST_PER_ITER = 20", "RUNNER_MAX_INGEST_PER_ITER = 0"),  # Task CE
        ("RUNNER_MAX_EVALS_PER_SIGNAL = 10", "RUNNER_MAX_EVALS_PER_SIGNAL = True"),  # Task CE
        ("DISK_MIN_FREE_GB = 2.0", "DISK_MIN_FREE_GB = -1.0"),  # Task CE
        ("RUNNER_DISK_BUDGET_GB = 5.0", "RUNNER_DISK_BUDGET_GB = float('nan')"),  # Task CE
        ("WS_GROUP_FLUSH_S = 0.25", "WS_GROUP_FLUSH_S = 0.0"),  # Task CC: must be > 0
        ("WS_GROUP_FLUSH_S = 0.25", "WS_GROUP_FLUSH_S = 5.0"),  # Task CC: not well below WS_ROW_REFRESH_S
        ("WS_GROUP_FLUSH_S = 0.25", "WS_GROUP_FLUSH_S = 1"),  # Task CC: a float
        ("WS_STOP_POLL_S = 1.0", "WS_STOP_POLL_S = 0.0"),
        ("WS_STOP_POLL_S = 1.0", "WS_STOP_POLL_S = 11.0"),
        ("M1_TOP_MARKETS = 5", "M1_TOP_MARKETS = 0"),
        ("M1_ROTATE_BYTES = 67108864", "M1_ROTATE_BYTES = 1.5"),
        ("M1_REST_ASSETS = 4", "M1_REST_ASSETS = True"),
        ("M1_MAX_RECONNECTS = 48", "M1_MAX_RECONNECTS = -1"),
        ("M1_MAX_GAP_RESYNCS_PER_ASSET = 24", "M1_MAX_GAP_RESYNCS_PER_ASSET = 2.5"),
        ("M1_ROTATE_S = 3600.0", "M1_ROTATE_S = 0.0"),
        ("M1_REPORT_STEP_S = 1.0", "M1_REPORT_STEP_S = 60.0"),
        ("M1_MIN_COVERAGE = 0.99", "M1_MIN_COVERAGE = 1.5"),
        ("M1_MIN_COVERAGE = 0.99", "M1_MIN_COVERAGE = 0.0"),
        ("M1_MAX_TOP_MISMATCH_RATE = 0.02", "M1_MAX_TOP_MISMATCH_RATE = 1.0"),
        ("M1_MAX_REST_UNMATCHED_RATE = 0.10", "M1_MAX_REST_UNMATCHED_RATE = -0.1"),
        ("M1_REST_MATCH_WINDOW_S = 30.0", "M1_REST_MATCH_WINDOW_S = 600.0"),
        ("M1_REST_MATCH_WINDOW_S = 30.0", "M1_REST_MATCH_WINDOW_S = 0.0"),
        ("M1_MIN_REST_MATCHED = 50", "M1_MIN_REST_MATCHED = 1000"),
        ("M1_REST_EVERY_S = 600.0", "M1_REST_EVERY_S = 7200.0"),
        ("INTENT_TTL_S = 60.0", "INTENT_TTL_S = 300.0"),
        ("INTENT_TTL_S = 60.0", "INTENT_TTL_S = 0.0"),
        ("INTENT_SHARE_STEP = 0.01", "INTENT_SHARE_STEP = 0.0"),
        ("ASSUMED_SLIPPAGE = 0.005", "ASSUMED_SLIPPAGE = -0.005"),
        ("ASSUMED_SLIPPAGE = 0.005", "ASSUMED_SLIPPAGE = 1.0"),
        ("INTENT_ASSUMED_SLIPPAGE = ASSUMED_SLIPPAGE", "INTENT_ASSUMED_SLIPPAGE = 0.004"),
        ("SPORTS_DEVIG_ASSUMED_SLIPPAGE = ASSUMED_SLIPPAGE", "SPORTS_DEVIG_ASSUMED_SLIPPAGE = 0.004"),
        ("MAX_SIGNAL_AGE_NEWS_S = 3600.0", "MAX_SIGNAL_AGE_NEWS_S = 0.0"),
        ("MAX_SOURCE_LOGIT_GAP = 1.0", "MAX_SOURCE_LOGIT_GAP = 0.0"),
        ("RESOLUTION_HOLD_HOURS = 48.0", "RESOLUTION_HOLD_HOURS = 0.0"),
        ("SECRET_VALUE_MIN_LEN = 16", "SECRET_VALUE_MIN_LEN = 64"),
        ("SECRET_VALUE_MIN_LEN = 16", "SECRET_VALUE_MIN_LEN = 16.0"),
        ("MNEMONIC_MIN_WORDS = 12", "MNEMONIC_MIN_WORDS = 13"),
        ("MNEMONIC_MIN_WORDS = 12", "MNEMONIC_MIN_WORDS = 24"),
        ("SECRET_HASH_PREFIX_CHARS = 8", "SECRET_HASH_PREFIX_CHARS = 64"),
        ("SECRET_HASH_PREFIX_CHARS = 8", "SECRET_HASH_PREFIX_CHARS = 0"),
        # Task AZ: M3 calibration.
        ("CALIBRATION_GROUP_MIN_MARKETS = 5", "CALIBRATION_GROUP_MIN_MARKETS = 50"),
        ("CALIBRATION_GROUP_MIN_MARKETS = 5", "CALIBRATION_GROUP_MIN_MARKETS = 0"),
        ("CALIBRATION_EQUAL_FREQ_BINS = 5", "CALIBRATION_EQUAL_FREQ_BINS = 2.5"),
        ("CALIBRATION_EQUAL_FREQ_MIN_PER_BIN = 6", "CALIBRATION_EQUAL_FREQ_MIN_PER_BIN = True"),
        ("CALIBRATION_BIN_EDGES = (0.0, 0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9, 1.0)",
         "CALIBRATION_BIN_EDGES = (0.0, 0.5, 0.4, 1.0)"),
        ("CALIBRATION_BIN_EDGES = (0.0, 0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9, 1.0)",
         "CALIBRATION_BIN_EDGES = (0.1, 0.5, 1.0)"),
        ("CALIBRATION_CI_LEVEL = 0.95  # bootstrap percentile intervals and Wilson intervals",
         "CALIBRATION_CI_LEVEL = 1.0"),
        ("CALIBRATION_LOGLOSS_CLIP = 0.01  # p clipped to [clip, 1 - clip] for log loss and the logit",
         "CALIBRATION_LOGLOSS_CLIP = 0.0"),
        ("CALIBRATION_MIN_SAMPLES = 30  # eligible samples for an overall verdict", "CALIBRATION_MIN_SAMPLES = 8"),
        ("CALIBRATION_EDGE_TIERS = (0.02, 0.04, 0.08)", "CALIBRATION_EDGE_TIERS = (0.04, 0.02)"),
        ("CALIBRATION_EXPIRY_TIERS_H = (24.0, 72.0, 336.0)", "CALIBRATION_EXPIRY_TIERS_H = ()"),
        # Task BB: Reflector proposal rules.
        ("REFLECTOR_MIN_EDGE_STEP = 0.01  # a MIN_EDGE tightening proposes MIN_EDGE + this",
         "REFLECTOR_MIN_EDGE_STEP = -0.01"),
        ("REFLECTOR_DISABLE_SLOPE_MAX = 0.5  # disable: calibration slope below this ...",
         "REFLECTOR_DISABLE_SLOPE_MAX = 0.9"),
        ("REFLECTOR_ECE_MAX = 0.05  # param_change: ECE above this and a judged bin outside its Wilson interval",
         "REFLECTOR_ECE_MAX = 0.0"),
        ("REFLECTOR_REJECT_COOLDOWN_DAYS = 7  # a rejected target is not proposed again for this many days ...",
         "REFLECTOR_REJECT_COOLDOWN_DAYS = 0"),
        ("REFLECTOR_MAX_PER_UTC_DAY = 3", "REFLECTOR_MAX_PER_UTC_DAY = 0"),
        # issue #52: the halt-state mutex constants.
        ("HALT_STATE_LOCK_TIMEOUT_S = 5.0", "HALT_STATE_LOCK_TIMEOUT_S = 0.0"),
        ("HALT_STATE_LOCK_POLL_S = 0.05", "HALT_STATE_LOCK_POLL_S = 5.0"),
        # Task CG (#145): the complementary-leg pairing allowance.
        ("PAIR_CROSS_TOLERANCE = 1e-9", "PAIR_CROSS_TOLERANCE = 0.0"),
        ("PAIR_CROSS_TOLERANCE = 1e-9", "PAIR_CROSS_TOLERANCE = 0.05"),
    ],
)
def test_an_inconsistent_edit_fails_at_import(line, replacement):
    source = SOURCE.read_text(encoding="utf-8")
    assert source.count(line + "\n") == 1
    with pytest.raises(RuntimeError, match="thresholds inconsistent"):
        exec(compile(source.replace(line + "\n", replacement + "\n"), "thresholds_edit", "exec"), {"__name__": "x"})


def test_sentinel_keeps_its_own_guard():
    source = (ROOT / "src" / "sentinel.py").read_text(encoding="utf-8")
    assert "if SENTINEL_STALE_S != MAX_BOOK_AGE_S:" in source


def test_thresholds_module_has_no_imports_and_reads_nothing():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert [(node.module, [alias.name for alias in node.names]) for node in imports] == [("__future__", ["annotations"])]
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not names & {"open", "load_risk", "RISK_PATH", "Path", "environ"}


OLD_LITERAL_NAMES = {
    "MAX_BOOK_AGE_S", "MIN_EDGE", "EDGE_TOLERANCE", "PAPER_EQUITY_USD", "STALE_AFTER_S", "SENTINEL_STALE_S",
    "SENTINEL_STALE_CONSECUTIVE", "WASH_WINDOW_S", "MIN_HOURS_TO_EXPIRY", "MIN_PRICE", "MAX_PRICE", "PRICE_MIN",
    "PRICE_MAX", "WINDOW_HOURS", "ASSUMED_SLIPPAGE", "OPEN_EPS", "MAX_PER_UTC_DAY", "SESSION_MIN_SCANNED",
    "MIN_SCANNED_MARKETS",
} | set(EXPECTED)


@pytest.mark.parametrize("path", sorted(p.name for p in (ROOT / "src").glob("*.py") if p.name != "thresholds.py"))
def test_no_module_defines_a_gathered_threshold_again(path):
    """A second literal definition would let the copies drift apart."""
    tree = ast.parse((ROOT / "src" / path).read_text(encoding="utf-8"))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            assert not (isinstance(target, ast.Name) and target.id in OLD_LITERAL_NAMES), (path, target.id)
    text = (ROOT / "src" / path).read_text(encoding="utf-8")
    assert '["scanned"] < 5' not in text
