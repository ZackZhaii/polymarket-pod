"""Sentinel stub. Emits halt only and cancels local paper quotes. No CLOB call.

Task AL: every halt is persisted first (src/halt_store.py) and read by RiskGate, the Maker and
paper_cycle until the Operator resumes it; see halt() for the write order and fallback.

Task Z (COORDINATION backlog #9): StaleWatch counts consecutive stale or failed book checks per
market and escalates with a paper ``halt`` once SENTINEL_STALE_CONSECUTIVE (2) are in a row.
A fresh check resets the streak. Missing, unparseable, non-finite or future book timestamps,
a broken ``now`` and a failed check all count as stale (fail closed).

This only adds an alarm. Every per-check block is unchanged and independent of it:
RiskGate still vetoes a single stale book (book_staleness, 120 s), paper_cycle still makes
no gap signal from a stale book and still sends the per-check BookWatcher stale signal.
Before Task Z nothing raised a Sentinel alarm automatically, so nothing is loosened.
SENTINEL_STALE_S and SENTINEL_STALE_CONSECUTIVE are code constants, not risk.toml keys;
they are threshold proposals for the user at M2. A caller may only make them stricter.
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from datetime import datetime
from pathlib import Path

from src.book_staleness import book_age_problem
from src.podlib import QUOTES_PATH, append_lock, bus, read_jsonl, utc_now
from src.thresholds import MAX_BOOK_AGE_S, SENTINEL_STALE_CONSECUTIVE, SENTINEL_STALE_S  # Task AC: values in src/thresholds.py

# Sentinel's proposal (COORDINATION #9): a book older than 120 s is stale, and the alarm fires only
# after 2 consecutive stale or failed checks (30 s gave false alarms). Code constants, not risk.toml
# keys; the user decides at M2. The per-check limit equals RiskGate's MAX_BOOK_AGE_S.
# SENTINEL_STALE_S and SENTINEL_STALE_CONSECUTIVE are imported above from src/thresholds.py (Task AC).

CHECK_FAILED = "check_failed"
STALE_ALARM = "stale_book_consecutive"

# Task AA: StaleWatch state between paper_cycle runs. Any problem with it counts as a stale check.
STATE_UNREADABLE = "state_unreadable"
STATE_CORRUPT = "state_corrupt"
STATE_MALFORMED = "state_malformed"
STATE_MISSING = "state_missing"
STATE_WRITE_FAILED = "state_write_failed"
STATE_PROBLEMS = (STATE_UNREADABLE, STATE_CORRUPT, STATE_MALFORMED, STATE_MISSING, STATE_WRITE_FAILED)


def halt(
    *,
    reason: str,
    market: str = "",
    bus_path: Path | None = None,
    quotes_path: Path | None = None,
    scope: str | None = None,
    source: str = "Sentinel",
    to: str = "Executor",
    extra: dict | None = None,
    emit=None,
) -> dict:
    """Halt ``market`` (or everything when ``market`` is empty or ``scope="global"``). Never raises for I/O.

    Task AL (Sentinel design gap 8), in this order:
    1. the halt record goes to logs/halt_state.json first (halt_store.record_halt), so RiskGate, the
       Maker and paper_cycle block the scope even if a later step fails;
    2. open paper quotes in scope are cancelled with an atomic rewrite (temp file + os.replace);
    3. the ``halt`` bus line is written.
    If step 1 could not use the state file, or step 2 or 3 fails, logs/HALT is written (a global
    halt, fail closed) and the error is reported in the returned message instead of being raised
    into paper_cycle. The halt is released only by an Operator resume (src/halt_store.py).
    ``emit`` replaces podlib.bus for the bus line (the caller's own bus writer, same arguments).
    """
    from src import halt_store  # local: halt_store imports podlib only; keeps this module light

    cleaned = " ".join(reason.split())
    if not cleaned:
        raise ValueError("halt reason required")
    market = str(market or "")
    if scope is None:
        scope = halt_store.SCOPE_MARKET if market else halt_store.SCOPE_GLOBAL
    ts = utc_now()
    record = halt_store.record_halt(scope=scope, market=market if scope == halt_store.SCOPE_MARKET else "",
                                    reason=cleaned, source=source, ts=ts)
    failures: list[str] = []
    if record.get("persisted") != halt_store.PERSISTED_STATE:
        failures.append(f"halt_record_{record.get('persisted')}")
    try:
        cancelled = cancel_open_quotes(quotes_path or QUOTES_PATH, ts,
                                       market if scope == halt_store.SCOPE_MARKET else "")
    except Exception as exc:  # noqa: BLE001 - reported and escalated below, never raised into the caller
        cancelled = 0
        failures.append(f"cancel_failed:{type(exc).__name__}")
    payload = {
        "reason": cleaned,
        "scope": scope,
        "halt_id": record.get("id"),
        "paper_quotes_cancelled": cancelled,
        "exchange_action": "none",
    }
    if extra:
        payload.update({k: v for k, v in extra.items() if k not in payload})
    try:
        writer = emit if emit is not None else bus
        message = writer(source, to, "halt", market, payload, path=bus_path)
        if not isinstance(message, dict):
            message = {"ts": ts, "from": source, "to": to, "type": "halt", "market": market, "payload": payload}
    except Exception as exc:  # noqa: BLE001
        failures.append(f"bus_write_failed:{type(exc).__name__}")
        message = {"ts": ts, "from": source, "to": to, "type": "halt", "market": market, "payload": payload}
    if failures:
        halt_store.write_fallback(
            f"{ts} halt id={record.get('id')} scope={scope} market={market or '-'} source={source} "
            f"reason={cleaned} failures={','.join(failures)}"
        )
        message = dict(message, failures=failures)
    return message


def cancel_open_quotes(path: Path, ts: str, market: str = "") -> int:
    """Cancel open paper quotes (all, or one market's). Task AL: atomic rewrite, never a torn file.

    The Maker appends quotes; it checks the halt before and after its append (src/maker.py), so a
    quote racing this rewrite is cancelled by the Maker itself. Raises OSError if the rewrite fails.
    Issue #162: the read and the rewrite hold the quotes file's append lock (the one the Maker's
    append_jsonl takes), so no appended quote is lost by the rewrite; a lock not taken raises
    AppendLockError (an OSError). The strict read of bad lines stays with #134 / #99.
    """
    with append_lock(path):
        return _cancel_locked(path, ts, market)


def _cancel_locked(path: Path, ts: str, market: str) -> int:
    rows = read_jsonl(path)
    cancelled = 0
    for row in rows:
        if row.get("status") != "open":
            continue
        if market and str(row.get("market") or "") != market:
            continue
        row["status"] = "cancelled"
        row["cancelled_ts"] = ts
        row["cancelled_by"] = "Sentinel"
        cancelled += 1
    if cancelled:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
        tmp_name = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.name + ".",
                                             suffix=".tmp", delete=False) as handle:
                tmp_name = handle.name
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
            tmp_name = None
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
    return cancelled


def _strict_limit(value: object, default: float, name: str) -> float:
    """A finite positive number no looser than ``default`` (bools and nan/inf rejected)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number) or number <= 0 or number > default:
        raise ValueError(f"{name} must be finite, > 0 and <= {default}")
    return number


