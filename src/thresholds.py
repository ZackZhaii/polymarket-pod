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
# Task AT: the ONE assumed slippage (price units per share) added to the price before an edge counts.
# M2 pending. Every user reads this value; the per-user names below are aliases, so no copy can drift
# (consistency_problems fails the import if one does). 0.005 is the value SportsDevig (Task H), the
# intent builder (Task AO), the paper_ledger CLI default and self-check and the e2e fixtures all used
# before Task AT; no value moved.
ASSUMED_SLIPPAGE = 0.005
# Task H: SportsDevig's assumed slippage before an edge counts. Alias of ASSUMED_SLIPPAGE (Task AT).
SPORTS_DEVIG_ASSUMED_SLIPPAGE = ASSUMED_SLIPPAGE
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
# Task AW: no PONG within this long after a PING = the connection is dead; the recorder reconnects
# (counted toward the Task AP reconnect escalation). 30 s per Poly研究's notes on the market channel.
# M2 pending.
WS_PONG_TIMEOUT_S = 30.0
# Task BI: no inbound frame of any kind (book data or PONG) for this long = a silent connection; the
# recorder closes it and reconnects (reason "silent", full resync), counted like any disconnect.
# Backstop next to WS_PONG_TIMEOUT_S that does not depend on the PING / PONG bookkeeping. M2 pending.
WS_SILENCE_TIMEOUT_S = 60.0
# Task BI evidence, not a setting: the longest inbound data silence across all assets in the healthy
# periods of M1 run m1-20261008T2259Z (Poly盘口's analysis). WS_SILENCE_TIMEOUT_S must stay above it.
WS_SILENCE_OBSERVED_HEALTHY_MAX_S = 38.7
# Task BI: the recorder writes a ping_latency event (PING / PONG round trips, unanswered PINGs) every this
# many PINGs and at the end of each connection. Operational (logging only).
WS_LATENCY_SUMMARY_PINGS = 60
# Task AY: opt-in raw book snapshots (src.m1_run --raw-books), kept to validate the snapshot hash
# offline (src.ws_hash_check). One raw_books file never passes the file cap (nor --rotate-mb); once a
# run has written the run cap, further frames are dropped (counted), the recording goes on. M2 pending.
WS_RAW_BOOK_FILE_MAX_BYTES = 16 * 1024 * 1024
WS_RAW_BOOK_RUN_MAX_BYTES = 256 * 1024 * 1024
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


# --- Signal -> OrderIntent (Task AO, forecaster/SPEC_signal_to_intent.md) ------------------------
# src/intent_builder.py only. Every one can only reject or shrink an intent (RiskGate still re-checks).
# M2 pending. Price units added to the book-walk price before the edge counts; RiskGate gets the same
# value. Alias of ASSUMED_SLIPPAGE (Task AT), so it moves only with it.
INTENT_ASSUMED_SLIPPAGE = ASSUMED_SLIPPAGE
# Share rounding step: shares are rounded DOWN to a multiple of this (never up past the size).
INTENT_SHARE_STEP = 0.01
# M2 pending. An intent expires at the earliest of now + this, book_ts + MAX_BOOK_AGE_S and
# end_date - MIN_HOURS_TO_EXPIRY. Must be <= MAX_BOOK_AGE_S.
INTENT_TTL_S = 60.0
# M2 pending. A signal older than this (seconds, now - signal_ts) is stale_signal. Per source;
# forecaster_override follows news, a blend gets the smallest of the three.
MAX_SIGNAL_AGE_NEWS_S = 3600.0
MAX_SIGNAL_AGE_SPORTS_DEVIG_S = 300.0
MAX_SIGNAL_AGE_ENDGAME_CONFIRM_S = 600.0
# M2 pending. Two model sources for one outcome whose point estimates differ by more than this in
# log-odds are source_conflict: no blend, no intent.
MAX_SOURCE_LOGIT_GAP = 1.0

# --- Secret-string scan of the bus and logs (Task AQ, Sentinel design gap 9) ------------------
# src/secret_scan.py only. A hit is a global halt (SEV-1); each can only widen what is caught.
# M2 pending. A value after a credential key name (private_key=, "api_secret": ...) counts as a
# secret when it is at least this many token characters with a letter and a digit. Must be <= 32.
SECRET_VALUE_MIN_LEN = 16
# M2 pending. This many consecutive BIP39 words on one line is a mnemonic. Must be <= 12 (the
# shortest standard phrase), so no 12-word phrase can slip through.
MNEMONIC_MIN_WORDS = 12
# M2 pending. Hex characters of the matched value's sha256 that a finding reports (to tell hits
# apart without echoing them). Kept between 4 and 12 so the prefix never identifies a value.
SECRET_HASH_PREFIX_CHARS = 8

