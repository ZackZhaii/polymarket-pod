"""Issue #347 (Task FH): settlement books under the commit lock (Poly风控 spec A1-A5, A7, A8, A10, A11).

Before: the runner's settlement (m2_runner._settle_tick -> paper_ledger.apply_settlements) read, planned and booked
outside ``<PAPER_PATH>.commit.lock``, so a settlement row could land between another process's RiskGate check and its
book (commit_if_allowed holds that lock across load_state -> RiskGate -> book). Now the public entry takes the same
lock (``paper_ledger._commit_lock``), re-plans inside it (ledger, resolutions, blocked markets), re-reads the clock
there, and defers (no write, no halt) when the lock cannot be taken or the clock is on a later UTC day. The commit
path calls the lock-free kernel ``_apply_settlements_locked`` (no self-lock, A2).

Determinism: lock holders are subprocesses driven over stdin/stdout (ready/release handshake, never sleep-held); every
wait is bounded; a holder that fails to start raises RuntimeError (a real failure). Offline: no network, no keys, no
orders, every file in the test's folder.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import halt_store, ledger_store, m2_runner, paper_ledger
from src.fees import booked_fee_usd, ledger_fee
from src.podlib import load_risk
from src.risk_gate import Intent
from tests.test_runner_settlement import NOW, Gamma, _events, _no_book, _global_halt, _hold_loser, _settlements, _tick, env, resolved_row  # noqa: F401,E501

ROOT = Path(__file__).resolve().parents[1]
DAY1 = "2026-09-01T12:00:00Z"
LATE = "2026-09-01T23:59:59Z"                              # a caller stamp just before UTC midnight
NEXT = datetime(2026, 9, 2, 0, 0, 1, tzinfo=timezone.utc)  # the clock once the lock is finally held
RESOLUTION = {"condition_id": "0xc", "winning_token_id": "yes", "outcome_prices_final": [1.0, 0.0],
              "outcome_token_ids": ["yes", "no"], "closed_ts": "2026-08-29T00:00:00Z",
              "snapshot_ts": "2026-08-31T01:00:00Z", "status": "resolved", "uma_checked": True}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def led(tmp_path, monkeypatch):
    """One losing leg (cost 10) + one confirmed resolution: exactly one settlement row to book."""
    ledger, res = tmp_path / "paper.jsonl", tmp_path / "resolutions.jsonl"
    for name, value in {"PAPER_PATH": ledger, "QUOTES_PATH": tmp_path / "q.jsonl",
                        "MARK_BOOKS_PATH": tmp_path / "b.jsonl", "DAILY_MARKS_DIR": tmp_path / "dm",
                        "SETTLEMENT_RESOLUTIONS_PATH": res}.items():
        monkeypatch.setattr(paper_ledger, name, value)
    monkeypatch.setattr(paper_ledger, "bus", lambda *a, **k: None)
    monkeypatch.setattr(paper_ledger, "utc_now", lambda: DAY1)
    monkeypatch.setattr(halt_store, "HALT_STATE_PATH", tmp_path / "halt_state.json")
    monkeypatch.setattr(halt_store, "HALT_FALLBACK_PATH", tmp_path / "HALT")
    shares, price = 20.0, 0.5
    ledger_store.book(ledger, dict(
        fill_id="x347-0000", ts="2026-08-31T20:00:00Z", market="m", token_id="no", side="buy", shares=shares,
        notional_usd=shares * price, fee_rate=0.0, market_price=price, effective_price=price,
        fee_usd=booked_fee_usd(ledger_fee(0.0, price, price, shares * price, shares)),
        hypothetical=False, counts_toward_risk=True, risk_decision="allow"))
    res.write_text(json.dumps(RESOLUTION) + "\n", encoding="utf-8")
    return ledger


def _settlement_rows(path: Path) -> list[dict]:
    return [row for row in ledger_store.read_ledger(path)[1].rows if ledger_store.is_settlement(row)]


class Holder:
    """A second OS process holding ``<ledger>.commit.lock`` exactly as commit_if_allowed builds it.
    Protocol: the child prints 'LOCKED <sha>' once locked; on stdin 'release' prints 'RELEASING <sha>' and exits."""

    def __init__(self, ledger: Path):
        code = textwrap.dedent(f"""
            import hashlib, sys
            from pathlib import Path
            sys.path.insert(0, {str(ROOT)!r})
            from src import halt_store
            ledger = Path({str(ledger)!r})
            lock = halt_store.StateLock(ledger)
            lock.path = Path(str(ledger) + ".commit.lock")
            if not lock.acquire(timeout_s=5.0):
                print("NOLOCK", flush=True); sys.exit(2)
            print("LOCKED", hashlib.sha256(ledger.read_bytes()).hexdigest(), flush=True)
            sys.stdin.readline()
            print("RELEASING", hashlib.sha256(ledger.read_bytes()).hexdigest(), flush=True)
            lock.release()
        """)
        self.proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(ROOT), text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        word, _, self.locked_sha = self.proc.stdout.readline().strip().partition(" ")
        if word != "LOCKED":
            self.proc.kill()
            raise RuntimeError(f"holder did not take the commit lock: {word!r}")

    def release(self) -> str:
        out, _ = self.proc.communicate("release\n", timeout=30)
        word, _, sha = out.strip().partition(" ")
        if word != "RELEASING":
            raise RuntimeError(f"holder died: {out!r}")
        return sha

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)


def _in_thread(fn):
    box: dict = {}
    thread = threading.Thread(target=lambda: box.update(out=fn()), daemon=True)
    thread.start()
    return thread, box


def _join(thread, what="settlement"):
    thread.join(timeout=30)
    if thread.is_alive():
        raise RuntimeError(f"{what} hung past 30 s")


# ---------------------------------------------------------------- controls
def test_c1_without_a_holder_one_settlement_is_booked(led):
    out = paper_ledger.apply_settlements(DAY1)
    assert len(out["booked"]) == 1 and out["problems"] == [] and "deferred" not in out


def test_c2_the_holder_really_excludes_a_commit_lock_taker(led):
    holder = Holder(led)
    try:
        assert paper_ledger._commit_lock().acquire(timeout_s=0.2) is False
    finally:
        holder.release()


# ---------------------------------------------------------------- A1: the same lock
def test_a1_settlement_and_commit_use_the_same_lock_file(led):
    assert paper_ledger._commit_lock().path == Path(str(led) + ".commit.lock")
    assert paper_ledger._commit_lock().path != Path(str(led) + ".lock")  # not ledger_store's per-book lock


def test_a1_a5_cross_process_holder_defers_without_writing_or_halting(led, monkeypatch):
    monkeypatch.setattr(paper_ledger, "COMMIT_LOCK_TIMEOUT_S", 0.3)
    holder = Holder(led)
    try:
        thread, box = _in_thread(lambda: paper_ledger.apply_settlements(DAY1))
        _join(thread)
        out = box["out"]
        assert _sha(led) == holder.locked_sha
        assert out["booked"] == [] and out["problems"] == []
        assert out["deferred"] == paper_ledger.SETTLEMENT_LOCK_UNAVAILABLE == "settlement_deferred_commit_lock"
        assert not halt_store.active_halts()
    finally:
        holder.release()


def test_a1_same_process_second_fd_holding_the_lock_also_defers(led, monkeypatch):
    """flock excludes a second fd of the same PID: no PID shortcut around the lock."""
    monkeypatch.setattr(paper_ledger, "COMMIT_LOCK_TIMEOUT_S", 0.2)
    lock = paper_ledger._commit_lock()
    if not lock.acquire(timeout_s=1.0):
        raise RuntimeError("could not take the commit lock for the test")
    try:
        before = _sha(led)
        out = paper_ledger.apply_settlements(DAY1)
    finally:
        lock.release()
    assert _sha(led) == before and out["booked"] == [] and out["deferred"] == paper_ledger.SETTLEMENT_LOCK_UNAVAILABLE


# ---------------------------------------------------------------- A2: no self-lock on the commit path
def test_a2_commit_path_settles_inside_its_own_lock_without_waiting(led):
    intent = Intent(strategy="test", market="x", side="buy", model_prob=0.6, market_price=0.4,
                    assumed_slippage=0.005, fee_rate=0.0, liquidity_usd=50_000.0, spread=0.01, token_id="t",
                    book_ts=DAY1)
    started = time.monotonic()
    paper_ledger.commit_if_allowed(intent, load_risk())
    elapsed = time.monotonic() - started
    assert len(_settlement_rows(led)) == 1
    assert elapsed < paper_ledger.COMMIT_LOCK_TIMEOUT_S / 2, f"commit path waited {elapsed:.2f}s (self-lock?)"


def test_a2_the_kernel_books_while_the_caller_holds_the_lock(led):
    lock = paper_ledger._commit_lock()
    assert lock.acquire(timeout_s=1.0)
    try:
        out = paper_ledger._apply_settlements_locked(DAY1)
    finally:
        lock.release()
    assert len(out["booked"]) == 1 and out["problems"] == []


# ---------------------------------------------------------------- A3: wait, then re-plan inside the lock
def test_a3_holder_sees_an_unchanged_ledger_then_settlement_books_after_release(led):
    """Real COMMIT_LOCK_TIMEOUT_S: the settlement waits; the holder's sha at release equals its sha at acquire."""
    holder = Holder(led)
    try:
        thread, box = _in_thread(lambda: paper_ledger.apply_settlements(DAY1))
        thread.join(timeout=0.5)  # head start only; the verdict is the sha read by the holder at release
        sha_at_release = holder.release()
        _join(thread)
    finally:
        holder.kill()
    assert sha_at_release == holder.locked_sha
    assert len(box["out"]["booked"]) == 1 and len(_settlement_rows(led)) == 1


