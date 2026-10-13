"""src/reconcile.py's own usage gate: an unusable ``--user`` is EXIT_USAGE before anything is read.

tests/test_reconcile.py pins the CLI argv errors (``test_cli_usage_is_exit_2_without_fetch_or_bus``),
the torn-ledger exits and the mismatch/remote paths. The gate inside ``reconcile()`` itself
(src/reconcile.py:425-428) was never asserted by name: it is the only check that runs before
``ledger_view`` and before the first ``public_get``, so it decides ordering and fail-closed behaviour.
These tests pin the exact problem code, that nothing is fetched or parsed, that a torn ledger is not
reached first (fail closed before any I/O), that a usable address is still not refused for free
(the same torn ledger is then EXIT_LEDGER), and that the CLI's ``--json`` branch prints that report
without writing a halt to the bus.

Offline: no network, tmp_path only, no bus writes outside tmp_path.
"""
from __future__ import annotations

import json

import pytest

from src import ledger_store, reconcile
from src.fees import booked_fee_usd, ledger_fee

W = "0x1111111111111111111111111111111111111111"
TA = "1" + "0" * 75
PRICE = 0.4

# Every shape the documented "0x + 40 hex" rule refuses: empty, None, short, long, no prefix,
# wrong prefix case, non-hex, and whitespace that survives argparse.
BAD_ADDRESSES = [
    "",
    None,
    "0x",
    "0x" + "1" * 39,
    "0x" + "1" * 41,
    "1" * 40,
    "0X" + "1" * 40,
    "0x" + "z" * 40,
    "0x" + "1" * 40 + " ",
    " 0x" + "1" * 40,
    "0x" + "1" * 40 + "\n",
]


def _no_fetch(url: str):
    """A getter that fails the test: a usage error must return before the first public read."""
    raise AssertionError(f"a usage error must not fetch: {url}")


def _fill(token: str = TA, shares: float = 10.0) -> dict:
    notional = shares * PRICE
    return dict(fill_id=f"usage-{token[:4]}-{shares}", ts="2026-10-09T00:00:00Z", market="m-1",
                token_id=token, side="buy", shares=shares, notional_usd=notional, fee_rate=0.0,
                market_price=PRICE, effective_price=PRICE,
                fee_usd=booked_fee_usd(ledger_fee(0.0, PRICE, PRICE, notional, shares)),
                hypothetical=False, counts_toward_risk=True, risk_decision="allow")


@pytest.mark.parametrize("user", BAD_ADDRESSES, ids=[repr(user)[:26] for user in BAD_ADDRESSES])
def test_an_unusable_user_is_a_usage_error_before_anything_else(tmp_path, user):
    ledger = tmp_path / "paper.jsonl"
    ledger_store.book(ledger, _fill())
    before = ledger.read_bytes()

    report = reconcile.reconcile(user, ledger, _no_fetch)

    assert report.exit_code == reconcile.EXIT_USAGE
    assert report.problems == ["invalid_user_address"]  # the one code, nothing guessed
    assert report.ok is False
    assert (report.tokens_checked, report.positions_remote, report.trades_remote) == (0, 0, 0)
    assert report.lag_s is None and report.pages == {}
    assert ledger.read_bytes() == before  # read-only, and not even parsed


def test_the_gate_runs_before_the_ledger_is_read(tmp_path):
    torn = tmp_path / "paper.jsonl"
    ledger_store.book(torn, _fill())
    torn.write_bytes(torn.read_bytes() + b'{"side":"buy"')  # would be EXIT_LEDGER with a good address

    refused = reconcile.reconcile("0x12", torn, _no_fetch)
    assert (refused.exit_code, refused.problems) == (reconcile.EXIT_USAGE, ["invalid_user_address"])

    # control: the same torn ledger, this time with a usable address, is the ledger error it is
    control = reconcile.reconcile(W, torn, _no_fetch)
    assert control.exit_code == reconcile.EXIT_LEDGER
    assert any(problem.startswith("ledger_invalid:") for problem in control.problems)
    assert "invalid_user_address" not in control.problems


def test_the_usage_report_carries_only_the_address_and_serialises(tmp_path):
    report = reconcile.reconcile("0x12", tmp_path / "missing.jsonl", _no_fetch)
    data = report.as_dict()

    assert data["user"] == "0x12"  # echoed back for the operator, never corrected
    assert data["ok"] is False and data["exit_code"] == reconcile.EXIT_USAGE
    assert data["problems"] == ["invalid_user_address"]
    assert data["lag_s"] is None and data["tokens_checked"] == 0
    assert json.loads(json.dumps(data, ensure_ascii=False, sort_keys=True)) == data


def test_main_prints_the_usage_report_and_writes_no_halt(tmp_path, capsys):
    bus = tmp_path / "bus.jsonl"
    code = reconcile.main(["--user", "0x" + "z" * 40, "--ledger", str(tmp_path / "l.jsonl"),
                           "--bus", str(bus), "--json"], getter=_no_fetch)

    assert code == reconcile.EXIT_USAGE
    assert not bus.exists()  # exit 2 is not a run failure: no global halt
    printed = json.loads(capsys.readouterr().out)
    assert printed["problems"] == ["invalid_user_address"]
    assert printed["exit_code"] == reconcile.EXIT_USAGE and printed["ok"] is False


def test_the_rule_is_exactly_0x_plus_40_hex():
    checksummed = "0xAbCdEf0123456789AbCdEf0123456789aBcDeF01"
    assert len(checksummed) == 42 and reconcile.ADDRESS_RE.fullmatch(checksummed)
    assert reconcile.ADDRESS_RE.fullmatch("0xAbCdEf0123456789AbCdEf0123456789aBcDeF0") is None  # 39 hex
    assert reconcile.ADDRESS_RE.fullmatch(checksummed.upper()) is None  # "0X..." is not the rule
