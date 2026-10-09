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
# Task AI: with a stop callback (graceful shutdown), the recorder notices SIGINT/SIGTERM within this.
WS_STOP_POLL_S = 1.0

# --- M1 24-hour acceptance (Task AI) -------------------------------------------------------------
# Run defaults (operational: they decide what is recorded, not what passes).
# default --top: markets taken from the latest scanner batch (both outcome tokens each)
M1_TOP_MARKETS = 5
# 64 MiB: a JSONL file is rotated before it would exceed this
M1_ROTATE_BYTES = 67108864
# ... or once it has been open this long
M1_ROTATE_S = 3600.0
# REST /book comparison sample every 10 minutes
M1_REST_EVERY_S = 600.0
# ... of the first N recorded assets
M1_REST_ASSETS = 4
# report timeline resolution (coverage and book age are evaluated every step)
M1_REPORT_STEP_S = 1.0
# Acceptance criteria (M2 pending; conservative first values, flagged for Poly指挥 / the user).
# the run must last at least 24 h minus 1 min and end by its deadline
M1_MIN_RUN_S = 86340.0
# every asset: share of the run with a fresh book (age <= MAX_BOOK_AGE_S)
M1_MIN_COVERAGE = 0.99
# connect attempts after the first (reconnects plus failed connects) over the whole run: 2 per hour
M1_MAX_RECONNECTS = 48
# resyncs not caused by a disconnect, per asset per run
M1_MAX_GAP_RESYNCS_PER_ASSET = 24
# a REST sample is compared with the WS row nearest in exchange time within this
M1_REST_MATCH_WINDOW_S = 30.0
# at least this many REST samples must be compared
M1_MIN_REST_MATCHED = 50
# best bid / best ask disagreement among compared samples
M1_MAX_TOP_MISMATCH_RATE = 0.02
# REST samples with no live WS row within the window (or an unusable REST book_ts)
M1_MAX_REST_UNMATCHED_RATE = 0.10

# --- Data API v2 reconciliation (Task AK) ------------------------------------------------------
# M2 pending. Ledger vs Data API share amounts may differ by at most this (1e-6 = one on-chain base
# unit of a 6-decimal outcome token); anything more is a mismatch.
RECONCILE_SHARES_TOL = 1e-6
# Data API v2 contract facts (docs.polymarket.com), not choices: /v2/positions hides holdings below
# its 0.1-share dust floor (filter_type=TOKENS default) and /v2/trades serves fills of at least
# 0.01 shares. A ledger leg or fill below them cannot be verified and fails closed.
DATA_API_POSITIONS_DUST_SHARES = 0.1
DATA_API_TRADES_MIN_SHARES = 0.01
# M2 pending. /v2/status is read first each round: snapshot age_seconds + serving.lag_seconds above
# this (seconds behind chain head) means the API is too stale to reconcile against (fails closed).
DATA_API_MAX_LAG_S = 120.0
# M2 pending. The documented /v2/positions row identities (current_value = current_size x
# current_price, unrealized = current_value - entry_cost, total_pnl = realized + unrealized,
# total_cost = entry_cost + entry_fees) must hold within this many USDC, or the row is malformed.
DATA_API_IDENTITY_TOL_USDC = 1e-6

# --- M3 calibration snapshot (Task AN) ---------------------------------------------------------
# Hours a market must stay closed (Gamma closedTime) before it is written to data/resolutions.jsonl.
# Exactly 48 h passes. Same number as ENDGAME_WINDOW_HOURS by coincidence; they are not coupled.
RESOLUTION_HOLD_HOURS = 48.0


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
    if not 0.0 < WS_STOP_POLL_S <= WS_PING_INTERVAL_S:
        problems.append("WS_STOP_POLL_S must satisfy 0 < WS_STOP_POLL_S <= WS_PING_INTERVAL_S")
    for name, value in (("M1_TOP_MARKETS", M1_TOP_MARKETS), ("M1_ROTATE_BYTES", M1_ROTATE_BYTES),
                        ("M1_REST_ASSETS", M1_REST_ASSETS), ("M1_MIN_REST_MATCHED", M1_MIN_REST_MATCHED)):
        if not (isinstance(value, int) and not isinstance(value, bool) and value >= 1):
            problems.append(f"{name} must be a whole number >= 1")
    for name, value in (("M1_MAX_RECONNECTS", M1_MAX_RECONNECTS),
                        ("M1_MAX_GAP_RESYNCS_PER_ASSET", M1_MAX_GAP_RESYNCS_PER_ASSET)):
        if not (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
            problems.append(f"{name} must be a whole number >= 0")
    if not (M1_ROTATE_S > 0.0 and M1_REST_EVERY_S > 0.0 and M1_MIN_RUN_S > 0.0):
        problems.append("M1_ROTATE_S, M1_REST_EVERY_S and M1_MIN_RUN_S must be > 0")
    if not 0.0 < M1_REPORT_STEP_S <= WS_ROW_REFRESH_S:
        problems.append("M1_REPORT_STEP_S must satisfy 0 < M1_REPORT_STEP_S <= WS_ROW_REFRESH_S")
    if not 0.0 < M1_MIN_COVERAGE <= 1.0:
        problems.append("M1_MIN_COVERAGE must satisfy 0 < M1_MIN_COVERAGE <= 1")
    if not (0.0 <= M1_MAX_TOP_MISMATCH_RATE < 1.0 and 0.0 <= M1_MAX_REST_UNMATCHED_RATE < 1.0):
        problems.append("M1 mismatch and unmatched rates must be in [0, 1)")
    if not 0.0 < M1_REST_MATCH_WINDOW_S <= WS_ROW_REFRESH_S:
        problems.append("M1_REST_MATCH_WINDOW_S must satisfy 0 < M1_REST_MATCH_WINDOW_S <= WS_ROW_REFRESH_S")
    if M1_MIN_RUN_S / M1_REST_EVERY_S * M1_REST_ASSETS < M1_MIN_REST_MATCHED:
        problems.append("a full run must be able to collect M1_MIN_REST_MATCHED REST samples")
    if not 0.0 < RECONCILE_SHARES_TOL < DATA_API_TRADES_MIN_SHARES <= DATA_API_POSITIONS_DUST_SHARES:
        problems.append("reconcile: 0 < RECONCILE_SHARES_TOL < DATA_API_TRADES_MIN_SHARES <= DATA_API_POSITIONS_DUST_SHARES")
    if not (DATA_API_MAX_LAG_S > 0.0 and DATA_API_IDENTITY_TOL_USDC > 0.0):
        problems.append("DATA_API_MAX_LAG_S and DATA_API_IDENTITY_TOL_USDC must be > 0")
    if not LEDGER_OPEN_EPS <= RECONCILE_SHARES_TOL:
        problems.append("RECONCILE_SHARES_TOL must be >= LEDGER_OPEN_EPS")
    if isinstance(RESOLUTION_HOLD_HOURS, bool) or not isinstance(RESOLUTION_HOLD_HOURS, (int, float)) or not (
        RESOLUTION_HOLD_HOURS > 0.0
    ):
        problems.append("RESOLUTION_HOLD_HOURS must be > 0")
    return problems


_PROBLEMS = consistency_problems()
if _PROBLEMS:  # pragma: no cover - guards a future edit
    raise RuntimeError("thresholds inconsistent: " + "; ".join(_PROBLEMS))