class StaleWatch:
    """Consecutive stale/failed book checks per market; a paper halt at the threshold.

    ``check`` returns the per-check problem (None when fresh) with the streak. It never hides a
    per-check problem: the caller's own block (RiskGate veto, no gap signal) still applies to
    every single stale check. The halt fires once per streak, when the count reaches
    ``consecutive``; later stale checks in the same streak do not halt again. A fresh check
    resets the streak and re-arms the alarm.
    """

    def __init__(
        self,
        *,
        max_age_s: object = SENTINEL_STALE_S,
        consecutive: object = SENTINEL_STALE_CONSECUTIVE,
        bus_path: Path | None = None,
        quotes_path: Path | None = None,
    ) -> None:
        self.max_age_s = _strict_limit(max_age_s, SENTINEL_STALE_S, "max_age_s")
        if isinstance(consecutive, bool) or not isinstance(consecutive, int):
            raise ValueError("consecutive must be an int")
        if not 1 <= consecutive <= SENTINEL_STALE_CONSECUTIVE:
            raise ValueError(f"consecutive must be 1..{SENTINEL_STALE_CONSECUTIVE}")
        self.consecutive = consecutive
        self.bus_path = bus_path
        self.quotes_path = quotes_path
        self.streaks: dict[str, int] = {}
        self.alarmed: set[str] = set()
        # Task AP gap 14: consecutive paper cycles in which every checked market was stale.
        self.all_stale_streak = 0
        # Issue #277 (Task DF): consecutive paper cycles skipped as input_pending (#75 criterion 2).
        self.input_pending_streak = 0

    def check(
        self,
        market: str,
        book_ts: object,
        now: datetime | None,
        *,
        failed: bool = False,
    ) -> dict:
        """Record one book check for ``market``.

        ``failed=True`` (the check itself could not run, e.g. no book) counts as stale.
        ``now`` must be an aware datetime; anything else counts as stale (invalid_now).
        """
        if failed:
            problem: str | None = CHECK_FAILED
        elif not isinstance(now, datetime):
            problem = "invalid_now"
        else:
            problem = book_age_problem(book_ts, now, self.max_age_s)
        return self.record(market, problem)

    def record(self, market: str, problem: str | None) -> dict:
        """Record one check whose per-check problem the caller already worked out.

        ``problem`` None is a fresh check (resets the streak); any non-empty string is stale.
        Anything else (an empty string, a non-string) is treated as a failed check, fail closed.
        """
        key = str(market or "")
        if problem is not None and (not isinstance(problem, str) or not problem):
            problem = CHECK_FAILED
        if problem is None:
            self.streaks.pop(key, None)
            self.alarmed.discard(key)
            return {"market": key, "stale": False, "problem": None, "streak": 0, "halt": None}
        streak = self.streaks.get(key, 0) + 1
        self.streaks[key] = streak
        message = None
        if streak >= self.consecutive and (key not in self.alarmed or self._resumed_while_stale(key)):
            self.alarmed.add(key)
            message = halt(
                reason=f"{STALE_ALARM}: {streak} consecutive stale or failed book checks (last: {problem})",
                market=key,
                bus_path=self.bus_path,
                quotes_path=self.quotes_path,
            )
        return {"market": key, "stale": True, "problem": problem, "streak": streak, "halt": message}

    @staticmethod
    def _resumed_while_stale(key: str) -> bool:
        """Task AP (AL follow-up): the streak already alarmed, but the Operator has since resumed that
        market's stale-book halt while the book is still stale. Re-alarm on this check, without
        waiting for a fresh check first. An unreadable halt state is already a global halt, so no
        duplicate is written then (the next readable check re-alarms if the market halt is gone).
        A market with no market id cannot hold a market halt; it is never re-halted here.
        """
        from src import halt_store

        if not key or halt_store.fallback_active():
            return False  # logs/HALT already halts everything; re-alarm once it is resumed
        active = halt_store.has_active_halt(scope=halt_store.SCOPE_MARKET, market=key, reason_prefix=STALE_ALARM)
        return active is False

    def to_dict(self) -> dict:
        return {
            "max_age_s": self.max_age_s,
            "consecutive": self.consecutive,
            "streaks": dict(sorted(self.streaks.items())),
            "alarmed": sorted(self.alarmed),
            "all_stale_streak": self.all_stale_streak,
            "input_pending_streak": self.input_pending_streak,
        }

    @classmethod
    def from_dict(
        cls,
        data: object,
        *,
        bus_path: Path | None = None,
        quotes_path: Path | None = None,
    ) -> StaleWatch:
        """Restore a saved watch. Any malformed field raises; a bad file never resets a streak quietly."""
        if not isinstance(data, dict):
            raise ValueError("state must be a dict")
        watch = cls(
            max_age_s=data.get("max_age_s"),
            consecutive=data.get("consecutive"),
            bus_path=bus_path,
            quotes_path=quotes_path,
        )
        streaks = data.get("streaks")
        alarmed = data.get("alarmed")
        if not isinstance(streaks, dict) or not isinstance(alarmed, list):
            raise ValueError("streaks must be a dict and alarmed a list")
        for key, count in streaks.items():
            if not isinstance(key, str) or isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise ValueError("bad streak entry")
            watch.streaks[key] = count
        for key in alarmed:
            if not isinstance(key, str) or key not in watch.streaks:
                raise ValueError("bad alarmed entry")
            watch.alarmed.add(key)
        # Task AP: absent in a state file written before Task AP (counts from 0); present means strict.
        all_stale = data.get("all_stale_streak", 0)
        if isinstance(all_stale, bool) or not isinstance(all_stale, int) or all_stale < 0:
            raise ValueError("bad all_stale_streak")
        watch.all_stale_streak = all_stale
        # Issue #277 (Task DF): same rule -- absent in an older state file (counts from 0); present means strict.
        pending = data.get("input_pending_streak", 0)
        if isinstance(pending, bool) or not isinstance(pending, int) or pending < 0:
            raise ValueError("bad input_pending_streak")
        watch.input_pending_streak = pending
        return watch


