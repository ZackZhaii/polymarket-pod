"""Task AF (backlog #20): an integrity layer for the paper ledger. Standard library only.

The ledger is still ``logs/paper.jsonl``: one JSON object per line, appended in order. This module
reads it strictly, gives every fill an id, writes it atomically and checks that cash and positions
are conserved. Anything it cannot prove consistent is an integrity problem; paper_ledger then
returns an invalid risk state (RiskGate vetoes ``invalid_risk_state``, as for Task U) and books
nothing more. All of this is tightening only and fails closed. No orders, keys or network.

(a) Fill ids and dedupe
    New fills carry ``fill_id`` (``uuid4().hex``, written by paper_ledger). A repeated ``fill_id``
    whose row is identical (same canonical JSON) is ignored idempotently: counted once,
    reported in ``duplicates_ignored``. A repeated ``fill_id`` with a different row is
    ``conflicting_fill_id`` (invalid state). A ``fill_id`` that is not a non-empty string, or that
    uses the reserved ``legacy:`` prefix, is ``invalid_fill_id``.
    Rows written before Task AF have no id, because that schema had none. A row without
    ``fill_id`` is accepted only before the first row that has one (the legacy prefix of the file)
    and gets the deterministic derived id ``legacy:<line number>:<sha256 of its canonical JSON>``.
    The line number is part of the id on purpose: two identical legacy rows are two real fills
    (the old schema could not tell a retry from a second fill), so they are never collapsed and no
    exposure is hidden. Appends never change earlier line numbers, so the id is stable.
    A row without ``fill_id`` after an id'd row is ``missing_fill_id`` (invalid state).

(b) Atomic writes
    ``atomic_append`` writes the old bytes plus the new line to a temp file in the same
    directory, flushes and fsyncs it, checks that the ledger did not change meanwhile, then
    ``os.replace``s it over the ledger and (POSIX) fsyncs the directory. Any failure before the
    replace removes the temp file and leaves the old ledger byte-for-byte intact. A torn line from
    an older appender (no final newline) is ``unterminated_last_row``; malformed or non-object
    lines are ``malformed_row`` / ``non_object_row`` (podlib.read_jsonl silently skips them; the
    ledger no longer does).

(c) Conservation
    Replaying the counted rows (not hypothetical, counts_toward_risk not False; the same rows
    paper_ledger nets) from ``PAPER_EQUITY_USD``:
        cash  = PAPER_EQUITY_USD - buy notional + sell notional - fees
        leg shares = buy shares - sell shares            (per market + token_id leg)
        leg cost basis = buy notional - sell notional    (per leg)
    A row without notional_usd is valued at 1 USD per share, exactly like paper_ledger's
    leg_positions. Every id'd row carries the state after it (``cash_after_usd``,
    ``leg_shares_after``, ``leg_cost_basis_after_usd``); each must equal the replay within
    ``LEDGER_OPEN_EPS`` (no new number). Every id'd row's fee_usd must also be at least
    ``booked_fee_usd(ledger_fee(...))`` recomputed from the row's own fee_rate, prices, notional and
    shares (src/fees.py, Task AE). A missing, non-finite or mismatching value is a problem.
    This runs on every load and on the candidate ledger before and after each write.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.fees import booked_fee_usd, ledger_fee
from src.thresholds import LEDGER_OPEN_EPS, PAPER_EQUITY_USD

FILL_ID_FIELD = "fill_id"
LEGACY_ID_PREFIX = "legacy:"
CASH_FIELD = "cash_after_usd"
LEG_SHARES_FIELD = "leg_shares_after"
LEG_COST_FIELD = "leg_cost_basis_after_usd"
SNAPSHOT_FIELDS = (CASH_FIELD, LEG_SHARES_FIELD, LEG_COST_FIELD)
LEDGER_NUMBER_FIELDS = ("shares", "notional_usd", "fee_usd")
SIDES = ("buy", "sell")


class LedgerIntegrityError(RuntimeError):
    """The ledger (or the row about to be booked) is not provably consistent. Halt."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("ledger integrity: " + ", ".join(self.problems))


@dataclass
class LedgerRead:
    rows: list[dict[str, Any]] = field(default_factory=list)  # unique fills, file order
    ids: list[str] = field(default_factory=list)  # fill id per row (derived for legacy rows)
    problems: list[str] = field(default_factory=list)
    duplicates_ignored: int = 0


def counts(row: dict[str, Any]) -> bool:
    """A row that counts toward risk: not hypothetical and counts_toward_risk is not False."""
    return not row.get("hypothetical") and row.get("counts_toward_risk") is not False