def test_a3_a_market_halt_added_while_waiting_is_honoured_inside_the_lock(led):
    """The caller's skip_markets is computed before the wait; the blocked set is recomputed inside the lock."""
    holder = Holder(led)
    try:
        thread, box = _in_thread(lambda: paper_ledger.apply_settlements(DAY1, skip_markets=frozenset()))
        thread.join(timeout=0.3)
        halt_store.record_halt(scope=halt_store.SCOPE_MARKET, market="m", reason="settlement_conflict:0xc",
                               source="RiskGate")
        holder.release()
        _join(thread)
    finally:
        holder.kill()
    out = box["out"]
    assert out["booked"] == [] and out["problems"] == [] and out.get("skipped_markets") == ["m"]
    assert _settlement_rows(led) == []


# ---------------------------------------------------------------- A4: the clock is read inside the lock
def test_a4_clock_on_the_next_utc_day_defers_and_books_nothing(led):
    before = _sha(led)
    out = paper_ledger.apply_settlements(LATE, clock=lambda: NEXT)
    assert _sha(led) == before and out["booked"] == [] and out["problems"] == []
    assert out["deferred"] == paper_ledger.SETTLEMENT_DAY_BOUNDARY


def test_a4_same_day_later_clock_restamps_the_row(led):
    later = datetime(2026, 9, 1, 12, 0, 5, tzinfo=timezone.utc)
    out = paper_ledger.apply_settlements(DAY1, clock=lambda: later)
    assert len(out["booked"]) == 1 and out["booked"][0]["ts"].startswith("2026-09-01T12:00:05")


