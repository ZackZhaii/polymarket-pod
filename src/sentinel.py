"""Sentinel stub. Emits halt only and cancels local paper quotes. No CLOB call.

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

from src.book_staleness import MAX_BOOK_AGE_S, book_age_problem
from src.podlib import QUOTES_PATH, bus, read_jsonl, utc_now

# Sentinel's proposal (COORDINATION #9): a book older than 120 s is stale, and the alarm fires only
# after 2 consecutive stale or failed checks (30 s gave false alarms). Code constants, not risk.toml
# keys; the user decides at M2. The per-check limit equals RiskGate's MAX_BOOK_AGE_S.
SENTINEL_STALE_S = 120.0
SENTINEL_STALE_CONSECUTIVE = 2

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
) -> dict:
    cleaned = " ".join(reason.split())
    if not cleaned:
        raise ValueError("halt reason required")
    ts = utc_now()
    cancelled = cancel_open_quotes(quotes_path or QUOTES_PATH, ts, market)
    return bus(
        "Sentinel",
        "Executor",
        "halt",
        market,
        {
            "reason": cleaned,
            "paper_quotes_cancelled": cancelled,
            "exchange_action": "none",
        },
        path=bus_path,
    )


def cancel_open_quotes(path: Path, ts: str, market: str = "") -> int:
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
        path.write_text(body, encoding="utf-8")
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
        if streak >= self.consecutive and key not in self.alarmed:
            self.alarmed.add(key)
            message = halt(
                reason=f"{STALE_ALARM}: {streak} consecutive stale or failed book checks (last: {problem})",
                market=key,
                bus_path=self.bus_path,
                quotes_path=self.quotes_path,
            )
        return {"market": key, "stale": True, "problem": problem, "streak": streak, "halt": message}

    def to_dict(self) -> dict:
        return {
            "max_age_s": self.max_age_s,
            "consecutive": self.consecutive,
            "streaks": dict(sorted(self.streaks.items())),
            "alarmed": sorted(self.alarmed),
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
