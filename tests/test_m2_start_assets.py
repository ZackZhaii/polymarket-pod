"""Issue #276 draft: m2_start keeps the boot asset set across restarts. Reuses tests/test_m2_start.py's Rig.
Offline; fake launcher; no network, no keys, no orders. Proposed contract (only adds fields):
- boot: the selected token ids are recorded in the chain row ``assets`` (or runs/m2/current.json ``assets``)
  and passed to m1_run as ``--assets``;
- every later start reads them back and passes the SAME ``--assets``; missing / malformed / inconsistent
  -> EXIT_REFUSED, never a fresh --top selection;
- --dry-run prints the asset list and its source (boot selection / chain).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest

import src.heartbeat as heartbeat
import src.m2_start as m2_start
from tests.test_m2_start import Rig, _beat, _chain, _current, _start  # noqa: F401

SCAN1 = ["tA1", "tA2", "tB1", "tB2"]
SCAN2 = ["tX1", "tX2", "tY1", "tY2"]


def _scan(root, tokens, ts):
    """data/markets.jsonl with one batch of two markets (the scanner's shape m1_run.select_assets reads)."""
    path = root / "data" / "markets.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"ts": ts, "id": f"m-{tokens[i]}", "question": "q", "version": "v2", "token_id_source": "clob",
             "token_ids": [tokens[i], tokens[i + 1]], "end_date": "2099-01-01T00:00:00Z",
             "tags": ["politics"], "category": "politics"} for i in (0, 2)]
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def _assets_arg(argv):
    if "--assets" not in argv:
        return None
    out = []
    for part in argv[argv.index("--assets") + 1:]:
        if part.startswith("--"):
            break
        out += [p for p in part.split(",") if p]
    return out


def _m1_argv(rig):
    return [argv for proc, argv, _ in rig.launched if proc == "m1_run"][-1]


def _boot_assets(rig):
    row = _chain(rig.root)[0]
    if isinstance(row.get("assets"), list):
        return row["assets"]
    return _current(rig.root).get("assets")


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path / "pod")
    _scan(r.root, SCAN1, "2026-10-12T00:00:00Z")
    return r


def _reboot(rig, **kw):
    for proc in ("m1_run", "paper_cycle"):
        _beat(proc, state="finished")
    time.sleep(1.1)  # run dirs are stamped to the second
    rig.launched.clear()
    return _start(rig, m2_end=None, reason="reboot", **kw)


# --- RED -------------------------------------------------------------------------------------------

def test_r1_boot_records_the_selected_assets(rig, no_sentinel):
    assert _start(rig) == m2_start.EXIT_OK
    assert _boot_assets(rig) == SCAN1


def test_r2_boot_passes_them_to_m1_run_as_assets(rig, no_sentinel):
    assert _start(rig) == m2_start.EXIT_OK
    assert _assets_arg(_m1_argv(rig)) == SCAN1


def test_r3_reboot_after_a_new_scan_reuses_the_boot_assets(rig, no_sentinel):
    assert _start(rig) == m2_start.EXIT_OK
    _scan(rig.root, SCAN2, "2026-10-13T00:00:00Z")  # the pool churned overnight
    assert _reboot(rig) == m2_start.EXIT_OK
    assert _assets_arg(_m1_argv(rig)) == SCAN1  # never SCAN2, never a fresh --top


def test_r4_reboot_with_no_assets_in_the_chain_refuses(rig, no_sentinel):
    """A chain written by c5aa8d3 (no assets field) must not fall back to a fresh selection."""
    assert _start(rig) == m2_start.EXIT_OK
    path = rig.root / "runs" / "m2-chain.jsonl"
    rows = [{k: v for k, v in r.items() if k != "assets"} for r in _chain(rig.root)]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    cur = rig.root / "runs" / "m2" / "current.json"
    if cur.exists():
        data = json.loads(cur.read_text()); data.pop("assets", None); cur.write_text(json.dumps(data))
    assert _reboot(rig) == m2_start.EXIT_REFUSED
    assert "m1_run" not in rig.order()


