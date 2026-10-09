"""M1 24-hour acceptance run (Task AI): a long, read-only recording of the public order books.

    python -m src.m1_run --out runs/m1-YYYYMMDD --max-runtime 86400 [--top N] [--assets TOKEN ...]
        [--markets data/markets.jsonl] [--rest-every 600] [--rest-assets 4]
        [--rotate-mb 64] [--rotate-s 3600] [--raw-books]

What it does (no orders, no auth, no keys; the only network reads are the public WS market channel
through src.ws_recorder and the public CLOB /book path through src.book.snapshot):

- Assets come from the scanner output already on disk (``data/markets.jsonl``): the latest scan
  batch (by parsed ``ts``), in its 24h-volume order, markets whose version-aware ``token_ids`` are
  usable (``token_ids_problem`` empty, a ``version``, two string ids), the first ``--top`` markets,
  both outcome tokens each. It never scans Gamma itself: run ``python -m src.scanner`` first.
  ``--assets`` overrides the selection (metadata still comes from markets.jsonl).
- The WS recorder (src.ws_recorder.Recorder, unchanged rules) writes book rows to
  ``books/books-NNNNNN.jsonl``; reconnects, disconnects, backoffs, resyncs (with reasons),
  resubscriptions and assets going live are written to ``events/events-NNNNNN.jsonl``.
- Every ``--rest-every`` seconds (and once at start) a background thread samples the first
  ``--rest-assets`` assets through ``book.snapshot`` (the existing public CLOB /book reader) into
  ``rest/rest-NNNNNN.jsonl`` for the WS-vs-REST comparison in src.m1_report.
- ``--raw-books`` (Task AY, opt-in, default off so a run's disk use does not change): every ``book`` frame
  of a subscribed asset, as received (all levels, the hash, min_order_size / tick_size when sent), goes
  to ``raw_books/raw_books-NNNNNN.jsonl`` as ``{"ts", "asset_id", "frame"}``, for validating the snapshot
  hash offline (``python -m src.ws_hash_check <run dir>``). A file never passes
  WS_RAW_BOOK_FILE_MAX_BYTES (nor ``--rotate-mb``); after WS_RAW_BOOK_RUN_MAX_BYTES in the run further
  frames are dropped and counted (one ``raw_books_capped`` event), and the recording itself goes on.
  Frames that cannot be written as strict JSON (NaN) are dropped and counted. Nothing else changes.
- Every JSONL file is append-only (src.ws_recorder.JsonlAppender: one write per line) and is
  rotated before it would pass ``--rotate-mb`` or once it has been open ``--rotate-s`` seconds.
- SIGINT / SIGTERM end the run gracefully: the recorder notices within WS_STOP_POLL_S, the REST
  thread stops, files are closed (fsync) and the manifest is finished with status ``interrupted``.
- ``manifest.json`` (atomic replace) is written at start (status ``running``) and at the end:
  start/end times, elapsed seconds, assets with their market metadata, settings, thresholds,
  versions (Python, websockets, schema), the git sha (and whether the tree was dirty), the
  config/risk.toml sha256 at start and end and live_enabled, the files written and the counts.

Exit codes: 0 ran to --max-runtime and recorded at least one snapshot; 1 no snapshot recorded;
2 usage error (bad arguments, no usable assets, a non-empty --out); 3 the optional websockets
package is missing; 4 writing an output file failed; 5 stopped by SIGINT/SIGTERM (the run dir is
complete and can still be reported, but it is not a full 24 h run); 6 config/risk.toml off its
pins at start (no run dir) or while running (Task AS, src/pins.py; a global halt is recorded).
Registry: docs/EXIT_CODES.md.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import thresholds  # noqa: E402
from src.book import snapshot as rest_snapshot  # noqa: E402
from src.book_staleness import parse_book_ts  # noqa: E402
from src.podlib import MARKETS_PATH, RISK_PATH, parse_risk_toml, read_jsonl  # noqa: E402
from src.thresholds import (  # noqa: E402
    M1_REST_ASSETS,
    M1_REST_EVERY_S,
    M1_ROTATE_BYTES,
    M1_ROTATE_S,
    M1_TOP_MARKETS,
    WS_RAW_BOOK_FILE_MAX_BYTES,
    WS_RAW_BOOK_RUN_MAX_BYTES,
    WS_STOP_POLL_S,
)
from src.ws_recorder import (  # noqa: E402
    RAW_BOOKS,  # Task BA: moved to ws_recorder with RotatingJsonl / RawBookSink (re-exported here)
    JsonlAppender,
    RawBookSink,
    Recorder,
    RotatingJsonl,
    Transport,
    TransportUnavailable,
    WebsocketsTransport,
    _line_bytes,
    asset_meta,
    validate_assets,
)

SCHEMA = "m1_run/1"
PINNED_RISK_SHA256 = "0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04"
MANIFEST = "manifest.json"
STREAMS = ("books", "events", "rest")

EXIT_OK = 0
EXIT_NO_ROWS = 1
EXIT_USAGE = 2
EXIT_NO_WEBSOCKETS = 3
EXIT_WRITE_FAILED = 4
EXIT_INTERRUPTED = 5
EXIT_PIN_DRIFT = 6  # Task AS: config/risk.toml off its pins at start or while running (global halt recorded)


def utc_now_ms() -> str:
    """ISO 8601 UTC with milliseconds and Z (finer than podlib.utc_now, for the run timeline)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# --- asset selection ---------------------------------------------------------------------------