def test_a4_clock_behind_the_caller_keeps_the_caller_time(led):
    earlier = datetime(2026, 9, 1, 11, 0, tzinfo=timezone.utc)
    out = paper_ledger.apply_settlements(DAY1, clock=lambda: earlier)
    assert len(out["booked"]) == 1 and out["booked"][0]["ts"].startswith("2026-09-01T12:00:00")


@pytest.mark.parametrize("clock", [lambda: (_ for _ in ()).throw(OSError("no clock")), lambda: "not a datetime",
                                   lambda: datetime(2026, 9, 1, 13, 0)])  # raises / wrong type / naive
def test_a4_an_unusable_clock_keeps_the_caller_time(led, clock):
    """Spec A4/A11: an unusable clock never stamps later than the caller and is not a deferral reason."""
    out = paper_ledger.apply_settlements(DAY1, clock=clock)
    assert len(out["booked"]) == 1 and out["booked"][0]["ts"].startswith("2026-09-01T12:00:00")
    assert "deferred" not in out


def test_a4_midnight_passes_while_another_process_holds_the_lock(led):
    """Table row 8: asked at 23:59:59, the holder releases after 00:00 -> defer (the clock flips on release)."""
    released = threading.Event()

    def clock():
        return NEXT if released.is_set() else datetime(2026, 9, 1, 23, 59, 59, tzinfo=timezone.utc)

    holder = Holder(led)
    try:
        thread, box = _in_thread(lambda: paper_ledger.apply_settlements(LATE, clock=clock))
        thread.join(timeout=0.5)
        released.set()
        sha_at_release = holder.release()
        _join(thread)
    finally:
        holder.kill()
    out = box["out"]
    assert sha_at_release == holder.locked_sha and _sha(led) == holder.locked_sha
    assert out["booked"] == [] and out["deferred"] == paper_ledger.SETTLEMENT_DAY_BOUNDARY


# ---------------------------------------------------------------- A5 on the runner: defer, no halt, retry
def test_a5_runner_tick_with_a_foreign_holder_defers_without_halt_then_settles(env, monkeypatch):
    monkeypatch.setattr(paper_ledger, "COMMIT_LOCK_TIMEOUT_S", 0.3)
    _hold_loser(env)
    holder = Holder(env["paper"])
    try:
        _tick(env, Gamma(resolved_row()))
        settled, halted = _settlements(env), _global_halt()
    finally:
        holder.release()
    assert settled == [] and not halted, f"runner settled under a foreign commit lock: {settled}"
    deferred = _events(env, "settlement_deferred")
    assert len(deferred) == 1 and deferred[0]["reason"] == paper_ledger.SETTLEMENT_LOCK_UNAVAILABLE
    assert _events(env, "settled") == []
    _tick(env, Gamma(resolved_row()))  # the lock is free: the next tick settles
    assert len(_settlements(env)) == 1 and len(_events(env, "settled")) == 1 and not _global_halt()