def state_marker(path: Path) -> Path:
    """Written next to the state file after the first successful save, so a later run can tell
    "first run" (no file, no marker) from "file lost after a prior run" (marker, no file)."""
    return path.with_name(path.name + ".seen")


def load_watch(
    path: Path,
    *,
    bus_path: Path | None = None,
    quotes_path: Path | None = None,
) -> tuple[StaleWatch, str | None]:
    """Return ``(watch, problem)``. problem is None on a clean load or a true first run.

    A first run (no state file and no marker) starts every streak at 0. Otherwise a missing file
    after a prior run, a read error, invalid JSON or a malformed state all return a fresh watch
    plus the problem, and the caller must count that problem as a stale check (fail closed).
    """
    fresh = StaleWatch(bus_path=bus_path, quotes_path=quotes_path)
    try:
        if not path.exists():
            if state_marker(path).exists():
                return fresh, STATE_MISSING
            return fresh, None
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return fresh, STATE_UNREADABLE
    try:
        data = json.loads(text)
    except ValueError:
        return fresh, STATE_CORRUPT
    try:
        return StaleWatch.from_dict(data, bus_path=bus_path, quotes_path=quotes_path), None
    except (ValueError, TypeError):
        return fresh, STATE_MALFORMED


def save_watch(watch: StaleWatch, path: Path) -> str | None:
    """Write the state atomically (temp file in the same folder, fsync, then os.replace) and
    then the marker. Returns None on success, else STATE_WRITE_FAILED (never raises)."""
    tmp_name = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(watch.to_dict(), ensure_ascii=False, sort_keys=True, allow_nan=False) + "\n"
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False
        ) as handle:
            tmp_name = handle.name
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        tmp_name = None
        marker = state_marker(path)
        if not marker.exists():
            marker.write_text("1\n", encoding="utf-8")
    except (OSError, ValueError, TypeError):
        return STATE_WRITE_FAILED
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return None


if SENTINEL_STALE_S != MAX_BOOK_AGE_S:  # pragma: no cover - guards a future edit
    raise RuntimeError("Sentinel and RiskGate must use the same per-check book age limit")