@pytest.mark.parametrize("bad", ["tA1,tA2", [], ["tA1", ""], ["tA1", "tA1"], [1, 2], None])
def test_r5_reboot_with_malformed_chain_assets_refuses(rig, no_sentinel, bad):
    _beat("sentinel")
    path = rig.root / "runs" / "m2-chain.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": "2026-10-12T00:00:00.000Z", "run_dir": "runs/m2/m1-x", "git_sha": "x", "reason": "boot",
           "m2_end": "2099-01-01T00:00:00.000Z", "max_runtime_s": 1, "host": "h", "sentinel_pid": 1, "assets": bad}
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert _start(rig, m2_end=None, reason="reboot") == m2_start.EXIT_REFUSED
    assert "m1_run" not in rig.order()


def test_r6_chain_rows_that_disagree_on_assets_refuse(rig, no_sentinel):
    assert _start(rig) == m2_start.EXIT_OK
    path = rig.root / "runs" / "m2-chain.jsonl"
    rows = _chain(rig.root)
    rows.append(dict(rows[0], reason="reboot", run_dir=rows[0]["run_dir"] + "b", assets=SCAN2))
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert _reboot(rig) == m2_start.EXIT_REFUSED


def test_r7_dry_run_prints_assets_and_source(rig, no_sentinel):
    lines: list[str] = []
    assert _start(rig, lines, dry_run=True) == m2_start.EXIT_OK
    text = "\n".join(lines)
    assert all(t in text for t in SCAN1) and "boot" in text.lower() and "assets" in text.lower()
    assert rig.launched == [] and not (rig.root / "runs").exists()


def test_r8_reboot_dry_run_says_the_assets_come_from_the_chain(rig, no_sentinel):
    assert _start(rig) == m2_start.EXIT_OK
    _scan(rig.root, SCAN2, "2026-10-13T00:00:00Z")
    lines: list[str] = []
    for proc in ("m1_run", "paper_cycle"):
        _beat(proc, state="finished")
    time.sleep(1.1)
    assert _start(rig, lines, m2_end=None, reason="reboot", dry_run=True) == m2_start.EXIT_OK
    text = "\n".join(lines)
    assert "chain" in text and all(t in text for t in SCAN1) and not any(t in text for t in SCAN2)


# --- GREEN (must stay green) ----------------------------------------------------------------------

def test_g1_reboot_still_reads_m2_end_from_the_chain_and_gets_a_new_run_dir(rig, no_sentinel):
    assert _start(rig) == m2_start.EXIT_OK
    assert _reboot(rig) == m2_start.EXIT_OK
    one, two = _chain(rig.root)
    assert two["m2_end"] == one["m2_end"] and two["run_dir"] != one["run_dir"]


def test_g2_chain_rows_keep_every_existing_field(rig, no_sentinel):
    assert _start(rig) == m2_start.EXIT_OK
    assert set(m2_start.CHAIN_FIELDS) <= set(_chain(rig.root)[0])  # only adds fields


def test_g3_m1_run_argv_never_carries_a_live_flag(rig, no_sentinel):
    assert _start(rig) == m2_start.EXIT_OK
    assert not any("live" in part.lower() for _, argv, _ in rig.launched for part in argv)


def test_g4_start_order_is_unchanged(rig, no_sentinel):
    assert _start(rig) == m2_start.EXIT_OK
    assert rig.order() == ["sentinel", "m1_run", "paper_cycle"]


def test_g5_a_bad_chain_still_refuses_before_anything_starts(rig):
    path = rig.root / "runs" / "m2-chain.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json\n", encoding="utf-8")
    assert _start(rig, m2_end=None, reason="reboot") == m2_start.EXIT_REFUSED and rig.launched == []


def test_g6_dry_run_touches_nothing(rig, no_sentinel):
    assert _start(rig, [], dry_run=True) == m2_start.EXIT_OK
    assert rig.launched == [] and not (rig.root / "runs").exists()