# --- Heartbeats and the resident Sentinel tick (Task AP, Poly哨兵 design gaps 2, 12, 13, 14) -------
# Every value is a first value from the Sentinel design review (dev@f760e51), M2 pending. A
# heartbeat older than its timeout (or unreadable, corrupt, malformed, or lost after it was written
# once) is stale; what a stale heartbeat does (alert, global halt) is in src/sentinel_tick.py.
# M2 pending. ws_recorder and m1_run main loop: a heartbeat at least this often (also while backing off).
HEARTBEAT_RECORDER_EVERY_S = 10.0
# M2 pending. ... and stale (global halt) once it is this old.
HEARTBEAT_RECORDER_TIMEOUT_S = 120.0
# M2 pending. m1_run REST sampler: stale (alert) once its heartbeat (one per sample) is this old.
HEARTBEAT_REST_TIMEOUT_S = 900.0
# M2 pending. Scanner (and endgame_scan) write start/finish: a start with no finish after this is hung (alert).
SCANNER_HANG_S = 1800.0
# M2 pending. The latest successful scan (exit 0) older than this raises an alert.
SCANNER_MAX_SUCCESS_AGE_S = 86400.0
# M2 pending. Consecutive scanner exit-3 (incomplete paging / network) runs that raise an alert.
SCANNER_INCOMPLETE_CONSECUTIVE = 2
# M2 pending. paper_cycle --loop: one cycle (and heartbeat) this often; stale (alert) after the
# timeout and a global halt after HEARTBEAT_PAPER_CYCLE_HALT_FACTOR x the timeout.
HEARTBEAT_PAPER_CYCLE_EVERY_S = 60.0
HEARTBEAT_PAPER_CYCLE_TIMEOUT_S = 180.0
HEARTBEAT_PAPER_CYCLE_HALT_FACTOR = 2
# M2 pending. A polling halt consumer: heartbeat this often, stale (global halt) after the timeout.
HEARTBEAT_CONSUMER_EVERY_S = 5.0
HEARTBEAT_CONSUMER_TIMEOUT_S = 30.0
# M2 pending. The resident Sentinel ticks this often; once it has beaten once, a Sentinel heartbeat
# older than the timeout is a global halt for every consumer (RiskGate, Maker, paper_cycle).
SENTINEL_TICK_S = 30.0
SENTINEL_HEARTBEAT_TIMEOUT_S = 90.0
# M2 pending. Recorder escalation (gap 13), over a sliding window: this many reconnects (connect
# attempts after the first) raise an alert; this many gap resyncs (not caused by a disconnect) of
# one asset halt its market (and alert).
RECORDER_ESCALATION_WINDOW_S = 600.0
RECORDER_RECONNECT_ALERT_COUNT = 4
RECORDER_GAP_RESYNC_HALT_COUNT = 3
# M2 pending. Gap 14: every checked market stale in the same paper cycle, this many cycles in a row,
# is a global halt.
ALL_STALE_CONSECUTIVE_CYCLES = 2


# --- Halt-state read-modify-write mutex (issue #52) ------------------------------------------
# M2 pending. record_halt and a normal-record resume share one cross-process mutex and hold it
# across the halt-state read, the validation and the write-back, so two processes can never
# interleave those steps and overwrite each other's halt records (a market resume used to drop a
# concurrent new global halt). Fail closed when the mutex cannot be taken within this long
# (seconds): a resume releases nothing (halt_lock_failed), record_halt goes to logs/HALT.
HALT_STATE_LOCK_TIMEOUT_S = 5.0
# M2 pending. Retry step (seconds) while waiting for the halt-state mutex.
HALT_STATE_LOCK_POLL_S = 0.05


# --- Runtime pin checks (Task AS, Poly哨兵 design gaps 5 and 6) -----------------------------
# M2 pending. A running ws_recorder / m1_run (and paper_cycle --loop, every cycle) re-reads
# config/risk.toml and checks every pinned key and its sha256 at least this often; drift stops the
# run and records a global halt. Must not exceed SENTINEL_HEARTBEAT_TIMEOUT_S.
PIN_RECHECK_S = 60.0

