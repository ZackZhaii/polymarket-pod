"""Task AJ: the end-to-end offline gate (backlog #24).

One run of the real pipeline on fixed, sanitised recordings in tests/fixtures/e2e/:

    Gamma market rows -> scanner.scan -> scanner.persist_markets            (markets.jsonl)
    CLOB /book bodies -> book.record (its getter hook reads the recording)  (books.jsonl)
    paper_cycle.run   -> gap / stale signals and Sentinel StaleWatch check 1
    endgame_scan      -> Endgame signals (to RiskGate only)
    forecasts         -> forecaster.emit_probability (NewsForecaster -> RiskGate signal)
    signal + book row + scanner row -> risk_gate.Intent  (build_intent below; no src module does this yet)
    paper_ledger.commit_if_allowed  -> RiskGate (fees.py, MIN_EDGE, staleness, wash, non_finite, caps)
        -> simulated paper fill -> ledger_store.book -> ledger_store.verify (conservation)
    paper_cycle.run   -> StaleWatch check 2 -> Sentinel halt after two consecutive stale checks

Nothing in src/ is replaced. Only the clock (a frozen ``datetime`` in podlib, book_staleness and
wash_detect, which also freezes every ``utc_now``) and I/O paths (bus, ledger, quotes, markets,
books and Sentinel state in a throwaway folder; the /book getter reads the recording; one
scenario's ledger path simulates a concurrent writer) are. socket, urllib, http.client and
podlib's urlopen are tripwires: a touch is recorded and fails the gate even if caught.

The expected output is tests/fixtures/e2e/golden.json. It is rewritten only by
``python -m pytest tests/test_e2e_offline.py --update-golden`` (off by default). gate() never
writes it. scripts/guard.py (check ``e2e``) runs gate() in-process: stdlib + src only.
"""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
import math
import re
import shutil
import socket
import sys
import tempfile
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES_REL = Path("tests") / "fixtures" / "e2e"
GOLDEN_NAME = "golden.json"
INPUT_FILES = ("scenario.json", "gamma_markets.json", "clob_books.json", "forecasts.json",
               "ledger_start.jsonl", "quotes_start.jsonl")
PINNED_RISK_SHA256 = "0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04"
FLOAT_DIGITS = 8  # golden rounding (formatting only, not a threshold)
NEW_FILL_ID = re.compile(r"[0-9a-f]{32}")
PATH_NAMES = ("BUS_PATH", "PAPER_PATH", "QUOTES_PATH", "MARKETS_PATH", "BOOKS_PATH", "SENTINEL_STATE_PATH",
              "SESSIONS_PATH")
BUS_KINDS = frozenset({"signal", "veto", "fill", "halt"})


class NetworkTouched(RuntimeError):
    """Raised by every network tripwire. The attempt is also recorded, so catching it does not hide it."""


# ---------------------------------------------------------------- frozen clock

_REAL_DATETIME = datetime


class _FrozenMeta(type):
    """isinstance(real_datetime, FrozenDateTime) stays True, so the patched modules' checks still work."""

    def __instancecheck__(cls, obj: object) -> bool:
        return isinstance(obj, _REAL_DATETIME)

    def __subclasscheck__(cls, sub: type) -> bool:
        return issubclass(sub, _REAL_DATETIME)


class Clock:
    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("clock must be aware")
        self.now = now

    def stamp(self) -> str:
        return self.now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def frozen_datetime(clock: Clock) -> type:
    class FrozenDateTime(_REAL_DATETIME, metaclass=_FrozenMeta):
        @classmethod
        def now(cls, tz=None):  # noqa: D102 - same signature as datetime.now
            current = clock.now
            return current.astimezone(tz) if tz is not None else current.astimezone().replace(tzinfo=None)

        @classmethod
        def utcnow(cls):  # noqa: D102
            return clock.now.astimezone(timezone.utc).replace(tzinfo=None)

    return FrozenDateTime


# ---------------------------------------------------------------- sandbox

@contextmanager
def _patched(patches: list[tuple[object, str, object]]) -> Iterator[None]:
    saved: list[tuple[object, str, object]] = []
    try:
        for owner, name, value in patches:
            saved.append((owner, name, getattr(owner, name)))
            setattr(owner, name, value)
        yield
    finally:
        for owner, name, value in reversed(saved):
            setattr(owner, name, value)


