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
    Issue #64: the comparison and the replace run under the issue #52 cross-process mutex
    (``halt_store.StateLock`` on ``<ledger>.lock``), never a thread lock or an extra unlocked
    read. The compare re-reads the ledger after the mutex is held, so a competing book that
    completed in the meantime is detected and kept (``LedgerIntegrityError``, our write is
    refused). A mutex that cannot be taken refuses the write too (``OSError``, fail closed).
    Issue #268 (Task DL): that replace is ``src.atomic_replace.replace_with_retry``: on Windows only, a
    ``PermissionError`` (another handle briefly holds the ledger) is retried a bounded number of times,
    still under the mutex. Any other exception, and every exception elsewhere, propagates at once as
    before; when the tries run out the ``PermissionError`` propagates unchanged, the temp file is removed
    and the old ledger is intact (fail closed).

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

(d) Risk-control fields (issue #68)
    A modern fill (one with a ``fill_id``) must carry its risk-control fields exactly:
    ``hypothetical`` and ``counts_toward_risk`` both present and JSON booleans, and only the two
    legal combinations — a real fill (false/true) or a hypothetical fill (true/false); and
    ``risk_decision`` must be the string ``allow`` (a fill is only ever booked on an allow).
    A string like ``'false'``, a number, null or a missing key is never coerced into a valid value:
    such a row is an integrity problem and is rejected before it is replayed into cash, positions
    or anything else (the truthy string ``'false'`` used to drop a fill from every risk total while
    its own conservation snapshot still verified). ``book`` refuses such a new row before it is
    applied or written. Legacy rows (no ``fill_id``) predate these fields and keep their old meaning.

(e) Settlement rows (issue #126, Task CG)
    A resolved market's open leg is closed by a settlement row: ``kind`` "settlement", ``risk_decision``
    "settlement" (the only other legal value, and only on such a row), a real row (hypothetical false,
    counts_toward_risk true), the closing side (a long is sold, a short bought back) at the payout (1 for
    the winning token, 0 otherwise) as both ``market_price`` and ``effective_price``, ``notional_usd`` =
    shares x payout, no fee (``fee_rate`` and ``fee_usd`` 0), and non-empty
    ``condition_id`` / ``winning_token_id``, ``settlement_payout`` the same finite number as the price, and
    ``closed_ts`` an ISO timestamp with a date, a time and a zone. It must close the whole open leg exactly (no partial close,
    no flip): ``settlement_not_closing``. Any other ``kind`` value, or a settlement field that is not
    exactly valid, is an integrity problem (``invalid_settlement``), so a damaged row is never replayed.
    Its fill_id is derived from the settlement (src/settlement.py), so booking it twice is idempotent.
    Its ``ts`` is checked like every counted row's, by src/daily_loss.py (an unusable ts is invalid_risk_state).

(f) Write-off rows (issue #197, Task CK, KEY-approved "close-then-write-off")
    When a held market is confirmed closed (src/market_close.py), each of its open legs gets one write-off
    row: ``kind`` "writeoff", ``risk_decision`` "writeoff", a real row (hypothetical false, counts_toward_risk
    true) that moves nothing: ``shares``, ``notional_usd``, both prices, ``fee_rate`` and ``fee_usd`` are all 0,
    so cash, shares and cost basis stay as they were (the leg still holds every limit until it settles). It
    names the closing side of the leg, ``writeoff_shares`` (the whole open leg, exactly), ``writeoff_usd``
    (finite, >= 0: the amount realized, checked by src/daily_loss.py against the average-cost replay) and
    ``closed_ts`` (the market's zoned closedTime). RiskGate's correction (#197, issuecomment-6109537257): shares
    held since the start of the UTC day are written off at max(cost, start-of-day mark), so the row also carries
    ``writeoff_cost_usd`` (the full cost) and ``writeoff_sod_uplift_usd`` (the start-of-day mark above that cost,
    0 when none); when present each must be a finite number >= 0 (src/daily_loss.py checks that they add up).
    A row without both (written before the correction) is the full cost exactly. It must sit on an open leg
    with exactly those shares and the leg must not be written off already (``writeoff_not_open``); a damaged row is ``invalid_writeoff``. Its
    fill_id is derived from the leg (market, token_id) (src/market_close.py: the write-off key is leg_key), so booking it twice is
    idempotent and a restart never writes the same leg off again.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from src.atomic_replace import replace_with_retry  # issue #268: bounded Windows PermissionError retry
from src.fees import booked_fee_usd, ledger_fee
from src.halt_store import StateLock  # issue #64: the issue #52 cross-process mutex, reused as-is
from src.thresholds import LEDGER_OPEN_EPS, PAPER_EQUITY_USD

FILL_ID_FIELD = "fill_id"
LEGACY_ID_PREFIX = "legacy:"
CASH_FIELD = "cash_after_usd"
LEG_SHARES_FIELD = "leg_shares_after"
LEG_COST_FIELD = "leg_cost_basis_after_usd"
SNAPSHOT_FIELDS = (CASH_FIELD, LEG_SHARES_FIELD, LEG_COST_FIELD)
LEDGER_NUMBER_FIELDS = ("shares", "notional_usd", "fee_usd")
SIDES = ("buy", "sell")
KIND_FIELD = "kind"  # issue #126: only settlement rows carry it
SETTLEMENT_KIND = "settlement"
SETTLEMENT_DECISION = "settlement"
SETTLEMENT_PAYOUTS = (0.0, 1.0)
WRITEOFF_KIND = "writeoff"  # issue #197 (Task CK)
WRITEOFF_DECISION = "writeoff"
KIND_DECISIONS = {SETTLEMENT_KIND: SETTLEMENT_DECISION, WRITEOFF_KIND: WRITEOFF_DECISION}


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


def control_field_problems(row: dict[str, Any], fill_id: str) -> list[str]:
    """Issue #68: strict types for a modern fill's risk-control fields. Never coerces anything.

    Both ``hypothetical`` and ``counts_toward_risk`` must be present and JSON booleans (a string,
    a number, null or a missing key is a problem, never a valid value); the only legal combinations
    are a real fill (hypothetical false / counts_toward_risk true) and a hypothetical fill
    (true / false). ``risk_decision`` must be the string ``allow``. Call for modern rows only: a
    legacy row (no ``fill_id``) predates these fields and keeps its old meaning (issue #68
    acceptance: the legacy compatibility rules stay as they are).
    """
    problems: list[str] = []
    values: dict[str, bool] = {}
    for name in ("hypothetical", "counts_toward_risk"):
        if name not in row:
            problems.append(f"missing_{name}:{fill_id}")
        elif not isinstance(row[name], bool):
            problems.append(f"invalid_{name}:{fill_id}")
        else:
            values[name] = row[name]
    if ("hypothetical" in values and "counts_toward_risk" in values
            and values["hypothetical"] == values["counts_toward_risk"]):
        problems.append(f"inconsistent_control_fields:{fill_id}")
    kind = row.get(KIND_FIELD) if KIND_FIELD in row else None
    if KIND_FIELD in row and (not isinstance(kind, str) or kind not in KIND_DECISIONS):
        problems.append(f"invalid_kind:{fill_id}")
    if "risk_decision" not in row:
        problems.append(f"missing_risk_decision:{fill_id}")
    elif row["risk_decision"] != ((KIND_DECISIONS.get(kind) if isinstance(kind, str) else None)
                                  if KIND_FIELD in row else "allow"):  # unhashable kind ([] / {}): invalid, never raise
        problems.append(f"invalid_risk_decision:{fill_id}")
    if kind == SETTLEMENT_KIND:
        problems.extend(settlement_field_problems(row, fill_id))
    elif kind == WRITEOFF_KIND:
        problems.extend(writeoff_field_problems(row, fill_id))
    return problems


def is_settlement(row: dict[str, Any]) -> bool:
    return row.get(KIND_FIELD) == SETTLEMENT_KIND


def settlement_field_problems(row: dict[str, Any], fill_id: str) -> list[str]:
    """Issue #126: a settlement row's own fields, exactly (see (e) in the module docstring)."""
    def number(name: str) -> float | None:
        return _finite(row.get(name))

    shares, payout = number("shares"), number("market_price")
    ok = (row.get("hypothetical") is False and row.get("counts_toward_risk") is True
          and row.get("side") in SIDES and shares is not None and shares > 0
          and payout in SETTLEMENT_PAYOUTS and number("effective_price") == payout
          and number("fee_rate") == 0.0 and number("fee_usd") == 0.0
          and number("notional_usd") is not None
          and abs(number("notional_usd") - shares * payout) <= LEDGER_OPEN_EPS  # type: ignore[operator]
          and all(isinstance(row.get(name), str) and row.get(name).strip()  # type: ignore[union-attr]
                  for name in ("condition_id", "winning_token_id", "token_id"))
          and (payout == 1.0) == (row.get("token_id") == row.get("winning_token_id"))
          # PR #194 review: the payout is booked twice (market_price and settlement_payout); both must be the
          # same finite number, and the market's close time must be a real zoned timestamp.
          and number("settlement_payout") == payout
          and _zoned_ts(row.get("closed_ts")))
    return [] if ok else [f"invalid_settlement:{fill_id}"]


def is_writeoff(row: dict[str, Any]) -> bool:
    return row.get(KIND_FIELD) == WRITEOFF_KIND


def writeoff_field_problems(row: dict[str, Any], fill_id: str) -> list[str]:
    """Issue #197: a write-off row's own fields, exactly (see (f) in the module docstring)."""
    def number(name: str) -> float | None:
        return _finite(row.get(name))

    shares, amount = number("writeoff_shares"), number("writeoff_usd")
    ok = (row.get("hypothetical") is False and row.get("counts_toward_risk") is True
          and row.get("side") in SIDES
          and all(number(name) == 0.0 for name in ("shares", "notional_usd", "market_price", "effective_price",
                                                   "fee_rate", "fee_usd"))
          and shares is not None and shares > LEDGER_OPEN_EPS and amount is not None and amount >= 0.0
          and all(isinstance(row.get(name), str) and row.get(name).strip()  # type: ignore[union-attr]
                  for name in ("market", "token_id"))
          and _zoned_ts(row.get("closed_ts"))
          # #197 correction (issuecomment-6109537257): the optional breakdown, when present, is a number >= 0
          and all(row.get(name) is None or (number(name) is not None and number(name) >= 0.0)  # type: ignore[operator]
                  for name in ("writeoff_cost_usd", "writeoff_sod_uplift_usd"))
          # #197 option A: the start-of-day snapshot's lateness, when present, is a bool
          # (an explicit null is invalid: the key is absent or a strict bool, PR #342 review 6183015083)
          and ("writeoff_sod_late" not in row or isinstance(row["writeoff_sod_late"], bool)))
    return [] if ok else [f"invalid_writeoff:{fill_id}"]


def _zoned_ts(value: object) -> bool:
    """A settlement row's closed_ts: an ISO 8601 string with a date, a time and a zone (Z or an offset)."""
    if not isinstance(value, str) or not value.strip():
        return False
    text = value.strip()
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


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

    Call only after non_finite_ledger_rows(rows) is empty. Issue #68: a modern row whose
    risk-control fields are not provably valid is reported and nothing is replayed (the same
    fail-closed rule as non-finite numbers).
    """
    control: list[str] = []
    for row, fill_id in zip(rows, ids):
        if FILL_ID_FIELD in row:
            control.extend(control_field_problems(row, fill_id))
    if control:
        return PAPER_EQUITY_USD, {}, control
    cash = PAPER_EQUITY_USD
    legs: dict[tuple[str, str], dict[str, float]] = {}
    problems: list[str] = []
    written_off: set[tuple[str, str]] = set()  # issue #197: open legs that already carry a write-off row
    for row, fill_id in zip(rows, ids):
        key = leg_key(row.get("market"), row.get("token_id"))
        if counts(row) and is_writeoff(row):  # issue #197: on an open leg, all of it, once
            held = legs.get(key, {"shares": 0.0})["shares"]
            closing = "sell" if held > 0 else "buy"
            if (abs(held) <= LEDGER_OPEN_EPS or key in written_off or row.get("side") != closing
                    or abs(abs(held) - float(row.get("writeoff_shares") or 0.0)) > LEDGER_OPEN_EPS):
                problems.append(f"writeoff_not_open:{fill_id}")
            written_off.add(key)
        if counts(row) and is_settlement(row):  # issue #126: it closes the whole open leg, exactly
            held = legs.get(leg_key(row.get("market"), row.get("token_id")), {"shares": 0.0})["shares"]
            closing = "sell" if held > 0 else "buy"
            if (abs(held) <= LEDGER_OPEN_EPS or row.get("side") != closing
                    or abs(abs(held) - float(row.get("shares") or 0.0)) > LEDGER_OPEN_EPS):
                problems.append(f"settlement_not_closing:{fill_id}")
        if counts(row):
            cash = _apply(cash, legs, row)
            if abs(legs[key]["shares"]) <= LEDGER_OPEN_EPS:
                written_off.discard(key)  # the leg is flat (settled): a write-off belongs to that position only
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
    """Every integrity problem of a parsed ledger: parse, ids, Task U numbers, conservation.

    Issue #68: every modern row's risk-control fields are checked before that row is replayed.
    """
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
    removed and the old ledger is untouched. Issue #64: the fresh read, the comparison and the
    replace all run under the cross-process mutex (issue #52), so another process's completed
    book can neither slip in between them nor be overwritten by ours; a stale ``expected`` is a
    LedgerIntegrityError that keeps the other writer's rows, and a mutex we cannot take is an
    OSError before anything is written (fail closed). Issue #268: the replace retries a Windows
    PermissionError a bounded number of times (src/atomic_replace.py); exhausted, it raises and the temp
    file is removed as for any other failure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp, "xb") as handle:
            handle.write(expected + line)
            handle.flush()
            os.fsync(handle.fileno())
        mutex = StateLock(path)
        if not mutex.acquire():
            raise OSError("ledger_lock_failed")
        try:
            current = path.read_bytes() if path.exists() else b""
            if current != expected:
                raise LedgerIntegrityError(["ledger_changed_during_write"])
            replace_with_retry(tmp, path)
        finally:
            mutex.release()
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def book(path: Path, fill: dict[str, Any], *, unique_decision_id: bool = False) -> dict[str, Any]:
    """Verify the ledger, stamp the fill's id and post-fill state, write it atomically, re-verify.

    Raises LedgerIntegrityError (halt; nothing booked unless the problem is post_write) or OSError
    (write failed; old ledger intact). Returns the row as written.

    Issue #120 (Task CJ): when ``unique_decision_id`` is True (M2 runner only), the same ``decision_id``
    is booked at most once. An existing counted row with that id returns
    ``{"already_booked": True, "fill": <existing row>}`` without writing and without raising. A missing
    or blank ``decision_id`` is refused (M2 fills must be joinable). Default False keeps every older
    call path and the e2e golden unchanged.
    """
    data, current = read_ledger(path)
    problems = verify(current)
    if problems:
        raise LedgerIntegrityError(problems)
    row = dict(fill)
    if FILL_ID_FIELD not in row:
        row = {FILL_ID_FIELD: new_fill_id(), **row}
    if unique_decision_id:
        decision_id = row.get("decision_id")
        if not (isinstance(decision_id, str) and decision_id.strip()):
            raise LedgerIntegrityError([f"missing_decision_id:{row[FILL_ID_FIELD]}"])
        for existing in current.rows:
            if counts(existing) and existing.get("decision_id") == decision_id:
                return {"already_booked": True, "fill": existing}
    control = control_field_problems(row, row[FILL_ID_FIELD])  # issue #68
    if control:
        # Refused before the new row is replayed or written: the old ledger bytes stand.
        raise LedgerIntegrityError(control)
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
    if not problems:
        problems = _post_write_row_problems(after, row[FILL_ID_FIELD], json.loads(text))
    if problems:
        raise LedgerIntegrityError(["post_write:" + problem for problem in problems])
    return row


def _post_write_row_problems(after: LedgerRead, fill_id: str, written: dict[str, Any]) -> list[str]:
    """Task DT: our row must be present by fill_id and equal, field by field, to what we wrote.

    It need not be the last line: another process may append right after our locked replace.
    Missing -> ``not_appended``; any differing, missing or extra field -> ``row_mismatch`` (fail closed).
    """
    if fill_id not in after.ids:
        return [f"not_appended:{fill_id}"]
    stored = after.rows[after.ids.index(fill_id)]
    fields = sorted(set(stored) | set(written))
    for name in fields:
        if name not in stored or name not in written or canonical({"v": stored[name]}) != canonical({"v": written[name]}):
            return [f"row_mismatch:{name}:{fill_id}"]
    return []