def test_a5_runner_passes_its_clock_a_next_day_clock_defers(env):
    """The runner hands its own clock to the settlement: a clock past UTC midnight defers (no halt)."""
    _hold_loser(env)
    now = NOW.replace(hour=23, minute=59, second=59)
    state = env["state"]
    state.monotonic = lambda: now.timestamp()
    state.clock = lambda: now + timedelta(seconds=2)
    m2_runner.iteration(state, now=now, getters=m2_runner.Getters(gamma=Gamma(resolved_row()),
                                                                  book=lambda url: _no_book(url, now)))
    assert _settlements(env) == [] and not _global_halt()
    assert [e["reason"] for e in _events(env, "settlement_deferred")] == [paper_ledger.SETTLEMENT_DAY_BOUNDARY]


# ---------------------------------------------------------------- A8: a crashed holder releases the lock
def test_a8_a_killed_holder_releases_the_lock_and_settlement_books(led):
    holder = Holder(led)
    holder.kill()
    out = paper_ledger.apply_settlements(DAY1)
    assert len(out["booked"]) == 1 and out["problems"] == []


# ---------------------------------------------------------------- A10: two real settlers -> exactly one row
SETTLER = textwrap.dedent("""
    import json, sys
    from pathlib import Path
    sys.path.insert(0, {root!r})
    from src import halt_store, paper_ledger
    base = Path({base!r})
    paper_ledger.PAPER_PATH = base / "paper.jsonl"
    paper_ledger.QUOTES_PATH = base / "q.jsonl"
    paper_ledger.MARK_BOOKS_PATH = base / "b.jsonl"
    paper_ledger.DAILY_MARKS_DIR = base / "dm"
    paper_ledger.SETTLEMENT_RESOLUTIONS_PATH = base / "resolutions.jsonl"
    paper_ledger.bus = lambda *a, **k: None
    paper_ledger.utc_now = lambda: {day!r}
    halt_store.HALT_STATE_PATH = base / "halt_state.json"
    halt_store.HALT_FALLBACK_PATH = base / "HALT"
    print("READY", flush=True)
    sys.stdin.readline()
    out = paper_ledger.apply_settlements({day!r})
    print(json.dumps({{"booked": len(out["booked"]), "problems": out["problems"],
                      "deferred": out.get("deferred")}}), flush=True)
""")


def test_a10_two_settler_processes_book_exactly_one_row(led):
    code = SETTLER.format(root=str(ROOT), base=str(led.parent), day=DAY1)
    procs = [subprocess.Popen([sys.executable, "-c", code], cwd=str(ROOT), text=True,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE) for _ in range(2)]
    try:
        for proc in procs:
            if proc.stdout.readline().strip() != "READY":
                raise RuntimeError("settler did not start")
        for proc in procs:
            proc.stdin.write("go\n")
            proc.stdin.flush()
        results = []
        for proc in procs:
            out, _ = proc.communicate(timeout=30)
            if proc.returncode != 0:
                raise RuntimeError(f"settler exited {proc.returncode}: {out!r}")
            results.append(json.loads(out.strip().splitlines()[-1]))
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
    assert all(r["problems"] == [] and r["deferred"] is None for r in results), results
    assert sum(r["booked"] for r in results) == 1, results  # the second one re-planned inside the lock: a no-op
    assert len(_settlement_rows(led)) == 1


# ---------------------------------------------------------------- A11: nothing is loosened
def test_a11_the_commit_lock_timeout_is_unchanged():
    from src import thresholds

    assert thresholds.COMMIT_LOCK_TIMEOUT_S == 5.0 and paper_ledger.COMMIT_LOCK_TIMEOUT_S == 5.0


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 0, -1])
def test_a11_an_invalid_lock_timeout_still_never_locks_and_books_nothing(led, monkeypatch, bad):
    monkeypatch.setattr(paper_ledger, "COMMIT_LOCK_TIMEOUT_S", bad)
    before = _sha(led)
    out = paper_ledger.apply_settlements(DAY1)
    assert _sha(led) == before and out["booked"] == [] and out["deferred"] == paper_ledger.SETTLEMENT_LOCK_UNAVAILABLE


def test_a11_an_unusable_resolutions_file_is_still_a_problem_under_the_lock(led):
    led.parent.joinpath("resolutions.jsonl").write_text("{not json\n", encoding="utf-8")
    out = paper_ledger.apply_settlements(DAY1)
    assert out["problems"] and out["booked"] == [] and "deferred" not in out