# --- Task DE additions: E4 (no replacement mid-run) and same-day sports singles -----------------------

def test_e4_an_asset_that_closed_mid_run_stays_in_the_list_on_reboot(rig, no_sentinel):
    """E4: a boot asset whose market closed (gone from the latest scan) is kept and counted as a gap downstream,
    never swapped for another market."""
    assert _start(rig) == m2_start.EXIT_OK
    _scan(rig.root, ["tA1", "tA2", "tZ1", "tZ2"], "2026-10-13T00:00:00Z")  # tB* closed, tZ* is new
    assert _reboot(rig) == m2_start.EXIT_OK
    assert _assets_arg(_m1_argv(rig)) == SCAN1
    assert _chain(rig.root)[-1]["assets"] == SCAN1 and _current(rig.root)["assets"] == SCAN1


def _sports_scan(root, ts, end_date):
    path = root / "data" / "markets.jsonl"
    rows = [
        {"ts": ts, "id": "m-game", "question": "A vs B", "version": "v2", "token_id_source": "clob",
         "token_ids": ["tG1", "tG2"], "tags": ["sports", "tennis"], "category": "sports", "end_date": end_date},
        {"ts": ts, "id": "m-pol", "question": "q", "version": "v2", "token_id_source": "clob",
         "token_ids": ["tP1", "tP2"], "tags": ["politics"], "category": "politics", "end_date": "2099-01-01T00:00:00Z"},
    ]
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_boot_drops_a_sports_market_ending_the_same_utc_day(rig, no_sentinel):
    now = datetime.now(timezone.utc)
    _sports_scan(rig.root, "2026-10-20T00:00:00Z", heartbeat.utc_stamp(now.replace(hour=23, minute=59, second=0)))
    assert _start(rig) == m2_start.EXIT_OK
    assert _boot_assets(rig) == ["tP1", "tP2"]


def test_boot_drops_a_sports_market_ending_on_a_later_day(rig, no_sentinel):
    """Task DI (#137): every sports market is dropped, whatever its end date."""
    _sports_scan(rig.root, "2026-10-20T00:00:00Z", "2099-01-01T00:00:00Z")
    assert _start(rig) == m2_start.EXIT_OK
    assert _boot_assets(rig) == ["tP1", "tP2"]


def test_boot_with_no_usable_assets_refuses(tmp_path, no_sentinel):
    r = Rig(tmp_path / "pod")
    (r.root / "data" / "markets.jsonl").unlink()
    assert _start(r) == m2_start.EXIT_REFUSED and r.launched == []


# --- Task DW (#329): classifier_errors in the boot chain row and runs/m2/current.json (observability only) ----

def _boom_scan(root, ts="2026-10-12T00:00:00Z"):
    """One more market in SCAN1's batch. Eligible on its own, so only a classifier error drops it."""
    path = root / "data" / "markets.jsonl"
    row = {"ts": ts, "id": "m-boom", "question": "q", "version": "v2", "token_id_source": "clob",
           "token_ids": ["tBoom1", "tBoom2"], "end_date": "2099-01-01T00:00:00Z",
           "tags": ["politics"], "category": "politics"}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


def _raise_on_boom(monkeypatch):
    real = m2_start.boot_eligible

    def boot_eligible(row, m2_end):
        if row.get("id") == "m-boom":
            raise RuntimeError("classifier boom")
        return real(row, m2_end)

    monkeypatch.setattr(m2_start, "boot_eligible", boot_eligible)


def test_dw_boot_without_classifier_errors_records_zero(rig, no_sentinel):
    assert _start(rig) == m2_start.EXIT_OK
    row, current = _chain(rig.root)[0], _current(rig.root)
    assert row["classifier_errors"] == 0 and current["classifier_errors"] == 0
    assert row["assets"] == current["assets"] == SCAN1


