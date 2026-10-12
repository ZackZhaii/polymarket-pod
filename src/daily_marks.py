"""Task CF (#124): the start-of-day leg marks snapshot, ``runs/m2/daily_marks/<UTC day>.json``.

Poly风控's ruling, point 5. One file per UTC day, written once and never overwritten:

* content ``{day, created_at, late, legs: [{leg_key, shares, cost_basis_usd, mark_usd, mark_source,
  book_ts, mark_invalid}], sha256}``; ``sha256`` is the hex digest of the canonical JSON (sorted keys,
  no spaces) of every other field;
* written by the first risk evaluation of the UTC day: a temp file in the same folder (fsynced), then an
  exclusive create of the day's name (``os.link``: it fails when the name exists), so a second write the
  same day is refused (``SnapshotExists``) and never replaces the first;
* ``late`` is True when the writing process did not evaluate on the previous UTC day (it was not running
  at the day roll, e.g. after a restart): every leg held at the start of the day then uses its cost as
  ``ref`` (src/leg_marks.py), so the loss between 00:00 and the late write is never hidden;
* read back with every field checked: a file that cannot be read or parsed, a wrong ``day``, a sha256
  that does not match or a bad leg is a problem. The caller then reports invalid_risk_state (a global
  halt, Task AL); the file is never deleted or rebuilt here.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

FIELDS = ("day", "created_at", "late", "legs", "sha256")
LEG_FIELDS = ("leg_key", "shares", "cost_basis_usd", "mark_usd", "mark_source", "book_ts", "mark_invalid")


class SnapshotExists(Exception):
    """The day's snapshot already exists; it is never overwritten."""


def snapshot_path(folder: Path, day: str) -> Path:
    return Path(folder) / f"{day}.json"


def digest(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "sha256"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def leg_problems(legs: object) -> list[str]:
    if not isinstance(legs, list):
        return ["daily_marks_legs_not_list"]
    problems = []
    seen = set()
    for index, leg in enumerate(legs):
        if not isinstance(leg, dict) or set(leg) != set(LEG_FIELDS):
            problems.append(f"daily_marks_bad_leg:{index}")
            continue
        ok = (isinstance(leg["leg_key"], str) and leg["leg_key"] not in seen
              and _number(leg["shares"]) and _number(leg["cost_basis_usd"]) and leg["cost_basis_usd"] >= 0
              and _number(leg["mark_usd"]) and isinstance(leg["mark_source"], str)
              and (leg["book_ts"] is None or isinstance(leg["book_ts"], str))
              and isinstance(leg["mark_invalid"], bool))
        if not ok:
            problems.append(f"daily_marks_bad_leg:{index}")
        else:
            seen.add(leg["leg_key"])
    return problems


def read_snapshot(folder: Path, day: str) -> tuple[dict[str, Any] | None, list[str]]:
    """``(None, [])`` when the day has no snapshot yet; ``(snapshot, [])`` when it is intact; else problems."""
    path = snapshot_path(folder, day)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None, []
    except OSError as exc:
        return None, [f"daily_marks_unreadable:{type(exc).__name__}"]
    try:
        snap = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, ["daily_marks_unparseable"]
    if not isinstance(snap, dict) or set(snap) != set(FIELDS):
        return None, ["daily_marks_bad_fields"]
    problems = []
    if snap["day"] != day:
        problems.append("daily_marks_day_mismatch")
    if not isinstance(snap["created_at"], str) or not isinstance(snap["late"], bool):
        problems.append("daily_marks_bad_header")
    if not isinstance(snap["sha256"], str):
        problems.append("daily_marks_sha_mismatch")
    else:
        try:
            if digest(snap) != snap["sha256"]:
                problems.append("daily_marks_sha_mismatch")
        except ValueError:
            problems.append("daily_marks_sha_mismatch")
    problems.extend(leg_problems(snap["legs"]))
    return (None, problems) if problems else (snap, [])


def create_snapshot(folder: Path, day: str, created_at: str, late: bool, legs: list[dict[str, Any]]) -> dict[str, Any]:
    """Write the day's snapshot once. Raises SnapshotExists when it is already there, OSError on a failed write."""
    folder = Path(folder)
    payload: dict[str, Any] = {"day": day, "created_at": created_at, "late": bool(late), "legs": legs}
    payload["sha256"] = digest(payload)
    folder.mkdir(parents=True, exist_ok=True)
    final = snapshot_path(folder, day)
    if final.exists():
        raise SnapshotExists(str(final.name))
    tmp = folder / f".{day}.{os.getpid()}.tmp"
    tmp.unlink(missing_ok=True)  # a temp file left by a crashed writer with the same pid
    data = json.dumps(payload, sort_keys=True, indent=1, allow_nan=False).encode("utf-8") + b"\n"
    try:
        with open(tmp, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp, final)  # exclusive create: fails when the day's file exists
        except FileExistsError as exc:
            raise SnapshotExists(str(final.name)) from exc
    finally:
        tmp.unlink(missing_ok=True)
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        fd = None
    if fd is not None:
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)
    return payload
