"""Phase 0 risk and threshold code constants, gathered in one place (Task AC).

Every value here is a code constant, not a config/risk.toml key. Values are exactly the ones
the modules used before Task AC; the modules now import them from here and keep their old
names (risk_gate.MIN_EDGE, sentinel.SENTINEL_STALE_S, endgame.PRICE_MIN, ...) as re-exports.
config/risk.toml keys stay in config/risk.toml, governed by that file and its pinned sha256;
none of them is copied here.

docs/THRESHOLDS.md lists every constant (value, unit, meaning, source task, M2 status);
tests/test_thresholds.py keeps the doc, this module and the old names in sync. Changing a value
is a threshold change: the ones marked M2 wait for the user's decision. This module imports
nothing from src, so any module can import it without a cycle.
"""
from __future__ import annotations

# --- Book staleness / RiskGate -------------------------------------------------------------
# Task L: a book older than this (seconds, from the exchange book_ts) is stale and RiskGate vetoes.
MAX_BOOK_AGE_S = 120.0
# Task X (backlog #6): minimum edge after slippage and fees. Inclusive (exactly 0.02 passes);
# EDGE_TOLERANCE only absorbs float rounding (0.425 - 0.405 = 0.01999...96).
MIN_EDGE = 0.02
EDGE_TOLERANCE = 1e-12
# Local paper bankroll used for sizing and the daily-loss ratio. Not a wallet.
PAPER_EQUITY_USD = 1000.0

# --- Paper cycle ---------------------------------------------------------------------------
# Task D, aligned with MAX_BOOK_AGE_S in Tasks R/T: per-check BookWatcher stale signal (seconds).
PAPER_CYCLE_STALE_AFTER_S = 120

# --- Sentinel (Task Z, backlog #9) ---------------------------------------------------------
SENTINEL_STALE_S = 120.0
SENTINEL_STALE_CONSECUTIVE = 2

# --- Wash / self-trade (Task S, backlog #1) ------------------------------------------------
WASH_WINDOW_S = 300.0

# --- Scanner -------------------------------------------------------------------------------
# Task O (backlog #8): a market must end at least this many hours after now.
MIN_HOURS_TO_EXPIRY = 24.0
# Task I: long-tail band defaults (YES price). risk.toml may override with scanner_min_price /
# scanner_max_price; neither key is in the file today, so these are the values in force.
SCANNER_MIN_PRICE = 0.03
SCANNER_MAX_PRICE = 0.97
# Task I: fewer scanned markets than this means a broken public read (scanner exit 1);
# endgame_scan (Task W) and paper_session (Task AB) use the same floor.
MIN_SCANNED_MARKETS = 5

# --- Endgame (Task F; second path Task W) --------------------------------------------------
# Price band (inclusive) and the latest end time. With the scanner's MIN_HOURS_TO_EXPIRY floor,
# endgame_scan signals only markets ending 24-48 h from now.
ENDGAME_PRICE_MIN = 0.98
ENDGAME_PRICE_MAX = 0.99
ENDGAME_WINDOW_HOURS = 48.0

# --- Other signal and ledger constants -----------------------------------------------------
# Task H: SportsDevig's assumed slippage (price units) before an edge counts.
SPORTS_DEVIG_ASSUMED_SLIPPAGE = 0.005
# Task P: a leg with at most this many open shares counts as closed.
LEDGER_OPEN_EPS = 1e-9
# Task B: at most this many Reflector proposals per UTC day.
REFLECTOR_MAX_PER_UTC_DAY = 3

# --- WebSocket book recorder (Task AG, M1): operational timing, not risk thresholds ----------
# None of these makes a book look fresher: freshness is always the exchange book_ts against
# MAX_BOOK_AGE_S, and a book that is resyncing writes an invalidation row (stale at once).
# Reconnect backoff after a disconnect or failed connect: base * 2**attempt, capped.
WS_BACKOFF_BASE_S = 1.0
WS_BACKOFF_CAP_S = 60.0
# How long an asset may wait for a fresh snapshot (after subscribe or a detected gap) before
# the recorder drops the connection and reconnects.
WS_RESYNC_TIMEOUT_S = 30.0
# Application heartbeat: send the text frame PING this often (docs.polymarket.com market channel).
WS_PING_INTERVAL_S = 10.0
# Staleness-relevant: a live book whose top of book does not change still gets a new row once the
# EXCHANGE time of the latest applied event is this far past the last written row, so a deep but
# quiet-top book does not look stale while its deltas keep arriving. Rows can only lag reality by
# this much (never lead it); it must stay well below MAX_BOOK_AGE_S.
WS_ROW_REFRESH_S = 30.0


def consistency_problems() -> list[str]:
    """Relations the values must keep. Import fails if any is broken (explicit, survives -O)."""
    problems = []
    if SENTINEL_STALE_S != MAX_BOOK_AGE_S:
        problems.append("SENTINEL_STALE_S must equal MAX_BOOK_AGE_S (Sentinel and RiskGate share one per-check limit)")
    if PAPER_CYCLE_STALE_AFTER_S != MAX_BOOK_AGE_S:
        problems.append("PAPER_CYCLE_STALE_AFTER_S must equal MAX_BOOK_AGE_S")
    if not (isinstance(SENTINEL_STALE_CONSECUTIVE, int) and SENTINEL_STALE_CONSECUTIVE >= 1):
        problems.append("SENTINEL_STALE_CONSECUTIVE must be a whole number >= 1")
    if not 0.0 <= SCANNER_MIN_PRICE < SCANNER_MAX_PRICE <= 1.0:
        problems.append("scanner band must satisfy 0 <= SCANNER_MIN_PRICE < SCANNER_MAX_PRICE <= 1")
    if not SCANNER_MAX_PRICE < ENDGAME_PRICE_MIN <= ENDGAME_PRICE_MAX < 1.0:
        problems.append("Endgame band must sit above the scanner band and below 1")
    if not 0.0 < MIN_HOURS_TO_EXPIRY < ENDGAME_WINDOW_HOURS:
        problems.append("Endgame window must be non-empty: 0 < MIN_HOURS_TO_EXPIRY < ENDGAME_WINDOW_HOURS")
    if not 0.0 < WS_BACKOFF_BASE_S <= WS_BACKOFF_CAP_S:
        problems.append("WebSocket backoff must satisfy 0 < WS_BACKOFF_BASE_S <= WS_BACKOFF_CAP_S")
    if not 0.0 < WS_PING_INTERVAL_S < WS_RESYNC_TIMEOUT_S:
        problems.append("WebSocket timing must satisfy 0 < WS_PING_INTERVAL_S < WS_RESYNC_TIMEOUT_S")
    if not 0.0 < WS_ROW_REFRESH_S < MAX_BOOK_AGE_S:
        problems.append("WS_ROW_REFRESH_S must satisfy 0 < WS_ROW_REFRESH_S < MAX_BOOK_AGE_S")
    return problems


_PROBLEMS = consistency_problems()
if _PROBLEMS:  # pragma: no cover - guards a future edit
    raise RuntimeError("thresholds inconsistent: " + "; ".join(_PROBLEMS))