def _usable_tokens(row: dict[str, Any]) -> list[str] | None:
    tokens = row.get("token_ids")
    if row.get("token_ids_problem") or not isinstance(row.get("version"), str) or not row.get("version"):
        return None
    if not (isinstance(tokens, list) and len(tokens) == 2 and all(isinstance(t, str) and t.strip() == t and t for t in tokens)):
        return None
    if tokens[0] == tokens[1]:
        return None
    return list(tokens)


def select_assets(rows: list[dict[str, Any]], top: int) -> dict[str, Any]:
    """Pick the first ``top`` usable markets of the latest scanner batch (file order = 24h volume desc).

    Returns {"assets", "meta", "markets", "scan_ts", "skipped"}; assets is empty when nothing is usable.
    """
    stamped = [(parse_book_ts(row.get("ts")), index, row) for index, row in enumerate(rows) if isinstance(row, dict)]
    stamped = [item for item in stamped if item[0] is not None]
    skipped: Counter = Counter()
    if not stamped:
        return {"assets": [], "meta": {}, "markets": [], "scan_ts": None, "skipped": {"no_timestamped_rows": 1}}
    latest = max(stamp for stamp, _, _ in stamped)
    batch = [row for stamp, _, row in stamped if stamp == latest]
    chosen: list[dict[str, Any]] = []
    seen_markets: set[str] = set()
    seen_tokens: set[str] = set()
    for row in batch:
        market_id = str(row.get("id") or "")
        tokens = _usable_tokens(row)
        if not market_id:
            skipped["missing_market_id"] += 1
        elif market_id in seen_markets:
            skipped["duplicate_market"] += 1
        elif tokens is None:
            skipped[str(row.get("token_ids_problem") or "unusable_token_ids")] += 1
        elif seen_tokens & set(tokens):
            skipped["token_in_two_markets"] += 1
        else:
            chosen.append(row)
            seen_markets.add(market_id)
            seen_tokens.update(tokens)
        if len(chosen) >= top:
            break
    meta = asset_meta(chosen)
    assets = [token for row in chosen for token in _usable_tokens(row) or []]
    markets = [{"market_id": str(row.get("id")), "question": row.get("question") or "", "version": row.get("version"),
                "token_id_source": row.get("token_id_source"), "token_ids": _usable_tokens(row),
                "outcomes": row.get("outcomes") or [], "volume_24h_usd": row.get("volume_24h_usd")} for row in chosen]
    return {"assets": assets, "meta": meta, "markets": markets, "scan_ts": str(batch[0].get("ts")),
            "skipped": dict(skipped)}


# --- REST /book sampler ------------------------------------------------------------------------