def _modules() -> dict[str, Any]:
    from src import (book, book_staleness, endgame, endgame_scan, forecaster, ledger_store, paper_cycle,
                     paper_ledger, podlib, risk_gate, scanner, sentinel, wash_detect)
    return {name: module for name, module in locals().items()}


def _stat(path: Path) -> tuple | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return (info.st_size, info.st_mtime_ns)


@contextmanager
def offline_sandbox(work: Path, clock: Clock) -> Iterator[dict[str, Any]]:
    """Freeze the clock, point every default data path into ``work`` and arm the network tripwires.

    Yields {"attempts": [...], "bus": Path, "paper": Path, "quotes": Path, "unused": Path,
    "defaults_before": {...}}. Everything is restored on exit.
    """
    mods = _modules()
    podlib = mods["podlib"]
    attempts: list[str] = []

    def tripwire(label: str):
        def blocked(*args: Any, **kwargs: Any):
            attempts.append(label)
            raise NetworkTouched(f"network touched in the offline gate: {label}")
        return blocked

    unused = work / "default_paths_must_stay_empty"
    unused.mkdir(parents=True, exist_ok=True)
    real_defaults = {name: Path(getattr(podlib, name)) for name in PATH_NAMES if hasattr(podlib, name)}
    paths = {"bus": work / "bus.jsonl", "paper": work / "paper.jsonl", "quotes": work / "quotes.jsonl"}
    explicit = {"BUS_PATH": paths["bus"], "PAPER_PATH": paths["paper"], "QUOTES_PATH": paths["quotes"]}
    patches: list[tuple[object, str, object]] = []
    for module in mods.values():
        for name in PATH_NAMES:
            if hasattr(module, name):
                patches.append((module, name, explicit.get(name, unused / name.lower())))
    frozen = frozen_datetime(clock)
    for name in ("podlib", "book_staleness", "wash_detect"):
        patches.append((mods[name], "datetime", frozen))
    patches += [
        (socket, "socket", tripwire("socket.socket")),
        (socket, "create_connection", tripwire("socket.create_connection")),
        (socket, "getaddrinfo", tripwire("socket.getaddrinfo")),
        (urllib.request, "urlopen", tripwire("urllib.request.urlopen")),
        (http.client.HTTPConnection, "connect", tripwire("http.client.HTTPConnection.connect")),
        (podlib, "urlopen", tripwire("podlib.urlopen")),
    ]
    before = {name: _stat(path) for name, path in real_defaults.items()}
    with _patched(patches):
        yield {**paths, "attempts": attempts, "unused": unused, "real_defaults": real_defaults,
               "defaults_before": before}


class ConcurrentWriterPath(type(Path())):
    """An I/O path whose ``fire_on``-th read first appends ``tail`` to the file: another writer
    tearing the ledger between RiskGate's state read and ledger_store.book's re-verify."""

    def setup(self, tail: bytes, fire_on: int = 2) -> ConcurrentWriterPath:
        self.tail, self.fire_on, self.reads, self.fired = tail, fire_on, 0, False
        return self

    def read_bytes(self) -> bytes:
        self.reads += 1
        if self.reads == self.fire_on and not self.fired:
            self.fired = True
            with open(self, "ab") as handle:
                handle.write(self.tail)
        return super().read_bytes()


# ---------------------------------------------------------------- fixtures and normalising

def fixtures_dir(root: Path | None = None) -> Path:
    return (root or ROOT) / FIXTURES_REL


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_now(text: str) -> datetime:
    value = _REAL_DATETIME.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return value