# --- M3 calibration report (Task AZ, Poly反思's M3 calibration design sections 2 and 3) -----------
# M3 pending (the user decides at M3). Below these the report says "insufficient" and gives no verdict.
CALIBRATION_MIN_SAMPLES = 30  # eligible samples for an overall verdict
CALIBRATION_MIN_MARKETS = 20  # distinct markets (condition_id) among them
CALIBRATION_MIN_RESOLUTION_DAYS = 5  # distinct UTC days of closedTime among them
CALIBRATION_GROUP_MIN_SAMPLES = 10  # a category / strategy / model_version / bin group with a verdict
CALIBRATION_GROUP_MIN_MARKETS = 5
CALIBRATION_FIXED_BINS_MIN_SAMPLES = 100  # fewer samples: equal-frequency bins instead of the fixed edges
CALIBRATION_EQUAL_FREQ_BINS = 5
CALIBRATION_EQUAL_FREQ_MIN_PER_BIN = 6
CALIBRATION_MAX_DECISIONS_PER_MARKET = 2  # trade sample: re-entries beyond this per market are not scored
CALIBRATION_BOOTSTRAP_RESAMPLES = 2000  # resampled by market (condition_id)
CALIBRATION_BOOTSTRAP_SEED = 20261009
CALIBRATION_CI_LEVEL = 0.95  # bootstrap percentile intervals and Wilson intervals
CALIBRATION_LOGLOSS_CLIP = 0.01  # p clipped to [clip, 1 - clip] for log loss and the logit
# Fixed reliability bins (design 2.3): finer at both ends. Not a number constant; checked below.
CALIBRATION_BIN_EDGES = (0.0, 0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9, 1.0)
# Trade-sample groups (design 2.5 / 2.6): edge tiers [0.02, 0.04) [0.04, 0.08) [0.08, ...) and hours to expiry at
# the first fill [24, 72) [72, 336) [336, ...). Grouping labels only; nothing is filtered by them.
CALIBRATION_EDGE_TIERS = (0.02, 0.04, 0.08)
CALIBRATION_EXPIRY_TIERS_H = (24.0, 72.0, 336.0)
# Task BD (design 3, "the largest event cluster holds at most 30% of the evidence"): the report flags a sufficient
# section "concentrated" when its largest cluster holds more than this share of the samples. Reported only.
CALIBRATION_MAX_CLUSTER_SHARE = 0.30