class RestSampler:
    """Every ``every_s`` (and once at start) read the public CLOB /book for ``assets`` via book.snapshot."""

    def __init__(self, assets: list[str], meta: dict[str, dict[str, Any]], sink: Any, events: Any, *,
                 every_s: float, deadline_s: float, getter: Any = None, now: Callable[[], str] = utc_now_ms,
                 monotonic: Callable[[], float] = time.monotonic, heartbeat: Any = None) -> None:
        self.assets = list(assets)
        self.meta = meta
        self.sink = sink
        self.events = events
        self.every_s = float(every_s)
        self.deadline_s = float(deadline_s)
        self.getter = getter
        self.now = now
        self.monotonic = monotonic
        self.samples = 0
        self.rows = 0
        self.errors = 0
        self.failure: BaseException | None = None
        self.heartbeat = heartbeat  # Task AP: one m1_rest beat after every sample

    def sample_once(self) -> None:
        self.samples += 1
        for token in self.assets:
            info = self.meta.get(token, {})
            market = {"id": info.get("market_id", ""), "question": info.get("question", ""),
                      "condition_id": info.get("condition_id", ""), "liquidity_usd": info.get("liquidity_usd")}
            requested = self.now()
            row = rest_snapshot(market, info.get("outcome", ""), token, requested, getter=self.getter)
            if row is None:
                self.errors += 1
                self.events.write({"ts": self.now(), "event": "rest_error", "asset": token, "sample": self.samples})
                continue
            row["sample"] = self.samples
            row["received_at"] = self.now()
            self.sink.write(row)
            self.rows += 1
        if self.heartbeat is not None:
            self.heartbeat.beat("running", {"samples": self.samples, "rows": self.rows, "errors": self.errors},
                                force=True)

    def run(self, stop: threading.Event, wait: Callable[[float], bool] | None = None) -> None:
        """Sample now, then every every_s until stop or the deadline. ``wait(seconds)`` returns True on stop."""
        wait = wait or stop.wait
        start = self.monotonic()
        try:
            self.sample_once()
            next_at = start + self.every_s
            while not stop.is_set():
                current = self.monotonic()
                if current - start >= self.deadline_s:
                    return
                if current >= next_at:
                    self.sample_once()
                    while next_at <= current:
                        next_at += self.every_s  # a slow sample skips missed slots, never bursts
                    continue
                if wait(min(next_at, start + self.deadline_s) - current):
                    return
        except BaseException as exc:  # noqa: BLE001 - reported by main (write failure -> exit 4)
            self.failure = exc


# --- manifest ----------------------------------------------------------------------------------


def risk_state(path: Path = RISK_PATH) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return {"sha256": None, "live_enabled": None, "error": type(exc).__name__}
    sha = hashlib.sha256(raw.replace(b"\r\n", b"\n")).hexdigest()
    try:
        live = parse_risk_toml(raw.decode("utf-8")).get("live_enabled")
    except Exception:  # noqa: BLE001 - recorded, the report fails it
        live = None
    return {"sha256": sha, "live_enabled": live}


def git_state(root: Path = ROOT) -> dict[str, Any]:
    def git(*args: str) -> str | None:
        try:
            done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.decode("utf-8", "replace").strip() if done.returncode == 0 else None
    sha = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=no") if sha else None
    return {"sha": sha, "dirty": None if status is None else bool(status)}


