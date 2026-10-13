"""Issue #64, criterion 4: two *independent processes* book the same paper ledger.

The deterministic-interleave regressions in test_ledger_integrity.py inject the race inside one
process; this file runs two real child interpreters against one ledger file. Both may win
serially, or one side may be refused (lock failure / ledger_changed_during_write) — but a book
that returned success must never be missing from the ledger afterwards (the reported bug: two
successful books, one row gone, verify still clean). Offline; every ledger lives in tmp_path.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from src import ledger_store

ROOT = Path(__file__).resolve().parents[1]
FILLS_PER_CHILD = 5  # several lock handoffs, not one lucky shot


def _tmp_files(ledger: Path) -> list[str]:
    return sorted(p.name for p in ledger.parent.glob(f".{ledger.name}.*.tmp"))


_CHILD = r"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from src.fees import booked_fee_usd, ledger_fee
from src.ledger_store import LedgerIntegrityError
from src import ledger_store

ledger, tag, go, out = Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]), Path(sys.argv[5])
n = int(sys.argv[6])
deadline = time.monotonic() + 60  # the parent writes the go file right after both children start
while not go.exists():
    if time.monotonic() > deadline:
        raise SystemExit(f"go file never appeared for {tag}")
    time.sleep(0.005)
with out.open("a", encoding="utf-8") as handle:
    for i in range(n):
        row = dict(ts="2026-10-13T00:00:00Z", market=f"m-{tag}-{i}", token_id="t", side="buy",
                   shares=10.0, notional_usd=4.0, fee_rate=0.02, market_price=0.4, effective_price=0.4,
                   fee_usd=booked_fee_usd(ledger_fee(0.02, 0.4, 0.4, 4.0, 10.0)),
                   hypothetical=False, counts_toward_risk=True, risk_decision="allow")
        try:
            written = ledger_store.book(ledger, row)
            attempt = {"tag": tag, "i": i, "status": "ok", "fill_id": written["fill_id"]}
        except (LedgerIntegrityError, OSError) as exc:  # the two legal refusals (criterion 5)
            attempt = {"tag": tag, "i": i, "status": "refused", "error": f"{type(exc).__name__}: {exc}"}
        handle.write(json.dumps(attempt) + "\n")
        handle.flush()
"""


def _child(ledger: Path, go: Path, out: Path, tag: str) -> list[str]:
    return [sys.executable, "-c", _CHILD, str(ROOT), str(ledger), tag, str(go), str(out),
            str(FILLS_PER_CHILD)]


def _attempts(tmp_path: Path) -> list[dict]:
    lines = [line for tag in ("a", "b")
             for line in (tmp_path / f"{tag}.result.jsonl").read_text(encoding="utf-8").splitlines()
             if line.strip()]
    return [json.loads(line) for line in lines]


def _wait(proc: subprocess.Popen, tmp_path: Path) -> None:
    assert proc.wait(timeout=180) == 0, (proc.communicate(), _attempts(tmp_path))


def test_two_processes_never_lose_a_successful_book(tmp_path):
    ledger, go = tmp_path / "paper.jsonl", tmp_path / "go"
    outs = {tag: tmp_path / f"{tag}.result.jsonl" for tag in ("a", "b")}
    procs = [subprocess.Popen(_child(ledger, go, outs[tag], tag), cwd=str(tmp_path),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for tag in ("a", "b")]
    time.sleep(0.5)  # both children are parked on go.exists() before the race starts
    go.write_text("1")
    for proc in procs:
        _wait(proc, tmp_path)

    attempts = _attempts(tmp_path)
    assert len(attempts) == 2 * FILLS_PER_CHILD  # every attempt reported exactly once
    oks = [a for a in attempts if a["status"] == "ok"]
    refused = [a for a in attempts if a["status"] == "refused"]
    assert len(oks) + len(refused) == len(attempts) and oks  # never vacuous: someone booked
    for a in refused:  # only the two legal conflict paths (criteria 2 and 5)
        assert a["error"].startswith(("LedgerIntegrityError: ledger integrity: ledger_changed_during_write",
                                      "OSError: ledger_lock_failed")), a

    _, read = ledger_store.read_ledger(ledger)
    assert ledger_store.verify(read) == []  # the survivor ledger is provably consistent
    ok_ids = [a["fill_id"] for a in oks]
    assert len(ok_ids) == len(set(ok_ids))  # each ok is its own fill, booked exactly once
    # Criterion 4's forbidden outcome is "both sides returned success yet a row is missing".
    # Stronger and still required: every successful book survived and nothing else was written.
    assert set(ok_ids) == set(read.ids)
    assert _tmp_files(ledger) == []


def test_two_processes_book_serially_keep_every_fill(tmp_path):
    # The other legal outcome of criterion 4, pinned deterministically: no overlap, all books ok.
    ledger, go = tmp_path / "paper.jsonl", tmp_path / "go"
    go.write_text("1")  # the barrier is already open: each child runs alone, start to finish
    for tag in ("a", "b"):
        out = tmp_path / f"{tag}.result.jsonl"
        _wait(subprocess.Popen(_child(ledger, go, out, tag), cwd=str(tmp_path),
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True), tmp_path)

    attempts = _attempts(tmp_path)
    assert len(attempts) == 2 * FILLS_PER_CHILD
    assert all(a["status"] == "ok" for a in attempts)  # serially there is no conflict to refuse
    _, read = ledger_store.read_ledger(ledger)
    assert ledger_store.verify(read) == []
    assert set(a["fill_id"] for a in attempts) == set(read.ids)
    assert _tmp_files(ledger) == []
