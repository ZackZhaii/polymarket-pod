"""Issue #268 (Task DL): bounded retry of the ledger's os.replace on a Windows PermissionError.

Windows refuses os.replace with PermissionError while another handle (a reader opened without
FILE_SHARE_DELETE, an antivirus scan) briefly holds the target. The ledger's write path
(ledger_store.atomic_append) retries exactly that, a bounded number of times; any other exception, or
any exception on another platform, propagates at once as before; when the tries run out the original
PermissionError propagates, the temp file is removed and the old ledger is untouched (fail closed).
Deterministic: os.replace is faked, the sleep is recorded instead of slept. Offline; tmp_path only.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from src import atomic_replace, ledger_store
from src.fees import booked_fee_usd, ledger_fee

REAL_REPLACE = os.replace


def _tmp_files(path: Path) -> list[str]:
    return sorted(p.name for p in path.parent.iterdir() if p.name.endswith(".tmp"))


@pytest.fixture
def sleeps(monkeypatch):
    recorded: list[float] = []
    monkeypatch.setattr(atomic_replace, "_sleep", recorded.append)
    return recorded


def _windows(monkeypatch, value: bool) -> None:
    monkeypatch.setattr(atomic_replace, "_retry_platform", lambda: value)


def _flaky_replace(monkeypatch, target: Path, errors: list[BaseException]):
    """os.replace onto ``target`` raises the queued errors in order, then really replaces."""
    calls: list[tuple[str, str]] = []

    def fake(src, dst, *args, **kwargs):
        if Path(dst) == target:
            calls.append((str(src), str(dst)))
            if errors:
                raise errors.pop(0)
        return REAL_REPLACE(src, dst, *args, **kwargs)

    monkeypatch.setattr(atomic_replace.os, "replace", fake)
    return calls


def _ledger(tmp_path: Path) -> tuple[Path, bytes]:
    path = tmp_path / "paper.jsonl"
    before = b'{"a": 1}\n'
    path.write_bytes(before)
    return path, before


def test_constants_are_bounded_and_small():
    assert isinstance(atomic_replace.REPLACE_ATTEMPTS, int) and atomic_replace.REPLACE_ATTEMPTS >= 2
    assert isinstance(atomic_replace.REPLACE_BACKOFF_S, float) and atomic_replace.REPLACE_BACKOFF_S > 0
    total = sum(atomic_replace.REPLACE_BACKOFF_S * 2 ** n for n in range(atomic_replace.REPLACE_ATTEMPTS - 1))
    assert total < 1.0  # the retry runs under the ledger mutex: keep the worst-case wait short


def test_platform_switch_matches_the_real_platform():
    assert atomic_replace._retry_platform() == (os.name == "nt")


def test_windows_permission_error_is_retried_then_the_write_lands(tmp_path, monkeypatch, sleeps):
    path, before = _ledger(tmp_path)
    _windows(monkeypatch, True)
    calls = _flaky_replace(monkeypatch, path, [PermissionError(13, "held"), PermissionError(13, "held")])
    ledger_store.atomic_append(path, before, b'{"b": 2}\n')
    assert path.read_bytes() == before + b'{"b": 2}\n'
    assert len(calls) == 3
    base = atomic_replace.REPLACE_BACKOFF_S
    assert sleeps == [base, base * 2]
    assert _tmp_files(path) == []


def test_exhausted_retries_raise_the_original_error_and_clean_up(tmp_path, monkeypatch, sleeps):
    path, before = _ledger(tmp_path)
    _windows(monkeypatch, True)
    errors = [PermissionError(13, f"held {n}") for n in range(atomic_replace.REPLACE_ATTEMPTS)]
    last = errors[-1]
    calls = _flaky_replace(monkeypatch, path, list(errors))
    with pytest.raises(PermissionError) as raised:
        ledger_store.atomic_append(path, before, b'{"b": 2}\n')
    assert raised.value is last  # never swallowed, never re-wrapped
    assert len(calls) == atomic_replace.REPLACE_ATTEMPTS
    assert len(sleeps) == atomic_replace.REPLACE_ATTEMPTS - 1
    assert path.read_bytes() == before  # old ledger byte-for-byte intact
    assert _tmp_files(path) == []  # temp file removed (fail closed)
    # The mutex was released: the next write goes through.
    monkeypatch.setattr(atomic_replace.os, "replace", REAL_REPLACE)
    ledger_store.atomic_append(path, before, b'{"c": 3}\n')
    assert path.read_bytes() == before + b'{"c": 3}\n'


def test_retry_count_and_backoff_are_injectable(tmp_path, monkeypatch, sleeps):
    path, before = _ledger(tmp_path)
    _windows(monkeypatch, True)
    monkeypatch.setattr(atomic_replace, "REPLACE_ATTEMPTS", 2)
    monkeypatch.setattr(atomic_replace, "REPLACE_BACKOFF_S", 0.001)
    calls = _flaky_replace(monkeypatch, path, [PermissionError(13, "held")] * 5)
    with pytest.raises(PermissionError):
        ledger_store.atomic_append(path, before, b'{"b": 2}\n')
    assert len(calls) == 2 and sleeps == [0.001]
    assert path.read_bytes() == before and _tmp_files(path) == []


def test_non_positive_attempts_still_try_once(tmp_path, monkeypatch, sleeps):
    path, before = _ledger(tmp_path)
    _windows(monkeypatch, True)
    monkeypatch.setattr(atomic_replace, "REPLACE_ATTEMPTS", 0)
    calls = _flaky_replace(monkeypatch, path, [])
    ledger_store.atomic_append(path, before, b'{"b": 2}\n')
    assert len(calls) == 1 and sleeps == []
    assert path.read_bytes() == before + b'{"b": 2}\n'


def test_ledger_write_path_uses_the_shared_helper():
    assert ledger_store.replace_with_retry is atomic_replace.replace_with_retry


def test_other_platforms_fail_at_once_exactly_as_before(tmp_path, monkeypatch, sleeps):
    path, before = _ledger(tmp_path)
    _windows(monkeypatch, False)
    first = PermissionError(13, "held")
    calls = _flaky_replace(monkeypatch, path, [first, PermissionError(13, "again")])
    with pytest.raises(PermissionError) as raised:
        ledger_store.atomic_append(path, before, b'{"b": 2}\n')
    assert raised.value is first
    assert len(calls) == 1 and sleeps == []
    assert path.read_bytes() == before and _tmp_files(path) == []


@pytest.mark.parametrize("error", [
    OSError(18, "cross-device"),
    FileNotFoundError(2, "gone"),
    IsADirectoryError(21, "dir"),
    ValueError("not an OSError"),
    KeyboardInterrupt(),
])
def test_other_exceptions_on_windows_are_not_retried(tmp_path, monkeypatch, sleeps, error):
    path, before = _ledger(tmp_path)
    _windows(monkeypatch, True)
    calls = _flaky_replace(monkeypatch, path, [error, error])
    with pytest.raises(type(error)) as raised:
        ledger_store.atomic_append(path, before, b'{"b": 2}\n')
    assert raised.value is error
    assert len(calls) == 1 and sleeps == []
    assert path.read_bytes() == before and _tmp_files(path) == []


def test_book_survives_a_transient_windows_permission_error(tmp_path, monkeypatch, sleeps):
    """The production path (book -> atomic_append) gets the retry too: issue #268's triage."""
    path = tmp_path / "paper.jsonl"
    path.write_bytes(b"")
    _windows(monkeypatch, True)
    calls = _flaky_replace(monkeypatch, path, [PermissionError(13, "held")])
    fill = dict(ts="2026-10-13T00:00:00Z", market="m-dl", token_id="t", side="buy",
                shares=10.0, notional_usd=4.0, fee_rate=0.02, market_price=0.4, effective_price=0.4,
                fee_usd=booked_fee_usd(ledger_fee(0.02, 0.4, 0.4, 4.0, 10.0)),
                hypothetical=False, counts_toward_risk=True, risk_decision="allow", fill_id="dl-0001")
    row = ledger_store.book(path, fill)
    assert len(calls) == 2 and len(sleeps) == 1
    data, rows = ledger_store.read_ledger(path)
    assert data.count(b"\n") == 1 and row["fill_id"] == "dl-0001" and set(rows.ids) == {"dl-0001"}
    assert _tmp_files(path) == []


