"""Phase 0 shared helpers. Standard library only. No secrets. No orders."""
from __future__ import annotations

import http.client
import json
import math
import socket
import ssl
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPHandler, HTTPSHandler, ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
RISK_PATH = ROOT / "config" / "risk.toml"
BUS_PATH = LOG_DIR / "bus.jsonl"
PAPER_PATH = LOG_DIR / "paper.jsonl"
SESSIONS_PATH = LOG_DIR / "sessions.jsonl"
MARKETS_PATH = DATA_DIR / "markets.jsonl"
BOOKS_PATH = DATA_DIR / "books.jsonl"
RESOLUTIONS_PATH = DATA_DIR / "resolutions.jsonl"
# Task AX (M3 step 2): one sealed NewsForecaster forecast per row (src/calibration_ids.py).
FORECASTS_PATH = LOG_DIR / "forecasts.jsonl"
QUOTES_PATH = LOG_DIR / "quotes.jsonl"
# Sentinel StaleWatch streaks between paper_cycle runs (Task AA). Not a risk.toml key.
SENTINEL_STATE_PATH = LOG_DIR / "sentinel_stale.json"

# Local paper bankroll. Not a wallet. Sizing never exceeds the 1% clip.
from src.thresholds import PAPER_EQUITY_USD  # noqa: E402 - Task AC: value in src/thresholds.py
from src.thresholds import PUBLIC_GET_DEADLINE_S  # noqa: E402,F401 - Task DQ: re-export, value in src/thresholds.py
from src.secret_scan import check_message as _secret_check  # noqa: E402 - Task AQ: bus secret filter
BUS_TYPES = frozenset({"signal", "veto", "fill", "proposal", "halt", "resume"})
# Task AL: ``resume`` lifts one halt. Only the Operator (the human, via ``python -m src.halt_store
# resume``) may send it, and "Operator" may send nothing else: it is not a bus role otherwise.
RESUME_FROM = "Operator"
RESUME_SCOPES = frozenset({"global", "market"})
# Bus role names follow docs/ROSTER.md. A legacy name is still accepted by bus() and written
# as its ROSTER name; readers of older logs/bus.jsonl lines compare via canonical_role().
ROLE_ALIASES = {"Forecaster": "NewsForecaster"}

# Ledger refuses to run if config/risk.toml drifts off these pins.
PINS = {
    "mode": "paper",
    "live_enabled": False,
    "require_human_gate": True,
    "max_order_pct": 0.01,
    "max_daily_loss_pct": 0.03,
}

GAMMA_PREFIX = "https://gamma-api.polymarket.com/markets"
BOOK_PREFIX = "https://clob.polymarket.com/book?"
# Independent public server clock: exact URL, no query or sub-path.
TIME_URL = "https://clob.polymarket.com/time"
# Task AK: the Data API v2 reads the Reconciler needs (plus DATA_API_STATUS_URL below) (v1 is retired 2026-10-24). Exact routes
# with their "?" so /v2/positions/combos and every other v2 route stay refused.
DATA_API_POSITIONS_PREFIX = "https://data-api.polymarket.com/v2/positions?"
DATA_API_TRADES_PREFIX = "https://data-api.polymarket.com/v2/trades?"
# Data freshness, read first on every reconciliation round. Exact URL (no query, no sub-path).
DATA_API_STATUS_URL = "https://data-api.polymarket.com/v2/status"
PUBLIC_PREFIXES = (GAMMA_PREFIX, BOOK_PREFIX, DATA_API_POSITIONS_PREFIX, DATA_API_TRADES_PREFIX)

# public_get retries transient network/TLS failures (SSL EOF, resets, timeouts, 5xx) and HTTP 429.
# Waits are PUBLIC_GET_BACKOFF_S * 2**n between attempts (0.5 s, 1 s). Task DP (#199): a 429 waits its
# Retry-After (integer seconds, at most PUBLIC_GET_RETRY_AFTER_MAX_S) instead, falling back to the backoff when
# the header is missing or not an integer. Every other HTTP 4xx is never retried. All share PUBLIC_GET_ATTEMPTS.
PUBLIC_GET_ATTEMPTS = 3
PUBLIC_GET_BACKOFF_S = 0.5
PUBLIC_GET_RETRY_AFTER_MAX_S = 30.0
HTTP_TOO_MANY_REQUESTS = 429
# Task DQ (Poly哨兵 on #309): one public_get call (every attempt, timeout and wait together) never runs past
# PUBLIC_GET_DEADLINE_S (src/thresholds.py). Each attempt's socket timeout is clipped to the time left, and a
# wait that would end past the deadline is not taken: PublicGetError at once. CLOB /time (TIME_URL) is never
# retried on 429: a retried /time widens the sent/recv interval past the clock tolerance anyway.
# Task DU (#316): the deadline is a hard wall-clock cap on one call, connect, headers and body read included.
# Each attempt runs in a short-lived worker thread that the caller waits for at most the time left; the worker
# reads the body in chunks of at most _READ_CHUNK_BYTES, re-clips the socket timeout to the time left before
# every read and stops once the deadline has passed. Past the deadline: PublicGetError, never partial data.
_READ_CHUNK_BYTES = 65536
_sleep = time.sleep  # tests patch this so they never sleep
_monotonic = time.monotonic  # tests patch this with a fake clock