# --- M3 Reflector proposal rules (Task BB, Poly反思's M3 calibration design section 4) -------------------
# M3 pending (the user decides at M3). Tightening triggers read from report.json intervals only; the daily cap is
# REFLECTOR_MAX_PER_UTC_DAY. Proposals are never applied by code.
REFLECTOR_DISABLE_REALIZATION_MAX = 0.25  # disable: realized P&L CI below 0 and realization below this
REFLECTOR_DISABLE_SLOPE_MAX = 0.5  # disable: calibration slope below this ...
REFLECTOR_DISABLE_SLOPE_CI_HIGH_MAX = 0.8  # ... and its CI upper bound below this
REFLECTOR_TIGHTEN_SLOPE_MAX = 0.8  # param_change: slope in [DISABLE_SLOPE_MAX, this) ...
REFLECTOR_TIGHTEN_SLOPE_CI_HIGH_MAX = 1.0  # ... and its CI upper bound below this
REFLECTOR_ECE_MAX = 0.05  # param_change: ECE above this and a judged bin outside its Wilson interval
REFLECTOR_MIN_EDGE_STEP = 0.01  # a MIN_EDGE tightening proposes MIN_EDGE + this
REFLECTOR_REJECT_COOLDOWN_DAYS = 7  # a rejected target is not proposed again for this many days ...
REFLECTOR_REJECT_NEW_SAMPLES = 15  # ... unless the sample grew by at least this many
# Task BE (design section 4, per-edge-tier realization): the bottom edge tiers whose realization interval lies
# wholly below this get a MIN_EDGE tightening to the top of the highest such tier. M3 pending.
REFLECTOR_EDGE_TIER_REALIZATION_MAX = 0.25


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
    if not (isinstance(WS_RAW_BOOK_FILE_MAX_BYTES, int) and isinstance(WS_RAW_BOOK_RUN_MAX_BYTES, int)
            and 0 < WS_RAW_BOOK_FILE_MAX_BYTES <= WS_RAW_BOOK_RUN_MAX_BYTES):
        problems.append("WS_RAW_BOOK_*_MAX_BYTES must be whole numbers with 0 < file cap <= run cap")
    if not 0.0 < WS_PING_INTERVAL_S < WS_PONG_TIMEOUT_S:
        problems.append("WS_PONG_TIMEOUT_S must satisfy 0 < WS_PING_INTERVAL_S < WS_PONG_TIMEOUT_S")
    if not 0.0 < WS_STOP_POLL_S <= WS_PING_INTERVAL_S:
        problems.append("WS_STOP_POLL_S must satisfy 0 < WS_STOP_POLL_S <= WS_PING_INTERVAL_S")
    if not WS_PONG_TIMEOUT_S < WS_SILENCE_TIMEOUT_S:
        problems.append("WS_SILENCE_TIMEOUT_S must satisfy WS_PONG_TIMEOUT_S < WS_SILENCE_TIMEOUT_S")
    if not 0.0 < WS_SILENCE_OBSERVED_HEALTHY_MAX_S < WS_SILENCE_TIMEOUT_S:
        problems.append("WS_SILENCE_TIMEOUT_S must exceed the observed healthy silence "
                        "(0 < WS_SILENCE_OBSERVED_HEALTHY_MAX_S < WS_SILENCE_TIMEOUT_S)")
    if not (isinstance(WS_LATENCY_SUMMARY_PINGS, int) and not isinstance(WS_LATENCY_SUMMARY_PINGS, bool)
            and WS_LATENCY_SUMMARY_PINGS >= 1):
        problems.append("WS_LATENCY_SUMMARY_PINGS must be a whole number >= 1")
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
    if not 0.0 <= ASSUMED_SLIPPAGE < 1.0:
        problems.append("ASSUMED_SLIPPAGE must satisfy 0 <= ASSUMED_SLIPPAGE < 1")
    if not SPORTS_DEVIG_ASSUMED_SLIPPAGE == INTENT_ASSUMED_SLIPPAGE == ASSUMED_SLIPPAGE:
        problems.append("SPORTS_DEVIG_ASSUMED_SLIPPAGE and INTENT_ASSUMED_SLIPPAGE must equal ASSUMED_SLIPPAGE "
                        "(Task AT: one slippage)")
    if not 0.0 <= INTENT_ASSUMED_SLIPPAGE < 1.0:
        problems.append("INTENT_ASSUMED_SLIPPAGE must satisfy 0 <= INTENT_ASSUMED_SLIPPAGE < 1")
    if not 0.0 < INTENT_SHARE_STEP <= 1.0:
        problems.append("INTENT_SHARE_STEP must satisfy 0 < INTENT_SHARE_STEP <= 1")
    if not 0.0 < INTENT_TTL_S <= MAX_BOOK_AGE_S:
        problems.append("INTENT_TTL_S must satisfy 0 < INTENT_TTL_S <= MAX_BOOK_AGE_S")
    if not (MAX_SIGNAL_AGE_NEWS_S > 0.0 and MAX_SIGNAL_AGE_SPORTS_DEVIG_S > 0.0
            and MAX_SIGNAL_AGE_ENDGAME_CONFIRM_S > 0.0):
        problems.append("MAX_SIGNAL_AGE_*_S must be > 0")
    if not MAX_SOURCE_LOGIT_GAP > 0.0:
        problems.append("MAX_SOURCE_LOGIT_GAP must be > 0")
    if not 0.0 < HEARTBEAT_RECORDER_EVERY_S < HEARTBEAT_RECORDER_TIMEOUT_S <= MAX_BOOK_AGE_S:
        problems.append("0 < HEARTBEAT_RECORDER_EVERY_S < HEARTBEAT_RECORDER_TIMEOUT_S <= MAX_BOOK_AGE_S")
    if not 0.0 < HEARTBEAT_REST_TIMEOUT_S:
        problems.append("HEARTBEAT_REST_TIMEOUT_S must be > 0")
    if not 0.0 < SCANNER_HANG_S < SCANNER_MAX_SUCCESS_AGE_S:
        problems.append("0 < SCANNER_HANG_S < SCANNER_MAX_SUCCESS_AGE_S")
    if not 0.0 < HEARTBEAT_PAPER_CYCLE_EVERY_S < HEARTBEAT_PAPER_CYCLE_TIMEOUT_S:
        problems.append("0 < HEARTBEAT_PAPER_CYCLE_EVERY_S < HEARTBEAT_PAPER_CYCLE_TIMEOUT_S")
    if not 0.0 < HEARTBEAT_CONSUMER_EVERY_S < HEARTBEAT_CONSUMER_TIMEOUT_S:
        problems.append("0 < HEARTBEAT_CONSUMER_EVERY_S < HEARTBEAT_CONSUMER_TIMEOUT_S")
    if not 0.0 < SENTINEL_TICK_S < SENTINEL_HEARTBEAT_TIMEOUT_S:
        problems.append("0 < SENTINEL_TICK_S < SENTINEL_HEARTBEAT_TIMEOUT_S")
    if not RECORDER_ESCALATION_WINDOW_S > 0.0:
        problems.append("RECORDER_ESCALATION_WINDOW_S must be > 0")
    for name, value in (("SCANNER_INCOMPLETE_CONSECUTIVE", SCANNER_INCOMPLETE_CONSECUTIVE),
                        ("HEARTBEAT_PAPER_CYCLE_HALT_FACTOR", HEARTBEAT_PAPER_CYCLE_HALT_FACTOR),
                        ("RECORDER_RECONNECT_ALERT_COUNT", RECORDER_RECONNECT_ALERT_COUNT),
                        ("RECORDER_GAP_RESYNC_HALT_COUNT", RECORDER_GAP_RESYNC_HALT_COUNT),
                        ("ALL_STALE_CONSECUTIVE_CYCLES", ALL_STALE_CONSECUTIVE_CYCLES)):
        if not (isinstance(value, int) and not isinstance(value, bool) and value >= 1):
            problems.append(f"{name} must be a whole number >= 1")
    if isinstance(RESOLUTION_HOLD_HOURS, bool) or not isinstance(RESOLUTION_HOLD_HOURS, (int, float)) or not (
        RESOLUTION_HOLD_HOURS > 0.0
    ):
        problems.append("RESOLUTION_HOLD_HOURS must be > 0")
    for name, value, low, high in (("SECRET_VALUE_MIN_LEN", SECRET_VALUE_MIN_LEN, 8, 32),
                                   ("MNEMONIC_MIN_WORDS", MNEMONIC_MIN_WORDS, 3, 12),
                                   ("SECRET_HASH_PREFIX_CHARS", SECRET_HASH_PREFIX_CHARS, 4, 12)):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            problems.append(f"{name} must be a whole number in [{low}, {high}]")
    if isinstance(PIN_RECHECK_S, bool) or not isinstance(PIN_RECHECK_S, (int, float)) or not (
        0.0 < PIN_RECHECK_S <= SENTINEL_HEARTBEAT_TIMEOUT_S
    ):
        problems.append("PIN_RECHECK_S must be in (0, SENTINEL_HEARTBEAT_TIMEOUT_S]")
    if not 0.0 < HALT_STATE_LOCK_POLL_S < HALT_STATE_LOCK_TIMEOUT_S:
        problems.append("halt-state mutex must satisfy 0 < HALT_STATE_LOCK_POLL_S < HALT_STATE_LOCK_TIMEOUT_S")
    # Task AZ: M3 calibration report.
    for name in ("CALIBRATION_MIN_SAMPLES", "CALIBRATION_MIN_MARKETS", "CALIBRATION_MIN_RESOLUTION_DAYS",
                 "CALIBRATION_GROUP_MIN_SAMPLES", "CALIBRATION_GROUP_MIN_MARKETS",
                 "CALIBRATION_FIXED_BINS_MIN_SAMPLES", "CALIBRATION_EQUAL_FREQ_BINS",
                 "CALIBRATION_EQUAL_FREQ_MIN_PER_BIN", "CALIBRATION_MAX_DECISIONS_PER_MARKET", "CALIBRATION_BOOTSTRAP_RESAMPLES", "CALIBRATION_BOOTSTRAP_SEED"):
        value = globals()[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            problems.append(f"{name} must be a whole number >= 1")
    if not (CALIBRATION_GROUP_MIN_SAMPLES <= CALIBRATION_MIN_SAMPLES
            and CALIBRATION_GROUP_MIN_MARKETS <= CALIBRATION_MIN_MARKETS <= CALIBRATION_MIN_SAMPLES):
        problems.append("calibration group minimums must not exceed the overall ones (markets <= samples)")
    if not CALIBRATION_MIN_SAMPLES <= CALIBRATION_FIXED_BINS_MIN_SAMPLES:
        problems.append("CALIBRATION_FIXED_BINS_MIN_SAMPLES must be >= CALIBRATION_MIN_SAMPLES")
    if not 0.5 < CALIBRATION_CI_LEVEL < 1:
        problems.append("CALIBRATION_CI_LEVEL must be in (0.5, 1)")
    if not 0 < CALIBRATION_LOGLOSS_CLIP < 0.5:
        problems.append("CALIBRATION_LOGLOSS_CLIP must be in (0, 0.5)")
    edges = CALIBRATION_BIN_EDGES
    if (not isinstance(edges, tuple) or len(edges) < 3 or edges[0] != 0.0 or edges[-1] != 1.0
            or any(not a < b for a, b in zip(edges, edges[1:]))):
        problems.append("CALIBRATION_BIN_EDGES must rise strictly from 0.0 to 1.0")
    for name in ("CALIBRATION_EDGE_TIERS", "CALIBRATION_EXPIRY_TIERS_H"):
        tiers = globals()[name]
        if (not isinstance(tiers, tuple) or not tiers or tiers[0] <= 0
                or any(not a < b for a, b in zip(tiers, tiers[1:]))):
            problems.append(f"{name} must be positive and rise strictly")
    if (isinstance(CALIBRATION_MAX_CLUSTER_SHARE, bool) or not isinstance(CALIBRATION_MAX_CLUSTER_SHARE, (int, float))
            or not 0 < CALIBRATION_MAX_CLUSTER_SHARE <= 1):
        problems.append("CALIBRATION_MAX_CLUSTER_SHARE must be in (0, 1]")
    # Task BB: M3 Reflector proposal rules.
    if not 0 < REFLECTOR_DISABLE_SLOPE_MAX < REFLECTOR_TIGHTEN_SLOPE_MAX <= 1:
        problems.append("need 0 < REFLECTOR_DISABLE_SLOPE_MAX < REFLECTOR_TIGHTEN_SLOPE_MAX <= 1")
    if not 0 < REFLECTOR_DISABLE_SLOPE_CI_HIGH_MAX <= REFLECTOR_TIGHTEN_SLOPE_CI_HIGH_MAX <= 1:
        problems.append("need 0 < REFLECTOR_DISABLE_SLOPE_CI_HIGH_MAX <= REFLECTOR_TIGHTEN_SLOPE_CI_HIGH_MAX <= 1")
    if not 0 <= REFLECTOR_DISABLE_REALIZATION_MAX < 1:
        problems.append("REFLECTOR_DISABLE_REALIZATION_MAX must be in [0, 1)")
    if (isinstance(REFLECTOR_EDGE_TIER_REALIZATION_MAX, bool)
            or not isinstance(REFLECTOR_EDGE_TIER_REALIZATION_MAX, (int, float))
            or not 0 <= REFLECTOR_EDGE_TIER_REALIZATION_MAX < 1):
        problems.append("REFLECTOR_EDGE_TIER_REALIZATION_MAX must be in [0, 1)")
    if not 0 < REFLECTOR_ECE_MAX < 1:
        problems.append("REFLECTOR_ECE_MAX must be in (0, 1)")
    if not 0 < REFLECTOR_MIN_EDGE_STEP < 1:
        problems.append("REFLECTOR_MIN_EDGE_STEP must be in (0, 1) (a tightening step, never negative)")
    for name in ("REFLECTOR_REJECT_COOLDOWN_DAYS", "REFLECTOR_REJECT_NEW_SAMPLES", "REFLECTOR_MAX_PER_UTC_DAY"):
        value = globals()[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            problems.append(f"{name} must be a whole number >= 1")
    return problems


_PROBLEMS = consistency_problems()
if _PROBLEMS:  # pragma: no cover - guards a future edit
    raise RuntimeError("thresholds inconsistent: " + "; ".join(_PROBLEMS))