def leg_key(market: object, token_id: object) -> tuple[str, str]:
    """A leg is one token in one market. No token_id means an unknown leg of that market.

    The unknown leg nets only with other rows that also lack a token_id; it never
    offsets a named token.
    """
    return (str(market or ""), str(token_id or ""))


def non_finite_ledger_rows(rows: list[dict[str, Any]]) -> list[int]:
    """Task U: indexes of counted rows whose shares / notional_usd / fee_usd is NaN, inf or garbage.

    A NaN share count makes abs(net) > OPEN_EPS False, which would hide an open leg,
    and a NaN fee poisons the daily total. Absent (None) values keep their old meaning.
    """
    bad: list[int] = []
    for index, row in enumerate(rows):
        if not counts(row):
            continue
        for name in LEDGER_NUMBER_FIELDS:
            value = row.get(name)
            if value is None:
                continue
            try:
                number = float(value)
            except (TypeError, ValueError):
                bad.append(index)
                break
            if not math.isfinite(number):
                bad.append(index)
                break
    return bad


def canonical(row: dict[str, Any]) -> str:
    """Key-order independent JSON text of a row (used for identity and legacy ids)."""
    return json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def legacy_fill_id(line_number: int, row: dict[str, Any]) -> str:
    """Deterministic id for a pre-Task-AF row: legacy:<1-based line>:<sha256(canonical JSON)>."""
    digest = hashlib.sha256(canonical(row).encode("utf-8")).hexdigest()
    return f"{LEGACY_ID_PREFIX}{line_number}:{digest}"


def new_fill_id() -> str:
    return uuid.uuid4().hex


def parse_ledger(data: bytes) -> LedgerRead:
    """Strictly parse ledger bytes. Never raises; every defect is a named problem."""
    result = LedgerRead()
    if not data:
        return result
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        result.problems.append("undecodable_ledger")
        return result
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    else:
        result.problems.append("unterminated_last_row")
    seen: dict[str, dict[str, Any]] = {}
    ids_started = False
    for number, line in enumerate(lines, start=1):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            result.problems.append(f"malformed_row:{number}")
            continue
        if not isinstance(row, dict):
            result.problems.append(f"non_object_row:{number}")
            continue
        if FILL_ID_FIELD in row:
            fill_id = row[FILL_ID_FIELD]
            if not isinstance(fill_id, str) or not fill_id.strip() or fill_id.startswith(LEGACY_ID_PREFIX):
                result.problems.append(f"invalid_fill_id:{number}")
                continue
            ids_started = True
            prior = seen.get(fill_id)
            if prior is not None:
                if canonical(prior) == canonical(row):
                    result.duplicates_ignored += 1
                else:
                    result.problems.append(f"conflicting_fill_id:{fill_id}")
                continue
            seen[fill_id] = row
        elif ids_started:
            result.problems.append(f"missing_fill_id:{number}")
            continue
        else:
            fill_id = legacy_fill_id(number, row)
        result.rows.append(row)
        result.ids.append(fill_id)
    return result


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _apply(cash: float, legs: dict[tuple[str, str], dict[str, float]], row: dict[str, Any]) -> float:
    """Book one counted row into the replay; returns the new cash. Mirrors leg_positions."""
    qty = float(row.get("shares") or 0.0)
    notional = row.get("notional_usd")
    usd = abs(qty) * 1.0 if notional is None else abs(float(notional))
    fee = float(row.get("fee_usd") or 0.0)
    leg = legs.setdefault(leg_key(row.get("market"), row.get("token_id")),
                          {"shares": 0.0, "cost_basis_usd": 0.0})
    if row.get("side") == "buy":
        cash -= usd
        leg["shares"] += qty
        leg["cost_basis_usd"] += usd
    else:
        cash += usd
        leg["shares"] -= qty
        leg["cost_basis_usd"] -= usd
    return cash - fee


def _snapshot(cash: float, legs: dict[tuple[str, str], dict[str, float]], row: dict[str, Any]) -> dict[str, float]:
    leg = legs.get(leg_key(row.get("market"), row.get("token_id")), {"shares": 0.0, "cost_basis_usd": 0.0})
    return {CASH_FIELD: cash, LEG_SHARES_FIELD: leg["shares"], LEG_COST_FIELD: leg["cost_basis_usd"]}


def _fee_problems(row: dict[str, Any], fill_id: str) -> list[str]:
    booked = _finite(row.get("fee_usd"))
    expected = ledger_fee(row.get("fee_rate"), row.get("market_price"), row.get("effective_price"),
                          row.get("notional_usd"), row.get("shares"))
    if booked is None or expected is None:
        return [f"invalid_fee:{fill_id}"]
    if booked + LEDGER_OPEN_EPS < booked_fee_usd(expected):
        return [f"fee_below_ledger_fee:{fill_id}"]
    return []