# OSError covers ConnectionResetError, socket.timeout/TimeoutError and ssl.SSLError (incl. SSLEOFError).
TRANSIENT_NETWORK_ERRORS = (URLError, OSError, http.client.HTTPException, ssl.SSLError, socket.timeout)


class PublicGetError(RuntimeError):
    """A public read still failed on a transient network/TLS error, 5xx or 429 after every retry."""

    def __init__(self, url: str, attempts: int, last_error: BaseException) -> None:
        self.url = url
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(
            f"public GET failed after {attempts} attempts: {type(last_error).__name__}: {last_error} ({url})"
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_ms() -> str:
    """UTC now with milliseconds and Z (``2026-10-10T12:59:04.634Z``), the precision of a CLOB book_ts.

    Issue #128: a clock that compares itself with a millisecond book_ts must not be cut to whole seconds,
    or a book stamped within the current second reads as from the future (future_book_ts). Truncated to
    the millisecond, never rounded up, so it is never later than the real clock. utc_now() (seconds) stays
    for row and log stamps.
    """
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_risk_toml(text: str) -> dict[str, Any]:
    """Parse the flat key = value file. Not a general TOML parser."""
    parsed: dict[str, Any] = {}
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"risk.toml:{lineno}: expected key = value")
        key, raw_value = (part.strip() for part in line.split("=", 1))
        if not key:
            raise ValueError(f"risk.toml:{lineno}: empty key")
        parsed[key] = _parse_scalar(raw_value, lineno)
    return parsed


def _parse_scalar(value: str, lineno: int) -> Any:
    if value == "true":
        return True
    if value == "false":
        return False
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    try:
        if any(ch in value for ch in ".eE"):
            return float(value)
        return int(value)
    except ValueError as exc:
        raise ValueError(f"risk.toml:{lineno}: unsupported value") from exc


def load_risk() -> dict[str, Any]:
    text = RISK_PATH.read_text(encoding="utf-8")
    cfg = parse_risk_toml(text)
    missing = [key for key in PINS if key not in cfg]
    if missing:
        raise RuntimeError("risk.toml missing required keys: " + ", ".join(missing))
    return cfg