def test_dw_boot_classifier_error_is_recorded_and_selection_is_unchanged(rig, no_sentinel, monkeypatch):
    # Baseline without the boom market (same Rig scan): selection must stay identical when the boom row raises.
    assert _start(rig) == m2_start.EXIT_OK
    clean_assets, clean_m1 = _chain(rig.root)[0]["assets"], _assets_arg(_m1_argv(rig))
    assert _chain(rig.root)[0]["classifier_errors"] == 0 and clean_assets == SCAN1
    for proc in ("m1_run", "paper_cycle"):
        _beat(proc, state="finished")
    time.sleep(1.1)
    # Fresh root so this start is still a boot (empty chain); keep real heartbeat dir via no_sentinel.
    boom = Rig(rig.root.parent / "boom")
    _scan(boom.root, SCAN1, "2026-10-12T00:00:00Z")
    _boom_scan(boom.root)
    _raise_on_boom(monkeypatch)
    lines: list[str] = []
    assert _start(boom, lines) == m2_start.EXIT_OK  # counted and dropped: no crash, no new halt
    row, current = _chain(boom.root)[0], _current(boom.root)
    assert row["classifier_errors"] == 1 and current["classifier_errors"] == 1
    assert row["assets"] == current["assets"] == clean_assets == SCAN1
    assert _assets_arg(_m1_argv(boom)) == clean_m1 == SCAN1
    assert current["status"] == "started" and boom.order()[-2:] == ["m1_run", "paper_cycle"]
    assert any("classifier_errors=1" in line for line in lines)


def test_dw_without_the_injection_the_extra_market_would_be_selected(rig, no_sentinel):
    """Guards the test above: the injected row only drops out because its classifier raised."""
    _boom_scan(rig.root)
    assert _start(rig) == m2_start.EXIT_OK
    assert _boot_assets(rig) == SCAN1 + ["tBoom1", "tBoom2"] and _chain(rig.root)[0]["classifier_errors"] == 0


def test_dw_reboot_records_null_and_never_classifies(rig, no_sentinel, monkeypatch):
    _boom_scan(rig.root)
    _raise_on_boom(monkeypatch)
    assert _start(rig) == m2_start.EXIT_OK
    monkeypatch.setattr(m2_start, "boot_eligible",
                        lambda row, m2_end: pytest.fail("a later start must not re-classify"))
    assert _reboot(rig) == m2_start.EXIT_OK
    one, two = _chain(rig.root)
    assert one["classifier_errors"] == 1 and two["classifier_errors"] is None
    assert "classifier_errors" in two and _current(rig.root)["classifier_errors"] is None
    assert two["assets"] == one["assets"] == SCAN1 and _assets_arg(_m1_argv(rig)) == SCAN1


def test_dw_old_chain_rows_without_classifier_errors_still_reboot(rig, no_sentinel):
    assert _start(rig) == m2_start.EXIT_OK
    path = rig.root / "runs" / "m2-chain.jsonl"
    rows = [{k: v for k, v in r.items() if k != "classifier_errors"} for r in _chain(rig.root)]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert _reboot(rig) == m2_start.EXIT_OK
    assert _chain(rig.root)[-1]["classifier_errors"] is None and _assets_arg(_m1_argv(rig)) == SCAN1


def test_dw_dry_run_chain_line_shows_classifier_errors(rig, no_sentinel, monkeypatch):
    _boom_scan(rig.root)
    _raise_on_boom(monkeypatch)
    lines: list[str] = []
    assert _start(rig, lines, dry_run=True) == m2_start.EXIT_OK
    assert any("would append" in line and '"classifier_errors": 1' in line for line in lines)
    assert rig.launched == [] and not (rig.root / "runs").exists()


def test_dw_a_failed_current_json_write_still_stops_the_boot(rig, no_sentinel, monkeypatch):
    """The extra field rides the existing atomic write; a failed write stays fail closed (exit 5)."""
    _boom_scan(rig.root)
    _raise_on_boom(monkeypatch)
    monkeypatch.setattr(heartbeat, "write_record",
                        lambda path, record: heartbeat.HB_WRITE_FAILED if path.name == "current.json" else None)
    assert _start(rig) == m2_start.EXIT_NOT_READY and rig.order() == ["sentinel"]
