"""Offline paper cycle.

Reads local market and book jsonl. Emits gap and staleness signals only.
Book age uses src/book_staleness.py, the same parser and boundary as RiskGate, and is read
from the exchange's book_ts (book.book_timestamp), never the recorder's ts (Task T).
A stale market gets no gap signal: the BookWatcher stale signal is its skip record.
The latest market/book row is chosen by parsed time; unparseable rows are skipped and recorded.
Task AA: every market with at least one recorded book is also fed to Sentinel's StaleWatch, whose
streaks persist in logs/sentinel_stale.json (SENTINEL_STATE_PATH, written atomically). Two stale or
failed checks in a row halt that market's paper quotes. A missing (after a prior run), unreadable,
corrupt or malformed state file, or a failed write, counts as one more stale check (fail closed).
This only adds the alarm: the per-check stale signal and the skipped gap signal are unchanged.
Task AL (Sentinel design gaps 1 and 10): a leg with no recorded book row (or a market with no
tokens) is stale (missing_book), so a market without a book is checked by StaleWatch instead of
skipped. A market covered by an active halt (src/halt_store.py: its own, a global one, a bad
halt state file or logs/HALT) gets no gap signal; the skip is recorded as halted_market.
Task AP (Sentinel design gaps 11, 12, 14):
- gap 11: ``recording_dir`` (CLI ``--recording-dir RUN_DIR``) reads the M1 run's rotated book files
  ``RUN_DIR/books/books-*.jsonl`` (src.m1_run) instead of data/books.jsonl, so StaleWatch sees the
  recorder's rows during M1. A recording dir with no book file makes every leg missing_book (stale).
- gap 14: every checked market stale in the same cycle, ALL_STALE_CONSECUTIVE_CYCLES cycles in a
  row (the count persists in the StaleWatch state), is a GLOBAL Sentinel halt. If the Operator
  resumes it while every market is still stale, the next such cycle halts again at once.
- gap 12: ``python -m src.paper_cycle --loop`` runs a cycle every HEARTBEAT_PAPER_CYCLE_EVERY_S and
  writes logs/heartbeat/paper_cycle.json each cycle (a one-shot run writes start and finish).
Issue #125 (Task BN, Poly指挥's ruling on the #88 conflict), with ``recording_dir`` only:
- (b) only markets in ``RUN_DIR/manifest.json`` (``assets[].market_id``, schema m1_run/1) may produce a gap
  signal. A missing, unreadable, corrupt, wrong-schema or empty manifest lets no market produce one (fail
  closed, never "all markets"); every market then stays under StaleWatch exactly as before.
- (c) a market outside the manifest is rejected as ``not_recorded``: no gap signal, one BookWatcher signal
  (``skip_reason`` not_recorded, its stale reasons still recorded and alarmed) and a ``not_recorded_market``
  skip, but no StaleWatch check, so a book it was never going to have no longer halts it market by market.
- (d) except a market with an open paper position (paper ledger): it stays under StaleWatch (stale or missing
  book -> market halt after the usual consecutive checks) and still gets no gap signal when outside the
  manifest. A ledger that cannot be read in full keeps every market under StaleWatch (fail closed).
- gap 14's all_markets_stale counts only the StaleWatch markets: the manifest's plus the held ones.
Without ``recording_dir`` nothing changes.
Issue #141 (Task BX), tightening only:
- a market past its end date (``expired``) or in UMA resolution (``market_expired_or_resolving``, the scanner's
  predicate in src/structural_gate.py) gets no gap path at all: one BookWatcher signal (``gap_signal`` skipped,
  every reason listed) and a ``resolving_market`` skip. It is still checked by StaleWatch first, as before.
- every ArbBinary / NegRiskArb gap (a YES+NO or all-outcome complete set: two or more legs) is rejected as
  ``structural_arb_disabled``: the bus record says ``gap_signal`` rejected, and a ``structural_arb_disabled`` skip is
  added. ``gaps`` still counts the gaps seen (all rejected; ``structural_rejected`` is the same count). Nothing
  multi-leg ever becomes an intent or an order.
No network, no orders, no paper fills, no edits to config/risk.toml.
Issue #75 (tightening only): markets, the single-file books and every enumerated rotated book file are read
strictly, in full, before the latest quote is chosen or any signal is emitted. A blank line, bad JSON, a
non-object, invalid UTF-8, an unterminated tail line (a torn append) or an unreadable file is refused: the
cycle returns ``ok`` False with reason ``input_invalid`` and ``input_problems`` (path, line, problem code),
zero gaps and zero fills, writes no StaleWatch state and repairs no source file. A missing file, empty books
and a recording dir with no book file keep their old meaning (missing_book). A non-finite constant
(NaN/Infinity) is not part of the refusal: the e2e recording carries such a row on purpose and its golden
output must stay unchanged (the row is inert: book age reads book_ts, the gap path needs 0 < ask < 1).
Issue #75 criterion 2 (tightening only): the inputs that are written while the cycle reads them -- the
direct markets and books files, and the active (last) file of a recording's rotation -- get one bounded
re-read when they change during a read. Still changing after that one re-read and the round is explicitly
skipped: ``ok`` False, reason ``input_pending``, zero gaps and zero fills, no StaleWatch advance and no
reset, no halt and no signal. ``--loop`` keeps the heartbeat and waits for the next round, so a transient
append is never mapped to a permanent halt (EXIT_PIN would record a global halt); a one-shot run reports
the skipped round and exits EXIT_OK. A file that has stopped changing is read exactly as before.
Issue #277 (Task DF, tightening only): consecutive ``input_pending`` rounds are no longer silent. Each one
adds 1 to ``input_pending_streak`` in the StaleWatch state (next to ``all_stale_streak``; the per-market
streaks are still neither advanced nor reset). At ALL_STALE_CONSECUTIVE_CYCLES rounds in a row it takes the
existing gap-14 escalation: one GLOBAL Sentinel halt with the ``all_markets_stale`` reason prefix (the reason
names input_pending and the count), not while one is active or logs/HALT is set, again after an Operator
resume while the input is still pending. The round itself stays a skip (``ok`` False, reason
``input_pending``, EXIT_OK one-shot, ``--loop`` continues). A normal completed cycle clears the streak; an
``input_invalid`` round leaves it untouched. A StaleWatch state that cannot be read is not overwritten by a
pending round (the next full cycle still counts it per market) and, like a failed state write, counts as one
more pending round (fail closed: a streak that cannot be kept must not hide the alarm). The heartbeat
progress carries ``input_pending_streak``. No new threshold: the same constant as all_markets_stale.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import arb_binary, halt_store, neg_risk, sentinel, structural_gate  # noqa: E402
from src.book_staleness import (  # noqa: E402
    INVALID_BOOK_TS,
    INVALID_NOW,
    MISSING_BOOK_TS,
    book_age_problem,
    latest_by_time,
    parse_book_ts,
)
from src.podlib import (  # noqa: E402
    BOOKS_PATH,
    MARKETS_PATH,
    PAPER_PATH,
    QUOTES_PATH,
    RISK_PATH,
    SENTINEL_STATE_PATH,
    bus,
    load_risk,
    pin_problems,
    read_jsonl,  # re-exported: scripts/e2e_offline.py reads the fixtures with it
    utc_now_ms,
)

# Same value as book_staleness.MAX_BOOK_AGE_S (RiskGate). Not a risk.toml key.
from src.thresholds import PAPER_CYCLE_STALE_AFTER_S as STALE_AFTER_S  # noqa: E402 - Task AC
from src.thresholds import ALL_STALE_CONSECUTIVE_CYCLES, HEARTBEAT_PAPER_CYCLE_EVERY_S  # noqa: E402 - Task AP

# Task AL gap 10: a leg (or a market) with no recorded book row counts as a stale check.
MISSING_BOOK = "missing_book"
# Task AP gap 14: the global halt reason prefix when every checked market is stale.
ALL_STALE_ALARM = "all_markets_stale"
# Task AP gap 11: the M1 run's rotated book files inside a run directory.
RECORDING_BOOKS_GLOB = "books/books-*.jsonl"
RECORDING_EMPTY = "recording_has_no_book_file"
# Issue #125 (Task BN): the recording's market scope.
RECORDING_MANIFEST = "manifest.json"
RECORDING_MANIFEST_SCHEMA = "m1_run/1"  # src.m1_run's manifest schema (== m1_report.RUN_SCHEMA)
# Issue #75: strict input reading for this module's own inputs (markets, books, rotated books).
# A bad line is never skipped: the cycle stops before choosing quotes or emitting any signal.
INPUT_INVALID = "input_invalid"
INPUT_UNREADABLE = "unreadable"
INPUT_NOT_UTF8 = "not_utf8"
INPUT_TORN_TAIL = "torn_tail"
INPUT_BLANK_LINE = "blank_line"
INPUT_BAD_JSON = "bad_json"
INPUT_NOT_OBJECT = "not_object"
# Issue #75 criterion 2: a monitored input that is still changing after the one bounded re-read.
INPUT_PENDING = "input_pending"
INPUT_CHANGING = "changing"
INPUT_REREAD_LIMIT = 1  # one bounded re-read of a changing input (structure, not a threshold)


class CorruptInput(Exception):
    """A markets/books JSONL input cannot be read as complete (Issue #75).

    Carries only where and what: the path, the 1-based line (None for a file-level problem) and one
    problem code. The source file is never repaired, truncated or rewritten.
    """

    def __init__(self, path: Path, problem: str, line: int | None = None, detail: str = "") -> None:
        self.path = str(path)
        self.problem = problem
        self.line = line
        self.detail = detail
        where = f"{self.path}:{line}" if line is not None else self.path
        super().__init__(f"{where}: {problem}" + (f" ({detail})" if detail else ""))

    def as_problem(self) -> dict:
        return {"path": self.path, "line": self.line, "problem": self.problem}


def read_strict_jsonl(path: Path) -> list[dict]:
    """Every line of ``path`` one JSON object, the file ending in a newline. An absent file has no rows.

    Issue #75: a blank line, bad JSON, a non-object, invalid UTF-8, an unterminated last line (a torn
    append) or an unreadable file raises CorruptInput, so the caller never keeps computing with the
    rows that happened to be readable. A non-finite constant (NaN/Infinity) is *not* refused here:
    the e2e fixture records such a row on purpose and criterion 7 keeps that golden output unchanged
    (the row is inert downstream: book age uses book_ts, and the gap path needs 0 < ask < 1).
    """
    path = Path(path)
    if not path.exists():
        return []
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise CorruptInput(path, INPUT_UNREADABLE, None, type(exc).__name__) from exc
    if data and not data.endswith(b"\n"):
        raise CorruptInput(path, INPUT_TORN_TAIL)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CorruptInput(path, INPUT_NOT_UTF8, None, str(exc)) from exc
    rows: list[dict] = []
    for number, line in enumerate(text.split("\n")[:-1] if text else [], 1):
        if not line.strip():
            raise CorruptInput(path, INPUT_BLANK_LINE, number)
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise CorruptInput(path, INPUT_BAD_JSON, number, str(exc)) from exc
        if not isinstance(row, dict):
            raise CorruptInput(path, INPUT_NOT_OBJECT, number)
        rows.append(row)
    return rows


class InputPending(Exception):
    """A monitored markets/books input is still being appended (Issue #75 criterion 2).

    Raised only after the one bounded re-read; the round is skipped (no signal, no gap, no StaleWatch
    bookkeeping), never halted. Carries where: the path and one problem code, never file content.
    """

    def __init__(self, path: Path, detail: str = "") -> None:
        self.path = str(path)
        self.detail = detail
        super().__init__(f"{self.path}: {INPUT_CHANGING}" + (f" ({detail})" if detail else ""))

    def as_problem(self) -> dict:
        return {"path": self.path, "line": None, "problem": INPUT_CHANGING}


def _file_signature(path: Path) -> tuple[int, int] | None:
    """(size, mtime_ns) of ``path``, or None when it is absent. How a mid-read append is seen."""
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


def read_stable_strict_jsonl(path: Path) -> list[dict]:
    """``read_strict_jsonl`` for an input that may be appended while it is read (Issue #75 criterion 2).

    The strict read happens with the file's signature taken before and after it. A signature that
    changed means the rows may be a partial view: exactly ``INPUT_REREAD_LIMIT`` bounded re-read is
    allowed. Still changing after that and InputPending names the path; the caller then skips the
    round. A file that has stopped changing is read exactly once, so a corrupt one is refused as
    ``CorruptInput`` without a re-read.
    """
    path = Path(path)
    problem: CorruptInput | None = None
    rows: list[dict] = []
    for _attempt in range(1 + INPUT_REREAD_LIMIT):
        before = _file_signature(path)
        problem = None
        try:
            rows = read_strict_jsonl(path)
        except CorruptInput as exc:
            problem = exc
        if _file_signature(path) == before:
            if problem is not None:
                raise problem
            return rows
    raise InputPending(path)


NOT_RECORDED = "not_recorded"
MANIFEST_MISSING = "recording_manifest_missing"
MANIFEST_INVALID = "recording_manifest_invalid"
MANIFEST_NO_MARKETS = "recording_manifest_no_markets"
LEDGER_UNREADABLE = "paper_ledger_unreadable"


def recording_markets(recording_dir: Path) -> tuple[frozenset[str], str | None]:
    """Issue #125: the market ids the M1 run records (``assets[].market_id`` of its manifest).

    Returns (markets, problem). Any problem (no manifest, unreadable or not JSON, not an m1_run/1 object,
    ``assets`` not a list, no non-empty market id) returns an empty set and the reason: fail closed, so no
    market is treated as recorded.
    """
    path = Path(recording_dir) / RECORDING_MANIFEST
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return frozenset(), MANIFEST_MISSING
    except (OSError, UnicodeDecodeError):
        return frozenset(), MANIFEST_INVALID
    try:
        manifest = json.loads(raw)
    except ValueError:
        return frozenset(), MANIFEST_INVALID
    if not isinstance(manifest, dict) or manifest.get("schema") != RECORDING_MANIFEST_SCHEMA:
        return frozenset(), MANIFEST_INVALID
    assets = manifest.get("assets")
    if not isinstance(assets, list):
        return frozenset(), MANIFEST_INVALID
    markets = frozenset(str(item["market_id"]) for item in assets
                        if isinstance(item, dict) and isinstance(item.get("market_id"), str)
                        and item["market_id"].strip())
    if not markets:
        return frozenset(), MANIFEST_NO_MARKETS
    return markets, None


def held_markets(paper_path: Path) -> frozenset[str] | None:
    """Issue #125 (d): markets with an open paper position (any leg's net shares above LEDGER_OPEN_EPS).

    None when the ledger cannot be read in full (integrity problems or non-finite rows, the rule RiskGate's
    load_state applies): the caller then treats every market as held (fail closed). No ledger file is no
    position."""
    from src import ledger_store, paper_ledger  # local: keeps the cycle's import light

    try:
        _, ledger = ledger_store.read_ledger(paper_path)
        if paper_ledger.non_finite_ledger_rows(ledger.rows) or ledger_store.verify(ledger):
            return None
        legs = paper_ledger.leg_positions(ledger.rows)
    except Exception:  # noqa: BLE001 - an unreadable ledger is fail closed, never "no positions"
        return None
    return frozenset(str(market) for (market, _), leg in legs.items()
                     if market and abs(leg["shares"]) > paper_ledger.OPEN_EPS)


def recording_book_files(recording_dir: Path) -> list[Path]:
    """The run's book files in rotation order (books-000001.jsonl, books-000002.jsonl, ...)."""
    return sorted(Path(recording_dir).glob(RECORDING_BOOKS_GLOB))


def read_books(books_path: Path, recording_dir: Path | None = None) -> tuple[list[dict], str | None]:
    """Book rows from ``books_path``, or from every rotated file of ``recording_dir`` when given.

    Returns (rows, problem). A recording dir with no book file returns no rows and
    RECORDING_EMPTY: every leg is then missing_book (stale), never skipped. Issue #75: every file is
    read strictly, so one corrupt row raises CorruptInput instead of dropping the row and keeping
    the rest; a missing or empty books file still means no rows (the missing_book rule is unchanged).
    The direct books file and the active (last) file of the rotation are the inputs that may still be
    appended to (Issue #75 criterion 2): they get the bounded re-read, the closed ones do not.
    """
    if recording_dir is None:
        return read_stable_strict_jsonl(books_path), None
    files = recording_book_files(recording_dir)
    if not files:
        return [], RECORDING_EMPTY
    rows: list[dict] = []
    for path in files[:-1]:
        rows.extend(read_strict_jsonl(path))
    rows.extend(read_stable_strict_jsonl(files[-1]))
    return rows, None


def _input_refusal(reason: str, problems: list[dict], *, state_path: Path | None,
                   skipped: int = 0, skips: list[dict] | None = None) -> dict:
    """Issue #75: the complete result structure of a cycle refused on its input.

    No signal, no gap, no fill, no StaleWatch bookkeeping: the caller sees a full result dict with
    ``ok`` False and the offending path, line and problem code in ``input_problems``.
    """
    path = state_path if state_path is not None else SENTINEL_STATE_PATH
    return {
        "ok": False,
        "reason": reason,
        "signals": 0,
        "gaps": 0,
        "structural_rejected": 0,
        "resolving_markets": 0,
        "stale": 0,
        "fills": 0,
        "skipped": skipped,
        "skips": skips if skips is not None else [],
        "halted_markets": [],
        "sentinel": {
            "checked": 0,
            "state_problem": None,
            "write_problem": None,
            "halts": [],
            "state_path": str(path),
            "all_stale_streak": 0,
            "all_stale_halt": False,
        },
        "books_problem": None,
        "not_recorded": 0,
        "recording": None,
        "input_problems": problems,
    }


def _input_invalid(exc: CorruptInput, *, state_path: Path | None) -> dict:
    """Issue #75: the round of a cycle refused for corrupt input (path, line and problem code only)."""
    return _input_refusal(INPUT_INVALID, [exc.as_problem()], state_path=state_path)


def _input_pending(exc: InputPending, *, state_path: Path | None, pending: dict | None = None) -> dict:
    """Issue #75 criterion 2: the round of a cycle skipped because its input is still changing.

    ``reason`` is ``input_pending`` and the round is one explicit skip: zero gaps and zero fills, the
    per-market StaleWatch streaks untouched (neither advanced nor cleared) -- a transient append must
    never become a permanent halt. Issue #277: ``pending`` (from ``_record_input_pending``) adds the
    persisted ``input_pending_streak`` and whether this round fired the all_markets_stale escalation.
    """
    result = _input_refusal(
        INPUT_PENDING, [exc.as_problem()], state_path=state_path,
        skipped=1, skips=[{"kind": INPUT_PENDING, "reason": INPUT_PENDING, "path": exc.path}])
    extra = pending or {}
    result["sentinel"].update({
        "state_problem": extra.get("state_problem"),
        "write_problem": extra.get("write_problem"),
        "input_pending_streak": extra.get("input_pending_streak", 0),
        "input_pending_halt": bool(extra.get("input_pending_halt", False)),
    })
    return result


def _record_input_pending(state_path: Path, *, bus_path: Path | None, quotes_path: Path | None) -> dict:
    """Issue #277: one more consecutive ``input_pending`` round in the StaleWatch state; escalate at the limit.

    Only ``input_pending_streak`` changes (the per-market streaks and ``all_stale_streak`` are kept as
    loaded). A state that cannot be read is not overwritten -- the next full cycle counts it as a stale
    check per market -- and, like a failed write, counts as one more pending round (fail closed). At
    ALL_STALE_CONSECUTIVE_CYCLES the existing all_markets_stale path: one global halt, not while one is
    active (or its state is unreadable) or logs/HALT is set.
    """
    quotes = quotes_path if quotes_path is not None else QUOTES_PATH
    watch, state_problem = sentinel.load_watch(state_path, bus_path=bus_path, quotes_path=quotes)
    watch.input_pending_streak += 1
    write_problem = None
    if state_problem is None:
        write_problem = sentinel.save_watch(watch, state_path)
    if state_problem is not None or write_problem is not None:
        watch.input_pending_streak += 1  # the streak could not be read or kept: one more round
    streak = watch.input_pending_streak
    message = None
    if streak >= ALL_STALE_CONSECUTIVE_CYCLES:
        active = halt_store.has_active_halt(scope=halt_store.SCOPE_GLOBAL, reason_prefix=ALL_STALE_ALARM)
        if active is False and not halt_store.fallback_active():
            message = sentinel.halt(
                reason=f"{ALL_STALE_ALARM}: {INPUT_PENDING} for {streak} consecutive cycles "
                       f"(inputs still changing after the bounded re-read; no market could be checked)",
                scope=halt_store.SCOPE_GLOBAL, bus_path=bus_path, quotes_path=quotes)
    return {"state_problem": state_problem, "write_problem": write_problem,
            "input_pending_streak": streak, "input_pending_halt": message is not None}


def run(
    *,
    markets_path: Path = MARKETS_PATH,
    books_path: Path = BOOKS_PATH,
    bus_path: Path | None = None,
    now: str | None = None,
    stale_after_s: int = STALE_AFTER_S,
    cfg: dict | None = None,
    sentinel_state_path: Path | None = None,
    quotes_path: Path | None = None,
    recording_dir: Path | None = None,
    paper_path: Path | None = None,
) -> dict:
    before = RISK_PATH.read_bytes()
    loaded = cfg if cfg is not None else load_risk()
    if pin_problems(loaded):
        _assert_risk_unchanged(before)
        return {"ok": False, "reason": "phase0_pin", "signals": 0, "gaps": 0, "stale": 0, "fills": 0}
    now_s = now or utc_now_ms()  # Issue #128: milliseconds, like the book_ts it is compared with
    now_at = parse_book_ts(now_s)
    skips: list[dict] = []
    # Issue #75: markets, the single-file books and every enumerated rotated book file are read in
    # full before any quote is chosen or any signal is emitted. A corrupt or unreadable input fails
    # the cycle with no gap, no stale count and no StaleWatch write (the streaks are untouched).
    # Criterion 2: a direct file (or the active rotated one) that is still changing after the one
    # bounded re-read skips the round the same way, with reason ``input_pending`` instead.
    try:
        market_rows = read_stable_strict_jsonl(markets_path)
        book_rows, books_problem = read_books(books_path, recording_dir)
    except CorruptInput as exc:
        _assert_risk_unchanged(before)
        return _input_invalid(exc, state_path=sentinel_state_path)
    except InputPending as exc:
        # Issue #277: the skip is counted; ALL_STALE_CONSECUTIVE_CYCLES in a row escalate (all_markets_stale).
        pending = _record_input_pending(
            sentinel_state_path if sentinel_state_path is not None else SENTINEL_STATE_PATH,
            bus_path=bus_path, quotes_path=quotes_path)
        _assert_risk_unchanged(before)
        return _input_pending(exc, state_path=sentinel_state_path, pending=pending)
    markets = _latest_by(market_rows, "id", skips, bus_path)
    books = _latest_books(book_rows, skips, bus_path)
    state_path = sentinel_state_path if sentinel_state_path is not None else SENTINEL_STATE_PATH
    watch, state_problem = sentinel.load_watch(
        state_path,
        bus_path=bus_path,
        quotes_path=quotes_path if quotes_path is not None else QUOTES_PATH,
    )
    # Issue #125: with a recording dir, only the manifest's markets may signal; others are not_recorded.
    recorded: frozenset[str] = frozenset()
    manifest_problem: str | None = None
    held: frozenset[str] | None = frozenset()
    if recording_dir is not None:
        recorded, manifest_problem = recording_markets(recording_dir)
        held = held_markets(paper_path if paper_path is not None else PAPER_PATH)
        if manifest_problem is not None:
            bus("BookWatcher", "Sentinel", "signal", "",
                {"recording_manifest_problem": manifest_problem, "recording_dir": str(recording_dir),
                 "gap_signal": "skipped", "skip_reason": NOT_RECORDED, "exchange_action": "none"},
                path=bus_path)
    not_recorded = 0
    checked: list[str] = []
    stale_checked: list[str] = []
    halts: list[str] = []
    halted_markets: list[str] = []
    gaps = 0
    stale = 0
    structural_rejected = 0
    resolving = 0
    for market_id, market in markets.items():
        tokens = [str(token) for token in market.get("token_ids") or []]
        asks: list[float] = []
        depths: list[float | None] = []
        stale_reasons: set[str] = set()
        sentinel_reasons: set[str] = set()
        for token in tokens:
            book = books.get((market_id, token))
            if book is None:
                stale_reasons.add(MISSING_BOOK)
                sentinel_reasons.add(MISSING_BOOK)
                continue
            problem = _row_book_problem(book, now_at, stale_after_s)
            if problem is not None:
                stale_reasons.add(problem)
            # Sentinel judges at its own 120 s too, so a looser stale_after_s never resets a streak.
            sentinel_problem = problem or _row_book_problem(book, now_at, sentinel.SENTINEL_STALE_S)
            if sentinel_problem is not None:
                sentinel_reasons.add(sentinel_problem)
            ask = book.get("best_ask")
            if isinstance(ask, (int, float)) and 0.0 < float(ask) < 1.0:
                asks.append(float(ask))
                size = book.get("best_ask_size")
                depths.append(float(size) if isinstance(size, (int, float)) else None)
        if not tokens:
            stale_reasons.add(MISSING_BOOK)
            sentinel_reasons.add(MISSING_BOOK)
        in_scope = recording_dir is None or (manifest_problem is None and market_id in recorded)
        watched = (in_scope or manifest_problem is not None or held is None or market_id in held)
        if not watched:
            # Issue #125 (c): outside the recording, no position: a reject, not a StaleWatch check.
            _not_recorded(market_id, stale_reasons, stale_after_s, None, skips, bus_path)
            not_recorded += 1
            if stale_reasons:
                stale += 1
            continue
        # Task AL gap 10: every market is checked; a missing book is a stale check, never a skip.
        checked.append(market_id)
        if state_problem is not None:
            _sentinel_record(watch, market_id, state_problem, halts)
        _sentinel_record(watch, market_id, min(sentinel_reasons) if sentinel_reasons else None, halts)
        if sentinel_reasons or state_problem is not None:
            stale_checked.append(market_id)
        if stale_reasons:
            bus(
                "BookWatcher",
                "Sentinel",
                "signal",
                market_id,
                {
                    "stale": True,
                    "stale_after_s": stale_after_s,
                    "stale_reasons": sorted(stale_reasons),
                    "gap_signal": "skipped",
                    "skip_reason": "stale_book",
                    "exchange_action": "none",
                },
                path=bus_path,
            )
            skips.append({"market": market_id, "kind": "stale_market", "reason": "stale_book",
                          "stale_reasons": sorted(stale_reasons)})
            stale += 1
            # A stale market gets no gap/spread signal at all (Task T).
            continue
        if not in_scope:
            # Issue #125 (b)/(d): watched (held, or the manifest is unusable) but not recorded: no signal.
            _not_recorded(market_id, stale_reasons, stale_after_s, manifest_problem, skips, bus_path)
            not_recorded += 1
            continue
        halted = halt_store.halt_problems(market_id)
        if halted:
            # Task AL gap 1: a halted market (or any market under a global halt) gets no gap signal.
            skips.append({"market": market_id, "kind": "halted_market", "reason": "halt_active",
                          "halt_reasons": halted})
            halted_markets.append(market_id)
            continue
        ended = structural_gate.resolution_problems(market, now_at)
        if ended:
            # Issue #141 (Task BX): expired or in UMA resolution: no gap path, the reasons recorded.
            _resolving(market_id, ended, skips, bus_path)
            resolving += 1
            continue
        message = _gap_message(asks, market_id, depths, bus_path)
        if message is not None:
            if message["type"] != "signal" or message["payload"].get("rejected") is not True:
                raise RuntimeError("paper cycle refused a non-signal or an unrejected multi-leg gap")
            gaps += 1
            # Issue #141 (Task BX): a complete-set gap is a structural reject, recorded, never an order.
            structural_rejected += 1
            skips.append({"market": market_id, "kind": structural_gate.STRUCTURAL_ARB_DISABLED,
                          "reason": structural_gate.STRUCTURAL_ARB_DISABLED, "strategy": message["from"],
                          "legs": len(asks)})
    # Task AP gap 14: every checked market stale in this cycle extends the all-stale streak.
    all_stale = bool(checked) and len(stale_checked) == len(checked)
    watch.all_stale_streak = watch.all_stale_streak + 1 if all_stale else 0
    all_stale_halt = None
    if watch.all_stale_streak >= ALL_STALE_CONSECUTIVE_CYCLES:
        active = halt_store.has_active_halt(scope=halt_store.SCOPE_GLOBAL, reason_prefix=ALL_STALE_ALARM)
        if active is False and not halt_store.fallback_active():
            all_stale_halt = sentinel.halt(
                reason=f"{ALL_STALE_ALARM}: all {len(checked)} checked markets stale for "
                       f"{watch.all_stale_streak} consecutive cycles",
                scope=halt_store.SCOPE_GLOBAL, bus_path=bus_path,
                quotes_path=quotes_path if quotes_path is not None else QUOTES_PATH)
    # Issue #277: a normal completed cycle ends any run of input_pending rounds.
    watch.input_pending_streak = 0
    write_problem = sentinel.save_watch(watch, state_path)
    if write_problem is not None:
        # This run's streaks are lost, so the failed write is one more stale check now (fail closed).
        for market_id in checked:
            _sentinel_record(watch, market_id, write_problem, halts)
    _assert_risk_unchanged(before)
    return {
        "ok": True,
        "reason": "signals_only",
        "signals": gaps + stale,
        "gaps": gaps,
        "structural_rejected": structural_rejected,
        "resolving_markets": resolving,
        "stale": stale,
        "fills": 0,
        "skipped": len(skips),
        "skips": skips,
        "halted_markets": halted_markets,
        "sentinel": {
            "checked": len(checked),
            "state_problem": state_problem,
            "write_problem": write_problem,
            "halts": halts,
            "state_path": str(state_path),
            "all_stale_streak": watch.all_stale_streak,
            "all_stale_halt": all_stale_halt is not None,
            "input_pending_streak": watch.input_pending_streak,
        },
        "books_problem": books_problem,
        "not_recorded": not_recorded,
        "recording": None if recording_dir is None else {
            "manifest_problem": manifest_problem,
            "recorded_markets": len(recorded),
            "held_markets": None if held is None else len(held),
            "ledger_problem": LEDGER_UNREADABLE if held is None else None,
        },
    }


def _not_recorded(market_id: str, stale_reasons: set[str], stale_after_s: object, manifest_problem: str | None,
                  skips: list[dict], bus_path: Path | None) -> None:
    """Issue #125: one reject record for a market the recording does not cover (never a gap signal)."""
    payload = {
        "stale": bool(stale_reasons),
        "stale_after_s": stale_after_s,
        "stale_reasons": sorted(stale_reasons),
        "gap_signal": "skipped",
        "skip_reason": NOT_RECORDED,
        "exchange_action": "none",
    }
    if manifest_problem is not None:
        payload["recording_manifest_problem"] = manifest_problem
    bus("BookWatcher", "Sentinel", "signal", market_id, payload, path=bus_path)
    skips.append({"market": market_id, "kind": "not_recorded_market", "reason": NOT_RECORDED,
                  "stale_reasons": sorted(stale_reasons)})


def _resolving(market_id: str, reasons: list[str], skips: list[dict], bus_path: Path | None) -> None:
    """Issue #141: one reject record for an expired or resolving market (never a gap signal)."""
    bus("BookWatcher", "Sentinel", "signal", market_id,
        {"gap_signal": "skipped", "skip_reason": reasons[0], "reasons": list(reasons), "exchange_action": "none"},
        path=bus_path)
    skips.append({"market": market_id, "kind": "resolving_market", "reason": reasons[0], "reasons": list(reasons)})


def _sentinel_record(watch: sentinel.StaleWatch, market_id: str, problem: str | None, halts: list[str]) -> None:
    result = watch.record(market_id, problem)
    if result["halt"] is not None:
        halts.append(market_id)


def _gap_message(asks: list[float], market_id: str, depths: list[float | None], bus_path: Path | None) -> dict | None:
    depth = min(depths) if depths and all(item is not None for item in depths) else None
    if len(asks) == 2:
        return arb_binary.emit_gap(asks[0], asks[1], market_id, bus_path=bus_path, displayed_depth=depth)
    if len(asks) >= 3:
        return neg_risk.emit_gap(asks, market_id, bus_path=bus_path, displayed_depth=depth)
    return None


def _record_skipped_rows(skipped: list[dict], rows: list[dict], source: str, skips: list[dict],
                         bus_path: Path | None) -> None:
    """Record each row dropped for an unusable ts: one skip entry and one BookWatcher signal."""
    for item in skipped:
        row = rows[item["index"]]
        market = str((row.get("market_id") if source == "books" else row.get("id")) or "")
        token = str(row.get("token_id") or "") if source == "books" else ""
        entry = {"kind": "skipped_row", "source": source, "market": market, "token_id": token, "reason": item["reason"]}
        skips.append(entry)
        bus(
            "BookWatcher",
            "Sentinel",
            "signal",
            market,
            {"skipped_row": True, "source": source, "token_id": token, "reason": item["reason"],
             "exchange_action": "none"},
            path=bus_path,
        )


def _latest_by(rows: list[dict], key: str, skips: list[dict] | None = None,
               bus_path: Path | None = None) -> dict[str, dict]:
    """Latest row per ``key`` by parsed ``ts`` (not string order). Unparseable rows are skipped."""
    chosen, skipped = latest_by_time(rows, lambda row: str(row.get(key) or ""))
    if skips is not None:
        _record_skipped_rows(skipped, rows, "markets", skips, bus_path)
    return chosen


def _latest_books(rows: list[dict], skips: list[dict] | None = None,
                  bus_path: Path | None = None) -> dict[tuple[str, str], dict]:
    """Latest book row per (market, token) by the recorder's parsed ``ts``.

    The newest recording wins even when its book_ts is missing, so a newer snapshot with
    no usable book time makes the leg stale instead of letting an older one look fresh.
    """

    def _key(row: dict) -> tuple[str, str] | None:
        pair = (str(row.get("market_id") or ""), str(row.get("token_id") or ""))
        return pair if pair[0] and pair[1] else None

    chosen, skipped = latest_by_time(rows, _key)
    if skips is not None:
        _record_skipped_rows(skipped, rows, "books", skips, bus_path)
    return chosen


def _row_book_problem(book: dict, now_at: datetime | None, stale_after_s: object) -> str | None:
    """Book age from the exchange's book_ts on the row, never the recorder ts (Task T).

    A recorded book_ts_problem always counts as stale: missing_book_ts / invalid_book_ts as
    recorded, any other value as invalid_book_ts. No book_ts key is missing_book_ts.
    """
    if now_at is None:
        return INVALID_NOW
    if "book_ts_problem" in book and book["book_ts_problem"] is not None:
        problem = book["book_ts_problem"]
        return problem if problem in (MISSING_BOOK_TS, INVALID_BOOK_TS) else INVALID_BOOK_TS
    return _book_problem(book.get("book_ts"), now_at, stale_after_s)


def _book_problem(ts: object, now_at: datetime | None, stale_after_s: object) -> str | None:
    """None when the book is fresh, else the book_staleness reason (RiskGate's parser and boundary).

    Fresh means 0 <= age <= stale_after_s, so exactly 120 s is fresh and a future stamp is not.
    Missing or unparseable values, an unparseable ``now`` and a broken limit all count as stale.
    ``now`` never falls back to the wall clock here: book_age_problem reads the clock when given None.
    """
    if now_at is None:
        return INVALID_NOW
    return book_age_problem(ts, now_at, stale_after_s)


def _assert_risk_unchanged(before: bytes) -> None:
    if RISK_PATH.read_bytes() != before:
        raise RuntimeError("risk.toml changed during paper cycle")


EXIT_OK = 0
EXIT_PIN = 1  # a phase-0 pin failed (in-memory config or, Task AS, the risk file: a global halt is recorded)
EXIT_USAGE = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.paper_cycle",
        description="Offline paper cycle: gap and staleness signals, Sentinel StaleWatch. No orders.",
        epilog="Exit codes: 0 ran (a round skipped on a still-changing input: ok=false reason=input_pending; "
               "ALL_STALE_CONSECUTIVE_CYCLES such rounds in a row are an all_markets_stale global halt); "
               "1 a phase-0 pin failed (the risk file is checked before every cycle; "
               "global halt recorded); 2 usage error. See docs/EXIT_CODES.md.",
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--books", type=Path, default=None, help="book rows JSONL (default data/books.jsonl)")
    source.add_argument("--recording-dir", type=Path, default=None,
                        help="an M1 run directory: read its rotated books/books-*.jsonl (Task AP gap 11)")
    parser.add_argument("--loop", action="store_true", help="run a cycle every --every seconds (Task AP gap 12)")
    parser.add_argument("--every", type=float, default=HEARTBEAT_PAPER_CYCLE_EVERY_S,
                        help=f"seconds between cycles with --loop (0 < s <= {HEARTBEAT_PAPER_CYCLE_EVERY_S:g})")
    parser.add_argument("--max-cycles", type=int, default=0, help="stop after N cycles with --loop (0 = no limit)")
    return parser


def _print(result: dict) -> None:
    print(
        f"ok={str(result['ok']).lower()} reason={result['reason']} "
        f"gaps={result['gaps']} stale={result['stale']} fills={result['fills']} orders=none"
    )


def main(argv: list[str] | None = None, *, sleep=time.sleep, monotonic=time.monotonic) -> int:
    """One cycle (start and finish heartbeat), or ``--loop``: a cycle and a heartbeat every ``--every``.

    ``--every`` may only be the default or shorter (a longer one would outrun the Sentinel's
    HEARTBEAT_PAPER_CYCLE_TIMEOUT_S). An exception ends the loop with a ``crashed`` heartbeat.
    """
    from src.heartbeat import Heartbeat  # local: keeps the module import light

    from src.pins import refuse_start  # Task AS (gap 5): the risk file pins before every cycle

    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    if not (0.0 < args.every <= HEARTBEAT_PAPER_CYCLE_EVERY_S) or args.max_cycles < 0:
        print(f"paper_cycle: usage error: 0 < --every <= {HEARTBEAT_PAPER_CYCLE_EVERY_S:g} and --max-cycles >= 0",
              file=sys.stderr)
        return EXIT_USAGE
    kwargs: dict = {"recording_dir": args.recording_dir}
    if args.books is not None:
        kwargs["books_path"] = args.books
    hb = Heartbeat("paper_cycle", every_s=args.every, monotonic=monotonic)
    hb.beat("running", {"cycles": 0}, force=True)
    cycles = 0
    try:
        while True:
            started = monotonic()
            if refuse_start("paper_cycle"):
                hb.finish(EXIT_PIN, {"cycles": cycles, "pin_drift": True})
                return EXIT_PIN
            result = run(**kwargs)
            cycles += 1
            _print(result)
            progress = {"cycles": cycles, "gaps": result["gaps"], "stale": result["stale"],
                        "halts": len((result.get("sentinel") or {}).get("halts") or []),
                        "books_problem": result.get("books_problem"),
                        # Issue #277: consecutive input_pending rounds (0 after a normal cycle).
                        "input_pending_streak": (result.get("sentinel") or {}).get("input_pending_streak")}
            if result["reason"] == INPUT_PENDING and not args.loop:
                # Issue #75 criterion 2: a round skipped on a changing input is not a failure and
                # never a halt. A one-shot run reports the skip (ok=false reason=input_pending) and
                # exits EXIT_OK; --loop falls through, keeps the heartbeat and waits for the next round.
                hb.finish(EXIT_OK, progress)
                return EXIT_OK
            if not result["ok"] and result["reason"] != INPUT_PENDING:
                hb.finish(EXIT_PIN, progress)
                return EXIT_PIN
            if not args.loop or (args.max_cycles and cycles >= args.max_cycles):
                hb.finish(EXIT_OK, progress)
                return EXIT_OK
            hb.beat("running", progress, force=True)
            sleep(max(0.0, args.every - (monotonic() - started)))
    except BaseException:
        hb.finish(None, {"cycles": cycles}, crashed=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