def pin_problems(cfg: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    for key, expected in PINS.items():
        if cfg.get(key) != expected:
            problems.append(f"{key}={cfg.get(key)!r} expected {expected!r}")
    return problems


class AppendLockError(OSError):
    """Issue #162: the ``<file>.lock`` mutex was not taken in time. An OSError, so every caller's
    existing write-failure path (halt / exit code) handles it: the write fails closed, never unguarded."""


@contextmanager
def append_lock(path: Path):
    """Issue #162: the cross-process mutex for a JSONL file several processes append to (bus, quotes,
    forecasts, proposals): halt_store's StateLock (msvcrt / flock byte lock on ``<file>.lock``, a file
    that is never replaced) with its existing HALT_STATE_LOCK_TIMEOUT_S. Not re-entrant: never nest it
    on the same file. Raises AppendLockError when the lock is not taken."""
    from src.halt_store import StateLock  # local: halt_store imports this module

    lock = StateLock(Path(path))
    if not lock.acquire():
        raise AppendLockError(f"append lock {lock.path} not taken; nothing written")
    try:
        yield
    finally:
        lock.release()


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    """Append one JSON line under ``<file>.lock`` (issue #162). Binary mode with an explicit ``\\n`` so a
    line is the same bytes on every OS. Raises OSError (including AppendLockError): nothing is written."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    with append_lock(path):
        with path.open("ab") as handle:
            handle.write(payload)
            handle.flush()


def canonical_role(name: str) -> str:
    """Return the ROSTER bus name for a legacy role name; any other name is returned unchanged."""
    return ROLE_ALIASES.get(name, name)


def bus(
    frm: str,
    to: str,
    kind: str,
    market: str,
    payload: dict[str, Any],
    path: Path | None = None,
) -> dict[str, Any]:
    frm = canonical_role(frm)
    to = canonical_role(to)
    if kind not in BUS_TYPES:
        raise RuntimeError(f"bus type not allowed: {kind}")
    if kind == "fill" and frm != "Executor":
        raise RuntimeError("only Executor may emit fills")
    if kind == "fill" and payload.get("risk_decision") != "allow":
        raise RuntimeError("fill refused without RiskGate allow")
    if kind == "resume" or frm == RESUME_FROM:
        _check_resume(frm, to, kind, payload)
    message = {
        "ts": utc_now(),
        "from": frm,
        "to": to,
        "type": kind,
        "market": market,
        "payload": payload,
    }
    # Task AQ (Sentinel design gap 9): a message holding a private key, mnemonic or credential value
    # is never written; a global halt is recorded and SecretRefused raised (nothing echoed).
    _secret_check(message)
    append_jsonl(path or BUS_PATH, message)
    return message


def _check_resume(frm: str, to: str, kind: str, payload: dict[str, Any]) -> None:
    """Task AL: a resume is from=Operator to=Sentinel, names one halt and who approved it."""
    if kind != "resume":
        raise RuntimeError("Operator is not a bus role except as the sender of resume")
    if frm != RESUME_FROM:
        raise RuntimeError("only Operator may emit resume")
    if to != "Sentinel":
        raise RuntimeError("resume goes to Sentinel")
    if not isinstance(payload, dict):
        raise RuntimeError("resume payload must be a dict")
    approved = payload.get("approved_by")
    if not isinstance(approved, str) or not approved.strip():
        raise RuntimeError("resume needs approved_by")
    if payload.get("scope") not in RESUME_SCOPES:
        raise RuntimeError("resume scope must be global or market")
    ref = payload.get("halt_ref")
    if not isinstance(ref, dict) or not isinstance(ref.get("id"), str) or not ref.get("id"):
        raise RuntimeError("resume needs halt_ref with an id")
    if ref.get("scope") == "global" and payload.get("scope") != "global":
        raise RuntimeError("a market resume cannot lift a global halt")
    if payload.get("exchange_action", "none") != "none":
        raise RuntimeError("resume carries no exchange action")


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, _BodyTooLarge):
        return False
    if isinstance(exc, HTTPError):
        return exc.code >= 500 or exc.code == HTTP_TOO_MANY_REQUESTS
    return isinstance(exc, TRANSIENT_NETWORK_ERRORS)


def _retry_after_s(exc: BaseException) -> float | None:
    """Task DP: a 429's Retry-After as integer seconds, capped at PUBLIC_GET_RETRY_AFTER_MAX_S.

    None (use the exponential backoff) when exc is not a 429 or the header is missing, an HTTP-date, negative,
    fractional or anything else that is not plain ASCII digits.
    """
    if not isinstance(exc, HTTPError) or exc.code != HTTP_TOO_MANY_REQUESTS:
        return None
    try:
        raw = exc.headers.get("Retry-After") if exc.headers is not None else None
    except Exception:  # noqa: BLE001 - odd header objects fall back to the backoff
        return None
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not (text.isascii() and text.isdigit()):
        return None
    return min(float(int(text)), PUBLIC_GET_RETRY_AFTER_MAX_S)


class _DeadlineExceeded(TimeoutError):
    """Task DU: one public_get call passed its deadline. Never retried: public_get raises PublicGetError."""


def _check_deadline_s(deadline_s: Any) -> float:
    """Task DU (#316, Codex P2 on #314): the deadline must be a finite number > 0 (int or float, not bool)."""
    if isinstance(deadline_s, bool) or not isinstance(deadline_s, (int, float)):
        raise ValueError(f"public_get deadline_s must be a finite number > 0, got {deadline_s!r}")
    budget = float(deadline_s)
    if not math.isfinite(budget) or budget <= 0:
        raise ValueError(f"public_get deadline_s must be a finite number > 0, got {deadline_s!r}")
    return budget


def _time_left(started: float, budget: float) -> float:
    left = budget - (_monotonic() - started)
    if left <= 0:
        raise _DeadlineExceeded(f"public_get deadline {budget:g} s passed during the read")
    return left


def _clip_socket_timeout(response: Any, seconds: float) -> None:
    """Best effort: set the response socket's timeout so one recv cannot outlast the time left."""
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if sock is None:
        return
    try:
        sock.settimeout(max(seconds, 0.001))
    except (OSError, ValueError):
        pass


# Task DU (Codex 6168938541 P1, optional): a body larger than this is refused before json.loads, which bounds the
# parse time that runs after the deadline check. Internal safety cap, not a trading threshold.
_MAX_BODY_BYTES = 8 * 1024 * 1024  # 8 MiB


class _BodyTooLarge(http.client.HTTPException):
    """Task DU: the answer is larger than _MAX_BODY_BYTES. Not retried (a retry would get the same size)."""


def _read_body(response: Any, timeout: float, started: float, budget: float) -> bytes:
    """Task DU: read the body in chunks, checking the deadline before and after every chunk."""
    read1 = getattr(response, "read1", None)
    if read1 is None:  # a response without read1 (test doubles): one read, then the deadline check
        body = response.read()
        _time_left(started, budget)
        if len(body) > _MAX_BODY_BYTES:
            raise _BodyTooLarge(f"public_get answer larger than {_MAX_BODY_BYTES} bytes")
        return body
    announced = getattr(response, "length", None)  # http.client: the Content-Length still to read
    if isinstance(announced, int) and not isinstance(announced, bool) and announced > _MAX_BODY_BYTES:
        raise _BodyTooLarge(f"public_get Content-Length {announced} > {_MAX_BODY_BYTES} bytes")
    chunks = []
    total = 0
    while True:
        _clip_socket_timeout(response, min(timeout, _time_left(started, budget)))
        chunk = read1(_READ_CHUNK_BYTES)
        _time_left(started, budget)  # a chunk that arrived past the deadline is not used
        if not chunk:
            # Task DU (Codex P1 on #328): read1 returns b"" when the peer closes early. With Content-Length,
            # http.client still counts the missing bytes in .length; a short body fails closed like read() did.
            missing = getattr(response, "length", None)
            if isinstance(missing, int) and not isinstance(missing, bool) and missing > 0:
                raise http.client.IncompleteRead(b"".join(chunks), missing)
            return b"".join(chunks)
        total += len(chunk)
        if total > _MAX_BODY_BYTES:
            raise _BodyTooLarge(f"public_get answer larger than {_MAX_BODY_BYTES} bytes")
        chunks.append(chunk)


# Task DU (Codex P2 on #328): at most this many public_get worker threads exist at once in the process. A worker
# left behind by a deadline has its socket shut down (see _attempt), so it frees its slot promptly; this caps how
# many leftovers can exist at once. Internal implementation bound, not a trading threshold.
_MAX_WORKERS = 4


def _valid_max_workers(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 16


assert _valid_max_workers(_MAX_WORKERS), f"_MAX_WORKERS must be an int in 1..16, got {_MAX_WORKERS!r}"
_WORKER_SLOTS = threading.BoundedSemaphore(_MAX_WORKERS)

# Task DU (Codex 6167868817 P2): each worker registers the sockets its connections open, so the caller can shut
# them down at the deadline during the response-header stage too (not only the body read).
_WORKER_LOCAL = threading.local()


def _register_socket(sock: Any) -> None:
    registry = getattr(_WORKER_LOCAL, "registry", None)
    if registry is None or sock is None:
        return
    with registry["lock"]:
        registry["sockets"].append(sock)
        cancelled = registry["cancelled"]
    if cancelled:  # the deadline already passed while this socket was connecting
        _shutdown_quietly(sock)


def _shutdown_quietly(sock: Any) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (OSError, ValueError, AttributeError):
        pass


class _TrackSocketMixin:
    """Task DU (Codex 6168938541 P2): register the raw TCP socket as soon as it exists, before the proxy CONNECT
    tunnel and the TLS handshake, so a stall in either is cancelled at the deadline (shutting down the raw socket
    also ends a TLS read on it)."""

    def connect(self) -> None:
        # http.client sets self._create_connection per instance (socket.create_connection), so wrap it here.
        create = self._create_connection  # type: ignore[attr-defined]

        def create_and_register(*args: Any, **kwargs: Any) -> Any:
            sock = create(*args, **kwargs)
            _register_socket(sock)
            return sock

        self._create_connection = create_and_register  # type: ignore[attr-defined]
        try:
            super().connect()  # type: ignore[misc]
        finally:
            self._create_connection = create  # type: ignore[attr-defined]


class _TrackedHTTPConnection(_TrackSocketMixin, http.client.HTTPConnection):
    pass


class _TrackedHTTPSConnection(_TrackSocketMixin, http.client.HTTPSConnection):
    pass


class _TrackedHTTPHandler(HTTPHandler):
    def http_open(self, req):  # noqa: D102
        return self.do_open(_TrackedHTTPConnection, req)


class _TrackedHTTPSHandler(HTTPSHandler):
    def https_open(self, req):  # noqa: D102
        return self.do_open(_TrackedHTTPSConnection, req, context=self._context)


def _make_opener(proxies: dict[str, str] | None = None):
    handlers = [_TrackedHTTPHandler(), _TrackedHTTPSHandler()]
    if proxies is not None:
        handlers.insert(0, ProxyHandler(proxies))
    return build_opener(*handlers)


_OPENER = _make_opener()


def urlopen(request: Request, timeout: float):
    """urllib's urlopen, but every socket it connects is registered for deadline cancellation."""
    return _OPENER.open(request, timeout=timeout)


def _close_quietly(response: Any) -> None:
    """Shut the response's socket (unblocks a pending recv) and close the response. Never raises."""
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    if sock is not None:
        _shutdown_quietly(sock)
    try:
        response.close()
    except Exception:  # noqa: BLE001 - best effort cleanup
        pass


def _attempt(request: Request, timeout: float, started: float, budget: float) -> bytes:
    """Task DU: one GET (connect, headers, body) that returns or raises within the time left.

    The request runs in a daemon worker thread holding one of _MAX_WORKERS slots. Waiting for a slot counts
    toward the deadline (Codex 6167868817 P1): the time left is recomputed after the slot is taken, and if it
    is gone the slot is released and no request is sent. The caller waits at most that time left; past it,
    _DeadlineExceeded is raised at once and the worker is cancelled (Codex P2 on #328 and 6167868817 P2): every
    socket the worker connected is shut down (so a stall in the response headers or body ends now and the slot
    frees promptly), an open response is closed, and a worker still connecting has its socket shut down as soon
    as it exists (the connect itself is bounded by the socket timeout, which is <= the time left).
    """
    left = _time_left(started, budget)
    if not _WORKER_SLOTS.acquire(timeout=left):
        raise _DeadlineExceeded(f"public_get deadline {budget:g} s passed waiting for a free worker slot")
    left = budget - (_monotonic() - started)
    if left <= 0:
        _WORKER_SLOTS.release()
        raise _DeadlineExceeded(f"public_get deadline {budget:g} s passed waiting for a free worker slot")
    attempt_timeout = min(timeout, left)
    outcome: dict[str, Any] = {}
    done = threading.Event()
    registry: dict[str, Any] = {"lock": threading.Lock(), "sockets": [], "cancelled": False, "response": None}

    def work() -> None:
        _WORKER_LOCAL.registry = registry
        try:
            response = urlopen(request, timeout=attempt_timeout)
            with registry["lock"]:
                registry["response"] = response
                cancelled = registry["cancelled"]
            try:
                if cancelled:
                    return
                body = _read_body(response, attempt_timeout, started, budget)
                # Task DU (Codex 6168938541 P1): decode and parse inside the deadline-bound worker.
                outcome["data"] = json.loads(body.decode("utf-8"))
            finally:
                _close_quietly(response)
        except BaseException as exc:  # noqa: BLE001 - handed to the caller's thread below
            outcome["error"] = exc
        finally:
            _WORKER_LOCAL.registry = None
            done.set()
            _WORKER_SLOTS.release()

    try:
        threading.Thread(target=work, name="public_get", daemon=True).start()
    except BaseException:
        _WORKER_SLOTS.release()
        raise
    if not done.wait(left):
        with registry["lock"]:
            registry["cancelled"] = True
            sockets = list(registry["sockets"])
            response = registry["response"]
        for sock in sockets:
            _shutdown_quietly(sock)
        if response is not None:
            _close_quietly(response)
        raise _DeadlineExceeded(f"public_get deadline {budget:g} s passed before the answer was read")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["data"]


def public_get(
    url: str,
    timeout: float = 20.0,
    *,
    attempts: int | None = None,
    backoff_s: float | None = None,
    deadline_s: float | None = None,
) -> Any:
    """GET a public read endpoint. Rejects order and auth URLs.

    Transient network/TLS errors and HTTP 5xx are retried with exponential backoff. HTTP 429 is retried too,
    after waiting its Retry-After seconds (capped at PUBLIC_GET_RETRY_AFTER_MAX_S; exponential backoff when the
    header is missing or not an integer), except on CLOB /time (TIME_URL), where a 429 is not retried (Task DQ).
    All of them share the same attempt budget. Every other HTTP 4xx and JSON errors are raised at once.

    Task DQ: the whole call is bounded by ``deadline_s`` (default PUBLIC_GET_DEADLINE_S, about 30 s): each
    attempt's timeout is min(timeout, time left), and a retry whose wait would end past the deadline is not
    taken. If every attempt fails, the deadline is reached, or /time answers 429, raises PublicGetError.

    Task DU (#316): the deadline is a hard wall-clock cap, the connect, headers and body read included (a
    slow-drip answer fails closed with PublicGetError at the deadline, never partial data or a late OK).
    ``deadline_s`` must be a finite number > 0, else ValueError before any network call.
    """
    if not (url.startswith(PUBLIC_PREFIXES) or url in (DATA_API_STATUS_URL, TIME_URL)):
        raise RuntimeError("refusing URL outside public Gamma markets, CLOB /book /time and Data API v2 positions/trades/status")
    lowered = url.lower()
    banned = ("/order", "/auth", "api-key", "secret", "passphrase", "signature", "withdraw")
    if any(token in lowered for token in banned):
        raise RuntimeError("refusing authenticated or order endpoint")
    request = Request(
        url,
        headers={"User-Agent": "polymarket-pod-phase0", "Accept": "application/json"},
        method="GET",
    )
    tries = max(1, int(PUBLIC_GET_ATTEMPTS if attempts is None else attempts))
    base = PUBLIC_GET_BACKOFF_S if backoff_s is None else float(backoff_s)
    budget = _check_deadline_s(PUBLIC_GET_DEADLINE_S if deadline_s is None else deadline_s)
    started = _monotonic()
    for attempt in range(tries):
        left = budget - (_monotonic() - started)
        try:
            if left <= 0:
                raise TimeoutError(f"public_get deadline {budget:g} s reached before attempt {attempt + 1}")
            data = _attempt(request, float(timeout), started, budget)
            break
        except Exception as exc:  # noqa: BLE001 - filtered by _retryable
            if left <= 0:
                raise PublicGetError(url, attempt, exc) from exc
            if isinstance(exc, _BodyTooLarge):
                raise PublicGetError(url, attempt + 1, exc) from exc  # Task DU: over _MAX_BODY_BYTES, never retried
            if isinstance(exc, _DeadlineExceeded):
                raise PublicGetError(url, attempt + 1, exc) from exc  # Task DU: the deadline passed mid-request
            if not _retryable(exc):
                raise
            if attempt + 1 >= tries:
                raise PublicGetError(url, tries, exc) from exc
            if url == TIME_URL and isinstance(exc, HTTPError) and exc.code == HTTP_TOO_MANY_REQUESTS:
                raise PublicGetError(url, attempt + 1, exc) from exc  # Task DQ: /time never retries a 429
            wait = _retry_after_s(exc)
            wait = base * (2 ** attempt) if wait is None else wait
            if (_monotonic() - started) + wait >= budget:
                raise PublicGetError(url, attempt + 1, exc) from exc  # Task DQ: the wait would pass the deadline
            _sleep(wait)
    # Task DU (Codex P2 on #328): decoding and parsing count toward the deadline too; a parse that ends past it
    # is not returned as success.
    if _monotonic() - started >= budget:
        exc = _DeadlineExceeded(f"public_get deadline {budget:g} s passed while decoding the answer")
        raise PublicGetError(url, attempt + 1, exc) from exc
    return data


def json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


READ_JSONL_WARN_LINES = 10  # issue #162: how many bad line numbers a WARN lists (display only)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Every JSON-object line of ``path``. Issue #162: a line that is not JSON or not an object is still
    skipped (no strict mode), but never silently: one WARN on stderr names the file, the count and the
    first line numbers."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    bad: list[int] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            row = json.loads(text)
        except json.JSONDecodeError:
            bad.append(number)
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad.append(number)
    if bad:
        shown = ", ".join(str(n) for n in bad[:READ_JSONL_WARN_LINES])
        more = f", ... ({len(bad) - READ_JSONL_WARN_LINES} more)" if len(bad) > READ_JSONL_WARN_LINES else ""
        print(f"WARN read_jsonl {path}: {len(bad)} unreadable line(s) skipped (line {shown}{more})", file=sys.stderr)
    return rows