@pytest.mark.skipif(os.name != "nt", reason="a real held handle blocks os.replace only on Windows")
def test_windows_target_held_open_is_a_real_permission_error_and_the_retry_rides_it_out(tmp_path, monkeypatch):
    """No fake os.replace: an open reader handle on the ledger really refuses the replace.

    The recorded 'sleep' closes the handle on the first retry, so the second try deterministically lands.
    """
    path, before = _ledger(tmp_path)
    holder = open(path, "rb")  # Python opens without FILE_SHARE_DELETE
    try:
        monkeypatch.setattr(atomic_replace, "REPLACE_ATTEMPTS", 1)
        with pytest.raises(PermissionError):
            ledger_store.atomic_append(path, before, b'{"b": 2}\n')
        assert path.read_bytes() == before and _tmp_files(path) == []

        monkeypatch.setattr(atomic_replace, "REPLACE_ATTEMPTS", 3)
        slept: list[float] = []

        def release(seconds: float) -> None:
            slept.append(seconds)
            holder.close()

        monkeypatch.setattr(atomic_replace, "_sleep", release)
        ledger_store.atomic_append(path, before, b'{"b": 2}\n')
        assert len(slept) == 1
        assert path.read_bytes() == before + b'{"b": 2}\n'
        assert _tmp_files(path) == []
    finally:
        holder.close()