def websockets_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("websockets")
    except Exception:  # noqa: BLE001 - PackageNotFoundError or a broken metadata install
        return None


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    """Atomic: temp file in the run dir, fsync, os.replace."""
    path = run_dir / MANIFEST
    temp = run_dir / f".{MANIFEST}.tmp"
    data = (json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
    with open(temp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _threshold_values() -> dict[str, Any]:
    return {name: value for name, value in vars(thresholds).items()
            if name.isupper() and not name.startswith("_") and isinstance(value, (int, float))}


# --- CLI ---------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.m1_run",
        description="M1 24-hour acceptance run: read-only WS book recording + REST /book samples (no orders).",
        epilog="Exit codes: 0 ran to --max-runtime with at least one snapshot; 1 no snapshot; 2 usage error; "
               "3 websockets missing (pip install -r requirements-ws.txt); 4 write failed; 5 stopped by SIGINT/SIGTERM; "
               "6 config/risk.toml off its pins (global halt recorded). See docs/EXIT_CODES.md.",
    )
    parser.add_argument("--out", required=True, type=Path, help="new or empty run directory")
    parser.add_argument("--max-runtime", required=True, type=float, help="seconds (86400 for the 24 h acceptance run)")
    parser.add_argument("--markets", type=Path, default=MARKETS_PATH, help="scanner output (default data/markets.jsonl)")
    parser.add_argument("--top", type=int, default=M1_TOP_MARKETS, help=f"markets from the latest scan (default {M1_TOP_MARKETS})")
    parser.add_argument("--assets", nargs="+", default=None, help="token ids instead of --top (comma or space separated)")
    parser.add_argument("--rest-every", type=float, default=M1_REST_EVERY_S, help=f"seconds between REST samples (default {M1_REST_EVERY_S:g})")
    parser.add_argument("--rest-assets", type=int, default=M1_REST_ASSETS, help=f"assets per REST sample (default {M1_REST_ASSETS})")
    parser.add_argument("--rotate-mb", type=float, default=M1_ROTATE_BYTES / 1048576, help="rotate a file before it passes this many MiB (default 64)")
    parser.add_argument("--rotate-s", type=float, default=M1_ROTATE_S, help=f"rotate a file after this many seconds (default {M1_ROTATE_S:g})")
    parser.add_argument("--raw-books", action="store_true",
                        help="also save every raw book frame to raw_books/ for python -m src.ws_hash_check "
                             f"(off by default; {WS_RAW_BOOK_FILE_MAX_BYTES // 1048576} MiB per file, "
                             f"{WS_RAW_BOOK_RUN_MAX_BYTES // 1048576} MiB per run)")
    return parser


def _usage(message: str) -> int:
    print(f"m1_run: usage error: {message}", file=sys.stderr)
    return EXIT_USAGE


def main(argv: list[str] | None = None, *, transport: Transport | None = None, rest_getter: Any = None,
         monotonic: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None,
         now: Callable[[], str] | None = None, stop_event: threading.Event | None = None,
         install_signals: bool = True, git: Callable[[], dict] = git_state) -> int:
    """Task AP (Sentinel design gap 2): logs/heartbeat/m1_run.json is written by the recorder loop every
    HEARTBEAT_RECORDER_EVERY_S (also while backing off) and logs/heartbeat/m1_rest.json after every REST
    sample; both end with a beat carrying the exit code. For the resident Sentinel (src/sentinel_tick.py):
    m1_run exit 4 is a global halt, 1/2/3 an alert, 5 (Operator stop) only logged; a lost m1_run
    heartbeat (HEARTBEAT_RECORDER_TIMEOUT_S) is a global halt, a lost m1_rest one
    (HEARTBEAT_REST_TIMEOUT_S) an alert."""
    from src.heartbeat import Heartbeat  # local: keeps the module import light

    hb = Heartbeat("m1_run", every_s=thresholds.HEARTBEAT_RECORDER_EVERY_S)
    rest_hb = Heartbeat("m1_rest", every_s=thresholds.HEARTBEAT_RECORDER_EVERY_S)
    hb.beat("starting", force=True)
    try:
        code = _main(argv, transport=transport, rest_getter=rest_getter, monotonic=monotonic, sleep=sleep, now=now,
                     stop_event=stop_event, install_signals=install_signals, git=git, heartbeat=hb,
                     rest_heartbeat=rest_hb)
    except BaseException:
        hb.finish(None, crashed=True)
        if rest_hb.seq:
            rest_hb.finish(None, crashed=True)
        raise
    hb.finish(code)
    if rest_hb.seq:
        rest_hb.finish(0 if code in (EXIT_OK, EXIT_NO_ROWS, EXIT_INTERRUPTED) else code)
    return code


def _main(argv: list[str] | None, *, transport: Transport | None, rest_getter: Any,
          monotonic: Callable[[], float] | None, sleep: Callable[[float], None] | None,
          now: Callable[[], str] | None, stop_event: threading.Event | None,
          install_signals: bool, git: Callable[[], dict], heartbeat: Any = None, rest_heartbeat: Any = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    numbers_ok = (args.max_runtime == args.max_runtime and 0 < args.max_runtime < float("inf")
                  and args.top >= 1 and args.rest_assets >= 0 and 0 < args.rest_every < float("inf")
                  and 0 < args.rotate_mb < float("inf") and 0 < args.rotate_s < float("inf"))
    if not numbers_ok:
        return _usage("--max-runtime, --top, --rest-every, --rotate-mb and --rotate-s must be > 0; --rest-assets >= 0")
    run_dir: Path = args.out
    if run_dir.exists() and (not run_dir.is_dir() or any(run_dir.iterdir())):
        return _usage(f"--out {run_dir} exists and is not an empty directory (a run never mixes with another)")
    rows = read_jsonl(args.markets)
    selection = select_assets(rows, args.top)
    if args.assets:
        try:
            assets = validate_assets([part for item in args.assets for part in item.split(",") if part])
        except ValueError as exc:
            return _usage(str(exc))
        meta = asset_meta(rows)
    else:
        assets, meta = selection["assets"], selection["meta"]
    if not assets:
        return _usage(f"no usable assets in {args.markets}; run python -m src.scanner first or pass --assets")

    if transport is None and importlib.util.find_spec("websockets") is None:
        # Checked before the run dir is created; the package itself is only imported by the adapter.
        print("m1_run: the optional websockets package is not installed: pip install -r requirements-ws.txt",
              file=sys.stderr)
        return EXIT_NO_WEBSOCKETS
    from src.pins import PinWatch, refuse_start  # Task AS (gap 5): runtime pin checks

    if refuse_start("m1_run"):
        return EXIT_PIN_DRIFT  # before the run dir exists: nothing recorded
    now = now or utc_now_ms
    monotonic = monotonic or time.monotonic
    stop = stop_event or threading.Event()
    watch = PinWatch("m1_run", monotonic=monotonic)

    def should_stop() -> bool:
        if watch.drifted():
            stop.set()  # the REST sampler stops too
            return True
        return stop.is_set()

    run_dir.mkdir(parents=True, exist_ok=True)
    rotate_bytes = max(1, int(args.rotate_mb * 1048576))
    sinks = {name: RotatingJsonl(run_dir / name, name, rotate_bytes, args.rotate_s, monotonic) for name in STREAMS}
    raw_sink = (RawBookSink(run_dir / RAW_BOOKS, min(rotate_bytes, WS_RAW_BOOK_FILE_MAX_BYTES), WS_RAW_BOOK_RUN_MAX_BYTES,
                            args.rotate_s, monotonic, on_event=sinks["events"].write, now=now)
                if args.raw_books else None)
    rest_assets = assets[:args.rest_assets]
    manifest: dict[str, Any] = {
        "schema": SCHEMA,
        "status": "running",
        "started_at": now(),
        "ended_at": None,
        "elapsed_s": None,
        "max_runtime_s": args.max_runtime,
        "assets": [{"token_id": token, **meta.get(token, {"market_id": "", "question": "", "outcome": ""})}
                   for token in assets],
        "rest_assets": rest_assets,
        "selection": {"markets_path": str(args.markets), "scan_ts": selection["scan_ts"], "top": args.top,
                      "explicit_assets": bool(args.assets), "markets": selection["markets"] if not args.assets else [],
                      "skipped": selection["skipped"]},
        "settings": {"rest_every_s": args.rest_every, "rest_assets": args.rest_assets,
                     "rotate_bytes": rotate_bytes, "rotate_s": args.rotate_s, "raw_books": bool(args.raw_books)},
        "thresholds": _threshold_values(),
        "version": {"schema": SCHEMA, "python": platform.python_version(), "websockets": websockets_version()},
        "git": git(),
        "risk": {"pinned_sha256": PINNED_RISK_SHA256, "start": risk_state(), "end": None},
        "files": {},
        "counts": {},
        "exit_code": None,
        "stop_signal": None,
        "orders": "none",
    }
    try:
        write_manifest(run_dir, manifest)
    except OSError as exc:
        print(f"m1_run: writing the manifest failed: {exc}", file=sys.stderr)
        return EXIT_WRITE_FAILED

    previous: dict[int, Any] = {}

    def on_signal(signum: int, frame: Any) -> None:  # noqa: ARG001
        manifest["stop_signal"] = signal.Signals(signum).name
        stop.set()

    if install_signals and threading.current_thread() is threading.main_thread():
        for name in ("SIGINT", "SIGTERM"):
            if hasattr(signal, name):
                signum = getattr(signal, name)
                previous[signum] = signal.signal(signum, on_signal)

    events = sinks["events"]
    events.write({"ts": now(), "event": "start", "assets": assets, "rest_assets": rest_assets})
    sampler = RestSampler(rest_assets, meta, sinks["rest"], events, every_s=args.rest_every,
                          deadline_s=args.max_runtime, getter=rest_getter, now=now, heartbeat=rest_heartbeat)
    thread = threading.Thread(target=sampler.run, args=(stop,), name="m1-rest-sampler", daemon=True)
    recorder = Recorder(assets, transport=transport or WebsocketsTransport(), sink=sinks["books"],
                        max_runtime_s=args.max_runtime, meta=meta, monotonic=monotonic, sleep=sleep, now=now,
                        stop=should_stop, on_event=events.write, heartbeat=heartbeat,
                        raw_books=raw_sink.write if raw_sink is not None else None)
    start_mono = monotonic()
    code = EXIT_OK
    status = "completed"
    report = recorder.report
    if rest_assets:
        thread.start()
    try:
        report = recorder.run()
    except TransportUnavailable as exc:
        print(str(exc), file=sys.stderr)
        code, status = EXIT_NO_WEBSOCKETS, "failed"
    except OSError as exc:
        print(f"m1_run: writing {run_dir} failed: {exc}", file=sys.stderr)
        code, status = EXIT_WRITE_FAILED, "failed"
    except BaseException:
        status = "failed"
        raise
    finally:
        stopped_by_signal = stop.is_set()
        stop.set()
        if thread.is_alive():
            thread.join(timeout=120)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        if sampler.failure is not None and code == EXIT_OK:
            print(f"m1_run: REST sampler failed: {sampler.failure!r}", file=sys.stderr)
            code, status = EXIT_WRITE_FAILED, "failed"
        if code == EXIT_OK and watch.problems:
            code, status = EXIT_PIN_DRIFT, "pin_drift"
        if code == EXIT_OK and stopped_by_signal:
            code, status = EXIT_INTERRUPTED, "interrupted"
        if code == EXIT_OK and report.counts["snapshot_applied"] < 1:
            code, status = EXIT_NO_ROWS, "no_snapshot"
        try:
            events.write({"ts": now(), "event": "finish", "status": status, "exit_code": code})
        except OSError:
            if code == EXIT_OK:
                code, status = EXIT_WRITE_FAILED, "failed"
        for sink in [*sinks.values(), *([raw_sink] if raw_sink is not None else [])]:
            try:
                sink.close()
            except OSError:
                code, status = (EXIT_WRITE_FAILED, "failed") if code == EXIT_OK else (code, status)
        manifest.update({
            "status": status,
            "ended_at": now(),
            "elapsed_s": round(monotonic() - start_mono, 3),
            "files": {name: sink.files for name, sink in sinks.items()},
            **({"raw_books": {"files": raw_sink.files.files, "saved": raw_sink.saved, "bytes": raw_sink.bytes,
                              "dropped": dict(raw_sink.dropped), "file_max_bytes": raw_sink.files.max_bytes,
                              "run_max_bytes": raw_sink.run_max_bytes}} if raw_sink is not None else {}),
            "counts": {
                "book_rows": report.rows, "events": events.rows, "rest_rows": sampler.rows,
                "rest_samples": sampler.samples, "rest_errors": sampler.errors,
                "connects": report.connects, "connect_failures": report.connect_failures,
                "disconnects": dict(report.disconnects), "pings": report.pings, "ended_by": report.ended_by,
                "snapshots": report.counts["snapshot_applied"],
                "price_changes": report.counts["price_change_applied"],
                "resyncs": {key.split(":", 1)[1]: value for key, value in sorted(report.counts.items())
                            if key.startswith("resync:")},
                "unmapped_assets": report.unmapped_assets,
            },
            "exit_code": code,
        })
        manifest["risk"]["end"] = risk_state()
        try:
            write_manifest(run_dir, manifest)
        except OSError as exc:
            print(f"m1_run: writing the manifest failed: {exc}", file=sys.stderr)
            code = EXIT_WRITE_FAILED
    print(f"m1_run status={manifest['status']} exit={code} assets={len(assets)} book_rows={report.rows} "
          f"rest_rows={sampler.rows} connects={report.connects} run_dir={run_dir} orders=none")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