def replay(rows: list[dict[str, Any]], ids: list[str]) -> tuple[float, dict[tuple[str, str], dict[str, float]], list[str]]:
    """Replay counted rows from PAPER_EQUITY_USD; returns (cash, legs, conservation problems).

    Call only after non_finite_ledger_rows(rows) is empty.
    """
    cash = PAPER_EQUITY_USD
    legs: dict[tuple[str, str], dict[str, float]] = {}
    problems: list[str] = []
    for row, fill_id in zip(rows, ids):
        if counts(row):
            cash = _apply(cash, legs, row)
        if FILL_ID_FIELD not in row:
            continue  # legacy row: no stored state to compare
        if row.get("side") not in SIDES:
            problems.append(f"invalid_side:{fill_id}")
        problems.extend(_fee_problems(row, fill_id))
        expected = _snapshot(cash, legs, row)
        for name, kind in ((CASH_FIELD, "cash_mismatch"), (LEG_SHARES_FIELD, "position_mismatch"),
                           (LEG_COST_FIELD, "cost_basis_mismatch")):
            stored = _finite(row.get(name))
            if stored is None:
                problems.append(f"missing_snapshot:{name}:{fill_id}")
            elif abs(stored - expected[name]) > LEDGER_OPEN_EPS:
                problems.append(f"{kind}:{fill_id}")
    return cash, legs, problems


def verify(read: LedgerRead) -> list[str]:
    """Every integrity problem of a parsed ledger: parse, ids, Task U numbers, conservation."""
    problems = list(read.problems)
    bad = non_finite_ledger_rows(read.rows)
    if bad:
        problems.extend(f"non_finite_row:{read.ids[index]}" for index in bad)
        return problems  # cannot replay non-finite numbers
    _, _, conservation = replay(read.rows, read.ids)
    problems.extend(conservation)
    return problems


def read_ledger(path: Path) -> tuple[bytes, LedgerRead]:
    data = path.read_bytes() if path.exists() else b""
    return data, parse_ledger(data)


def _fsync_dir(directory: Path) -> None:
    """Make the rename durable on POSIX. Best effort: Windows cannot open a directory."""
    if os.name == "nt":
        return
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_append(path: Path, expected: bytes, line: bytes) -> None:
    """Replace ``path`` (whose bytes must still be ``expected``) with ``expected + line``, atomically.

    Temp file in the same directory, flush + fsync, os.replace. On any failure the temp file is
    removed and the old ledger is untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "xb") as handle:
            handle.write(expected + line)
            handle.flush()
            os.fsync(handle.fileno())
        current = path.read_bytes() if path.exists() else b""
        if current != expected:
            raise LedgerIntegrityError(["ledger_changed_during_write"])
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def book(path: Path, fill: dict[str, Any]) -> dict[str, Any]:
    """Verify the ledger, stamp the fill's id and post-fill state, write it atomically, re-verify.

    Raises LedgerIntegrityError (halt; nothing booked unless the problem is post_write) or OSError
    (write failed; old ledger intact). Returns the row as written.
    """
    data, current = read_ledger(path)
    problems = verify(current)
    if problems:
        raise LedgerIntegrityError(problems)
    row = dict(fill)
    if FILL_ID_FIELD not in row:
        row = {FILL_ID_FIELD: new_fill_id(), **row}
    cash, legs, _ = replay(current.rows, current.ids)
    if counts(row):
        if non_finite_ledger_rows([row]):
            raise LedgerIntegrityError([f"non_finite_row:{row[FILL_ID_FIELD]}"])
        cash = _apply(cash, legs, row)
    row.update(_snapshot(cash, legs, row))
    try:
        text = json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except ValueError as exc:
        raise LedgerIntegrityError([f"non_finite_value:{row[FILL_ID_FIELD]}"]) from exc
    line = (text + "\n").encode("utf-8")
    candidate = parse_ledger(data + line)
    problems = verify(candidate)
    if not problems and (len(candidate.rows) != len(current.rows) + 1 or candidate.rows[-1] != row):
        problems = [f"not_appended:{row[FILL_ID_FIELD]}"]
    if problems:
        raise LedgerIntegrityError(["pre_write:" + problem for problem in problems])
    atomic_append(path, data, line)
    _, after = read_ledger(path)
    problems = verify(after)
    if not problems and (not after.ids or after.ids[-1] != row[FILL_ID_FIELD]):
        problems = [f"not_appended:{row[FILL_ID_FIELD]}"]
    if problems:
        raise LedgerIntegrityError(["post_write:" + problem for problem in problems])
    return row