def norm(value: Any) -> Any:
    """Golden form: floats rounded, NaN/inf as strings, tuples as lists, dict keys sorted."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return "NaN" if math.isnan(value) else ("inf" if value > 0 else "-inf")
        rounded = round(value, FLOAT_DIGITS)
        return 0.0 if rounded == 0 else rounded
    if isinstance(value, dict):
        return {str(key): norm(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple)):
        return [norm(item) for item in value]
    return str(value)


def risk_sha(root: Path) -> str:
    """sha256 of config/risk.toml with CRLF normalised to LF (as guard's risk_sha_worktree reads it)."""
    return hashlib.sha256((root / "config" / "risk.toml").read_bytes().replace(b"\r\n", b"\n")).hexdigest()


# ---------------------------------------------------------------- glue: signal -> intent

def build_intent(forecast: dict[str, Any], signal: dict[str, Any], market_row: dict[str, Any],
                 books: dict[tuple[str, str], dict[str, Any]]):
    """The one stage with no src module: turn a NewsForecaster signal into a RiskGate Intent.

    model_prob comes from the bus signal payload, the token from the scanner row, the price
    (best ask to buy, best bid to sell), spread and book_ts from the latest recorded /book row,
    and liquidity from the scanner row. fee_rate and assumed_slippage are fixture inputs (no src
    module derives them per market). Never hypothetical; no counterparty.
    """
    from src.risk_gate import Intent

    token = str(market_row["token_ids"][forecast["outcome_index"]])
    book_row = books.get((market_row["id"], token)) or {}
    side = forecast["side"]
    price = book_row.get("best_ask") if side == "buy" else book_row.get("best_bid")
    return Intent(
        strategy=forecast["strategy"],
        market=market_row["id"],
        side=side,
        model_prob=signal["payload"]["model_prob"],
        market_price=price,
        assumed_slippage=forecast["assumed_slippage"],
        fee_rate=forecast["fee_rate"],
        liquidity_usd=market_row.get("liquidity_usd"),
        spread=book_row.get("spread"),
        token_id=token,
        hypothetical=False,
        note=forecast.get("note", ""),
        book_ts=book_row.get("book_ts"),
        counterparty=None,
    )


def _latest_books(rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        latest[(str(row.get("market_id") or ""), str(row.get("token_id") or ""))] = row
    return latest


def _ledger_view(path: Path) -> dict[str, Any]:
    from src import ledger_store

    data, read = ledger_store.read_ledger(path)
    problems = ledger_store.verify(read)
    cash, legs, _ = ledger_store.replay(read.rows, read.ids) if not problems else (None, {}, [])
    positions = {f"{market}|{token}": {"shares": leg["shares"], "cost_basis_usd": leg["cost_basis_usd"]}
                 for (market, token), leg in sorted(legs.items())}
    return {
        "lines": data.count(b"\n"),
        "unique_rows": len(read.rows),
        "duplicates_ignored": read.duplicates_ignored,
        "problems": problems,
        "cash_usd": cash,
        "positions": positions,
        "open_legs": sum(1 for leg in legs.values() if abs(leg["shares"]) > 1e-9),
        "ids": list(read.ids),
        "bytes": data,
    }


def _decision_row(label: str, intent, result: dict[str, Any], rows_before: int, rows_after: int) -> dict[str, Any]:
    decision = result.get("decision") or {}
    fill = result.get("fill") or {}
    row = {
        "label": label,
        "market": intent.market,
        "token_id": intent.token_id,
        "side": intent.side,
        "allow": result.get("allow"),
        "halted": bool(result.get("halted")),
        "reasons": list(decision.get("reasons") or []),
        "edge": decision.get("edge", fill.get("edge")),
        "fee_usd": decision.get("fee_usd", fill.get("fee_usd")),
        "booked_rows": rows_after - rows_before,
    }
    if fill:
        row["fill"] = {key: fill.get(key) for key in (
            "ts", "market_price", "effective_price", "notional_usd", "shares", "fee_usd", "cash_after_usd",
            "leg_shares_after", "leg_cost_basis_after_usd", "risk_decision", "hypothetical")}
    if result.get("halted"):
        row["halt_reason"] = result.get("halt_reason")
        row["ledger_problems"] = list(result.get("ledger_problems") or [])
    return row


# ---------------------------------------------------------------- the pipeline

def run_pipeline(fixtures: Path, work: Path, *, root: Path | None = None) -> dict[str, Any]:
    """Run every stage once. Returns {"actual": golden-comparable dict, "problems": [...], "detail": {...}}."""
    root = root or ROOT
    mods = _modules()
    podlib, scanner, book, paper_cycle = mods["podlib"], mods["scanner"], mods["book"], mods["paper_cycle"]
    endgame_scan, forecaster, paper_ledger = mods["endgame_scan"], mods["forecaster"], mods["paper_ledger"]
    ledger_store = mods["ledger_store"]

    scenario = load_json(fixtures / "scenario.json")
    raw_markets = load_json(fixtures / "gamma_markets.json")
    recorded_books = load_json(fixtures / "clob_books.json")["books"]
    forecasts = {item["label"]: item for item in load_json(fixtures / "forecasts.json")}
    # CRLF normalised: a Windows checkout must replay the same bytes (fixtures also carry .gitattributes eol=lf)
    ledger_start = (fixtures / "ledger_start.jsonl").read_bytes().replace(b"\r\n", b"\n")
    quotes_start = (fixtures / "quotes_start.jsonl").read_bytes().replace(b"\r\n", b"\n")

    problems: list[str] = []
    clock = Clock(parse_now(scenario["now"]))
    risk_before = risk_sha(root)
    risk_bytes_before = (root / "config" / "risk.toml").read_bytes()
    live_seen: list[object] = []
    work.mkdir(parents=True, exist_ok=True)
    markets_path, books_path = work / "markets.jsonl", work / "books.jsonl"
    state_path = work / "sentinel_stale.json"
    getter_calls: list[str] = []

    def book_getter(url: str) -> dict:
        token = (parse_qs(urlsplit(url).query).get("token_id") or [""])[0]
        getter_calls.append(token)
        if token not in recorded_books:
            raise LookupError(f"no recorded /book body for token {token}")
        return copy.deepcopy(recorded_books[token])

    with offline_sandbox(work, clock) as box:
        bus_path, paper_path, quotes_path = box["bus"], box["paper"], box["quotes"]
        paper_path.write_bytes(ledger_start)
        quotes_path.write_bytes(quotes_start)

        def risk_cfg() -> dict:
            cfg = podlib.load_risk()
            live_seen.append(cfg.get("live_enabled"))
            return cfg

        now_s = clock.stamp()
        cfg = risk_cfg()

        # 1. scanner rows
        limits = scanner.thresholds_from(cfg)
        scan = scanner.scan(raw_markets=copy.deepcopy(raw_markets), limits=limits, ts=now_s, now=clock.now)
        scanner.persist_markets(scan["rows"], markets_path, bus_path, bus_limit=scanner.BUS_TOP_N)
        # 2. recorded /book bodies through the real recorder
        book_count = book.record(scan["rows"], now_s, books_path=books_path, bus_path=bus_path, getter=book_getter)
        book_rows = podlib.read_jsonl(books_path)
        books = _latest_books(book_rows)
        markets = {row["id"]: row for row in podlib.read_jsonl(markets_path)}
        # 3. paper cycle + Sentinel check 1
        cycle_1 = paper_cycle.run(markets_path=markets_path, books_path=books_path, bus_path=bus_path, now=now_s,
                                  cfg=risk_cfg(), sentinel_state_path=state_path, quotes_path=quotes_path)
        # 4. Endgame signals
        candidates = endgame_scan.endgame_candidates(copy.deepcopy(raw_markets), limits, now_s, now=clock.now)
        endgame_signals = endgame_scan.emit_signals(candidates["rows"], now_s, bus_path=bus_path)

        # 5-8. forecasts -> intents -> RiskGate -> paper fill -> ledger_store -> conservation
        decisions: list[dict[str, Any]] = []
        booked_ids: list[str] = []
        booked_by_label: dict[str, str] = {}

        def commit(label: str, path: Path) -> dict[str, Any]:
            forecast = forecasts[label]
            signal = forecaster.emit_probability(market=forecast["market"], model_prob=forecast["model_prob"],
                                                 note=forecast.get("note", ""), bus_path=bus_path)
            intent = build_intent(forecast, signal, markets[forecast["market"]], books)
            before = _ledger_view(Path(path))  # a plain path: the gate's own reads never count as a writer's
            paper_ledger.PAPER_PATH = path
            result = paper_ledger.commit_if_allowed(intent, risk_cfg())
            after = _ledger_view(Path(path))
            row = _decision_row(label, intent, result, before["unique_rows"], after["unique_rows"])
            if result.get("allow"):
                booked_ids.append(result["fill"]["fill_id"])
                booked_by_label[label] = result["fill"]["fill_id"]
                if after["unique_rows"] != before["unique_rows"] + 1:
                    problems.append(f"{label}: allow did not book exactly one row")
            elif after["bytes"] != before["bytes"] and not result.get("halted"):
                problems.append(f"{label}: a veto changed the ledger")
            return row

        for label in scenario["intents"]:
            decisions.append(commit(label, paper_path))
        main_ledger = _ledger_view(paper_path)

        # duplicate fill replay: the same row written twice is deduped, then booking carries on
        replay = scenario["duplicate_replay"]
        source = next(d for d in decisions if d["label"] == replay["fill_of"])
        line = next(raw for raw in main_ledger["bytes"].splitlines(keepends=True)
                    if json.loads(raw).get("fill_id") == booked_by_label[source["label"]])
        paper_path.write_bytes(main_ledger["bytes"] + line)
        replayed = _ledger_view(paper_path)
        replay_state = paper_ledger.load_state(source["market"], source["token_id"])
        then = commit(replay["then_commit"], paper_path)
        after_replay = _ledger_view(paper_path)

        # corrupted ledger: (a) torn before the call -> invalid_risk_state veto; (b) torn mid-call -> halt
        corrupt = scenario["corrupt_ledger"]
        tail = corrupt["torn_tail"].encode("utf-8")
        torn_path = work / "corrupt_at_start" / "paper.jsonl"
        torn_path.parent.mkdir()
        torn_path.write_bytes(ledger_start + tail)
        torn = commit(corrupt["commit"], torn_path)
        torn_unchanged = torn_path.read_bytes() == ledger_start + tail
        mid_path = ConcurrentWriterPath(work / "corrupt_mid_write" / "paper.jsonl").setup(tail)
        mid_path.parent.mkdir()
        Path(mid_path).write_bytes(ledger_start)
        mid = commit(corrupt["commit"], mid_path)
        mid_bytes = Path(mid_path).read_bytes()
        paper_ledger.PAPER_PATH = paper_path

        # 9. Sentinel check 2: the stale market's second consecutive stale check halts it
        clock.now = clock.now + timedelta(seconds=scenario["second_check_after_s"])
        cycle_2 = paper_cycle.run(markets_path=markets_path, books_path=books_path, bus_path=bus_path,
                                  now=clock.stamp(), cfg=risk_cfg(), sentinel_state_path=state_path,
                                  quotes_path=quotes_path)
        quotes_after = podlib.read_jsonl(quotes_path)
        bus_rows = podlib.read_jsonl(bus_path)
        attempts = list(box["attempts"])
        unused_files = sorted(p.name for p in box["unused"].rglob("*") if p.is_file())
        defaults_after = {name: _stat(path) for name, path in box["real_defaults"].items()}
        defaults_before = box["defaults_before"]

    risk_after = risk_sha(root)
    risk_bytes_after = (root / "config" / "risk.toml").read_bytes()

    # ---------------------------------------------------------------- invariants (not golden)
    if attempts:
        problems.append(f"network touched: {attempts}")
    if risk_before != PINNED_RISK_SHA256 or risk_after != PINNED_RISK_SHA256:
        problems.append(f"risk.toml sha256 {risk_before} -> {risk_after} != pin")
    if risk_bytes_after != risk_bytes_before:
        problems.append("risk.toml bytes changed during the run")
    if not live_seen or any(value is not False for value in live_seen):
        problems.append(f"live_enabled was not false at every stage: {live_seen}")
    if unused_files:
        problems.append(f"a default data path was written: {unused_files}")
    if defaults_after != defaults_before:
        problems.append("a real data/ or logs/ file changed during the gate")
    if sorted(getter_calls) != sorted(set(getter_calls)) or not getter_calls:
        problems.append(f"book getter calls unexpected: {getter_calls}")
    for name, view in (("main", main_ledger), ("after_replay", after_replay)):
        if view["problems"]:
            problems.append(f"{name} ledger conservation: {view['problems']}")
        if len(view["ids"]) != len(set(view["ids"])):
            problems.append(f"{name} ledger has a repeated fill id")
    start_ids = set(_ledger_view_bytes(ledger_start))
    for fill_id in booked_ids:
        if not NEW_FILL_ID.fullmatch(fill_id) or fill_id in start_ids:
            problems.append(f"booked fill id not a fresh uuid4 hex: {fill_id}")
    if len(booked_ids) != len(set(booked_ids)):
        problems.append("a fill id was booked twice")
    if set(after_replay["ids"]) != start_ids | set(booked_ids):
        problems.append("ledger ids are not exactly the start ids plus the booked ids")
    if not torn_unchanged:
        problems.append("corrupt_at_start: the torn ledger was written")
    if mid_bytes != ledger_start + tail:
        problems.append("corrupt_mid_write: something was booked after the concurrent tear")
    problems.extend(_bus_problems(bus_rows, booked_ids))

    fills = [row for row in bus_rows if row.get("type") == "fill"]
    counts: dict[str, int] = {}
    for row in bus_rows:
        key = f"{row.get('from')}>{row.get('to')}:{row.get('type')}"
        counts[key] = counts.get(key, 0) + 1
    actual = {
        "clock": {"now": scenario["now"], "second_check": clock.stamp()},
        "scanner": {"scanned": scan["scanned"], "passed": [row["id"] for row in scan["rows"]],
                    "first_reason": scan["first_reason"]},
        "books": {"recorded": book_count, "rows": [
            {key: row.get(key) for key in ("market_id", "token_id", "outcome", "best_bid", "best_ask", "spread",
                                           "book_ts", "book_ts_problem")} for row in book_rows]},
        "cycle_1": _cycle(cycle_1),
        "endgame": {"candidates": [row["id"] for row in candidates["rows"]],
                    "first_reason": candidates["first_reason"],
                    "signals": [{"market": s["market"], **{k: s["payload"].get(k) for k in (
                        "outcome_index", "outcome", "token_id", "price", "exchange_action")}}
                                for s in endgame_signals]},
        "decisions": decisions,
        "ledger_after_intents": _ledger_golden(main_ledger),
        "duplicate_replay": {
            "lines": replayed["lines"], "unique_rows": replayed["unique_rows"],
            "duplicates_ignored": replayed["duplicates_ignored"], "problems": replayed["problems"],
            "cash_unchanged": replayed["cash_usd"] == main_ledger["cash_usd"],
            "positions_unchanged": replayed["positions"] == main_ledger["positions"],
            "load_state": {k: replay_state.get(k) for k in ("open_positions", "duplicate_fill_rows",
                                                           "leg_already_open", "ledger_problems")},
            "then": then,
            "ledger": _ledger_golden(after_replay),
        },
        "corrupt_at_start": {**torn, "ledger_unchanged": torn_unchanged},
        "corrupt_mid_write": {**mid, "ledger_is_start_plus_tear": mid_bytes == ledger_start + tail},
        "cycle_2": _cycle(cycle_2),
        "quotes_after": [{k: q.get(k) for k in ("market", "token_id", "side", "price", "status", "cancelled_by",
                                                "cancelled_ts")} for q in quotes_after],
        "bus": {"counts": counts, "fills": len(fills),
                "halts": [{"from": r.get("from"), "to": r.get("to"), "market": r.get("market"),
                           "reason": (r.get("payload") or {}).get("reason")}
                          for r in bus_rows if r.get("type") == "halt"]},
        "invariants": {"network_attempts": len(attempts), "risk_sha256": risk_after,
                       "live_enabled": sorted({str(v) for v in live_seen}), "live_checks": len(live_seen),
                       "booked_fill_ids": len(booked_ids)},
    }
    detail = {"bus": bus_rows, "booked_ids": booked_ids, "main_ledger": main_ledger, "after_replay": after_replay,
              "quotes": quotes_after, "attempts": attempts}
    return {"actual": norm(actual), "problems": problems, "detail": detail}


def _ledger_view_bytes(data: bytes) -> list[str]:
    from src import ledger_store
    return list(ledger_store.parse_ledger(data).ids)


def _ledger_golden(view: dict[str, Any]) -> dict[str, Any]:
    return {key: view[key] for key in ("lines", "unique_rows", "duplicates_ignored", "problems", "cash_usd",
                                       "positions", "open_legs")}


def _cycle(result: dict[str, Any]) -> dict[str, Any]:
    sentinel = result.get("sentinel") or {}
    return {"ok": result.get("ok"), "gaps": result.get("gaps"), "stale": result.get("stale"),
            "fills": result.get("fills"),
            "stale_markets": sorted(s["market"] for s in result.get("skips") or [] if s.get("kind") == "stale_market"),
            "sentinel_checked": sentinel.get("checked"), "sentinel_halts": sentinel.get("halts"),
            "state_problem": sentinel.get("state_problem"), "write_problem": sentinel.get("write_problem")}


def _bus_problems(rows: list[dict[str, Any]], booked_ids: list[str]) -> list[str]:
    """No exchange action anywhere: only paper message kinds, exchange_action none, no order flag,
    and every fill is an Executor fill of a RiskGate allow that is in the ledger."""
    problems = []
    fill_ids = []
    for index, row in enumerate(rows):
        kind, payload = row.get("type"), row.get("payload") or {}
        if kind not in BUS_KINDS:
            problems.append(f"bus[{index}] kind {kind!r} is not a paper kind")
        if payload.get("exchange_action", "none") != "none":
            problems.append(f"bus[{index}] exchange_action {payload.get('exchange_action')!r}")
        if payload.get("order", False) is not False:
            problems.append(f"bus[{index}] carries an order flag")
        if kind == "fill":
            if row.get("from") != "Executor" or payload.get("risk_decision") != "allow" or payload.get("hypothetical"):
                problems.append(f"bus[{index}] fill without a non-hypothetical Executor allow")
            fill_ids.append(payload.get("fill_id"))
    if fill_ids != booked_ids:
        problems.append(f"bus fills {fill_ids} != booked fills {booked_ids}")
    return problems


# ---------------------------------------------------------------- golden

def golden_problems(actual: Any, golden: Any, path: str = "$") -> list[str]:
    """Every difference between the run and golden.json, as JSON paths (exact, no tolerance)."""
    if isinstance(actual, dict) and isinstance(golden, dict):
        out = []
        for key in sorted(set(actual) | set(golden)):
            if key not in golden:
                out.append(f"{path}.{key}: not in golden")
            elif key not in actual:
                out.append(f"{path}.{key}: missing from the run")
            else:
                out.extend(golden_problems(actual[key], golden[key], f"{path}.{key}"))
        return out
    if isinstance(actual, list) and isinstance(golden, list):
        if len(actual) != len(golden):
            return [f"{path}: {len(actual)} items != golden {len(golden)}"]
        out = []
        for index, (a, g) in enumerate(zip(actual, golden)):
            out.extend(golden_problems(a, g, f"{path}[{index}]"))
        return out
    if type(actual) is not type(golden) or actual != golden:
        return [f"{path}: {actual!r} != golden {golden!r}"]
    return []


def golden_text(actual: dict[str, Any]) -> str:
    return json.dumps(actual, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def write_golden(fixtures: Path, actual: dict[str, Any]) -> Path:
    """Only tests/test_e2e_offline.py calls this, and only under --update-golden."""
    path = fixtures / GOLDEN_NAME
    path.write_text(golden_text(actual), encoding="utf-8", newline="\n")
    return path


def run_once(fixtures: Path, root: Path | None = None) -> dict[str, Any]:
    """run_pipeline in a throwaway temp folder that is always removed."""
    work = Path(tempfile.mkdtemp(prefix="pod-e2e-"))
    try:
        return run_pipeline(fixtures, work / "run", root=root)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def gate(root: Path | None = None) -> tuple[list[str], str]:
    """The hard gate: (problems, summary). Never writes the repo or golden.json."""
    root = root or ROOT
    fixtures = fixtures_dir(root)
    missing = [name for name in (*INPUT_FILES, GOLDEN_NAME) if not (fixtures / name).is_file()]
    if missing:
        return [f"missing fixture {FIXTURES_REL.as_posix()}/{name}" for name in missing], ""
    golden = load_json(fixtures / GOLDEN_NAME)
    result = run_once(fixtures, root)
    problems = list(result["problems"]) + golden_problems(result["actual"], golden)
    actual = result["actual"]
    decisions = actual["decisions"]
    summary = (f"{sum(1 for d in decisions if d['allow'])} allow / {sum(1 for d in decisions if not d['allow'])} veto, "
               f"cash {actual['ledger_after_intents']['cash_usd']}, sentinel halts {actual['cycle_2']['sentinel_halts']}, "
               f"network 0")
    return problems, summary


def main() -> int:
    problems, summary = gate()
    for problem in problems:
        print(f"FAIL {problem}")
    print(f"e2e: {'PASS' if not problems else 'FAIL'} {summary}".rstrip())
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
