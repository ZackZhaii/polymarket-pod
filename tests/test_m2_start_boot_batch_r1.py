"""#362-R1 acceptance: parsed same-read batch declaration, not file integrity.

Only owned synthetic roots are used. Application IO is fenced separately from
imports/fixture setup. Launch/readiness/clock/pins/host/PID boundaries are fake;
reader, classification, selection, chain fsync and Current's atomic writer are real.
The Source RED runner controls SSL/proxy/opener initialization before conftest.
"""
from __future__ import annotations

import copy
import importlib
import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

NOW = datetime(2026, 11, 5, 7, 0, tzinfo=timezone.utc)
END = "2099-01-01T00:00:00Z"
TS = "2026-11-05T07:00:00.000000Z"
BOOT = {"scan_ts": TS, "batch_rows": 1}


def row(name="A", ts=TS, **extra):
    return {"id": name, "ts": ts, "question": "synthetic",
            "version": "v2", "token_id_source": "clob",
            "token_ids": [name + "Y", name + "N"], "tags": ["politics"],
            "category": "politics", "end_date": END, **extra}


def scan(root, rows, tail=""):
    path = root / "data" / "markets.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=True) + "\n" for r in rows) + tail,
                    encoding="utf-8")
    return path


@contextmanager
def application(root):
    """Reject external application IO; this is not an interpreter/OS sandbox."""
    active = [True]
    allowed = os.path.normcase(os.path.abspath(root))
    denied = []

    def check(value):
        if isinstance(value, int) or value is None:
            return
        target = os.path.normcase(os.path.abspath(os.fsdecode(value)))
        if os.path.commonpath([allowed, target]) != allowed:
            denied.append(target)
            raise AssertionError("external application IO: " + target)

    def audit(event, args):
        if not active[0]:
            return
        if event in {"socket.connect", "socket.getaddrinfo", "subprocess.Popen",
                     "os.system", "os.kill", "os.posix_spawn"}:
            denied.append(event)
            raise AssertionError("real process/network/PID boundary: " + event)
        if event == "open":
            check(args[0])
        elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.listdir", "os.scandir"}:
            check(args[0])
        elif event == "os.rename":
            check(args[0])
            check(args[1])

    sys.addaudithook(audit)
    real_stat, real_lstat = os.stat, os.lstat

    def stat(path, *args, **kw):
        check(path)
        return real_stat(path, *args, **kw)

    def lstat(path, *args, **kw):
        check(path)
        return real_lstat(path, *args, **kw)

    try:
        with patch.object(os, "stat", stat), patch.object(os, "lstat", lstat):
            yield
    finally:
        active[0] = False
        assert not denied, denied


@pytest.fixture
def env(tmp_path, monkeypatch):
    # No top-level project import in this file. Source's runner also controls
    # conftest's earlier imports and its shared heartbeat fixture.
    ms = importlib.import_module("src.m2_start")
    root = tmp_path / "pod"
    root.mkdir()
    scan(root, [row()])
    state = SimpleNamespace(ms=ms, root=root, now=NOW, launched=[], writes=[], lines=[])
    monkeypatch.setattr(ms.heartbeat, "git_sha", lambda: "synthetic-git")
    monkeypatch.setattr(ms.socket, "gethostname", lambda: "synthetic-host")
    monkeypatch.setattr(ms.pins, "refuse_start", lambda *a, **k: [])
    monkeypatch.setattr(ms.pins, "risk_file_problems", lambda *a, **k: [])
    monkeypatch.setattr(ms.halt_store, "main", lambda *a, **k: 0)
    monkeypatch.setattr(ms, "popen_launcher",
                        lambda *a, **k: pytest.fail("real launcher selected"))
    monkeypatch.setattr(ms, "recording_problem", lambda p: None)

    def record(proc):
        return {"pid": 1000 + len(state.launched), "state": "running",
                "started_at": ms.heartbeat.utc_stamp(state.now)}

    def fresh(proc, **kw):
        return (record(proc), None) if proc in [p for p, _ in state.launched] else (None, "absent")

    monkeypatch.setattr(ms, "fresh_beat", fresh)
    monkeypatch.setattr(ms, "sentinel_fresh", lambda **kw: fresh("sentinel", **kw))
    real_write = ms.heartbeat.write_record

    def write(path, data):
        assert path.is_relative_to(root)
        if path.name == "current.json":
            state.writes.append(copy.deepcopy(data))
        return real_write(path, data)

    monkeypatch.setattr(ms.heartbeat, "write_record", write)
    return state


def detail(env, **kw):
    with application(env.root):
        return env.ms.boot_selection_detail(env.root, env.now, **kw)


def start(env, *, reason="manual", dry_run=False, m2_end=END):
    def launch(argv, log, cwd):
        assert cwd == env.root and log.is_relative_to(env.root)
        proc = "sentinel" if argv[-1] == "src.sentinel_tick" else argv[2].removeprefix("src.")
        env.launched.append((proc, list(argv)))
        return SimpleNamespace(pid=1000 + len(env.launched), poll=lambda: None)

    with application(env.root):
        return env.ms.start(reason=reason, m2_end=m2_end, dry_run=dry_run,
                            root=env.root, launcher=launch, python="synthetic-python",
                            now=lambda: env.now, sleep=lambda _: pytest.fail("unexpected readiness wait"),
                            monotonic=lambda: 0.0, runner_available=lambda _: False,
                            out=env.lines.append)


def chain(env):
    return [json.loads(line) for line in (env.root / "runs/m2-chain.jsonl").read_text().splitlines()]


@pytest.mark.parametrize("microsecond", [0, 123456])
def test_same_read_instant_count_selection_and_call_counts(env, monkeypatch, microsecond):
    instant = NOW.replace(microsecond=microsecond)
    stamp = instant.isoformat(timespec="microseconds").replace("+00:00", "Z")
    rows = [row("OLD", "2026-11-04T07:00:00Z"),
            row("A", stamp), row("A", instant.astimezone(timezone(timedelta(hours=9))).isoformat()),
            row("SPORT", instant.timestamp(), tags=["sports"], category="sports"),
            row("BAD", stamp, tags={"unknown": "shape"}), row("EMPTY", stamp, token_ids=[]),
            row("INVALID", "not-a-time")]
    scan(env.root, rows)
    calls = {"read": [], "parse": [], "select": []}
    real_read, real_parse, real_select = env.ms.read_jsonl, env.ms.parse_book_ts, env.ms.select_assets

    def read(path):
        calls["read"].append(path)
        result = real_read(path)
        calls["rows"] = result
        calls["before"] = copy.deepcopy(result)
        return result

    def parse(value):
        calls["parse"].append(value)
        return real_parse(value)

    def select(kept, top):
        calls["select"].append(([r["id"] for r in kept], top))
        return real_select(kept, top)

    monkeypatch.setattr(env.ms, "read_jsonl", read)
    monkeypatch.setattr(env.ms, "parse_book_ts", parse)
    monkeypatch.setattr(env.ms, "select_assets", select)
    d = detail(env)
    assert d["assets"] == ["AY", "AN"] and d["why"] is None and d["classifier_errors"] == 0
    assert calls["read"] == [env.root / "data/markets.jsonl"]
    assert len(calls["parse"]) == 2 * len(rows)
    assert calls["select"] == [(["A", "A", "EMPTY"], env.ms.M1_TOP_MARKETS)]
    assert calls["rows"] == calls["before"]
    assert type(d["boot"]) is dict and set(d["boot"]) == {"scan_ts", "batch_rows"}
    assert d["boot"] == {"scan_ts": stamp, "batch_rows": 5}
    assert type(d["boot"]["batch_rows"]) is int


def test_wrapper_remains_original_two_tuple(env):
    with application(env.root):
        result = env.ms.boot_selection(env.root, NOW)
    assert type(result) is tuple and result == (["AY", "AN"], None)


def test_declarations_are_fresh_and_do_not_mutate_input(env):
    a, b = detail(env), detail(env)
    assert a["assets"] == b["assets"] == ["AY", "AN"]
    assert a["boot"] == b["boot"] == BOOT and a["boot"] is not b["boot"]


@pytest.mark.parametrize("unused", [float("nan"), float("inf"), float("-inf"), "\ud800"])
def test_unused_values_do_not_add_strict_serialization_or_veto(env, unused):
    scan(env.root, [row(unused=unused)])
    d = detail(env)
    assert d["assets"] == ["AY", "AN"] and d["why"] is None and d["classifier_errors"] == 0
    assert d["boot"] == BOOT


def test_lax_bad_json_nonobjects_and_partial_tail_keep_real_batch(env, capsys):
    scan(env.root, [row()], "not-json\n[]\n{\"partial\":")
    d = detail(env)
    assert d["assets"] == ["AY", "AN"] and d["why"] is None
    assert "3 unreadable line(s) skipped" in capsys.readouterr().err
    assert d["boot"] == BOOT


@pytest.mark.parametrize("missing", [True, False])
def test_missing_or_no_timestamp_keeps_none_and_old_classification(env, monkeypatch, missing):
    calls = []
    real = env.ms.boot_eligible
    monkeypatch.setattr(env.ms, "boot_eligible",
                        lambda r, end: (calls.append(r["id"]), real(r, end))[1])
    if missing:
        (env.root / "data/markets.jsonl").unlink()
    else:
        scan(env.root, [row(ts="bad")])
    d = detail(env)
    assert d["assets"] == [] and d["why"] and d["classifier_errors"] == 0
    assert calls == ([] if missing else ["A"])
    assert d["boot"] is None


@pytest.mark.parametrize("kind", [OSError, ValueError])
def test_existing_handled_reader_error_is_not_reclassified(env, monkeypatch, kind):
    marker = kind("owned reader failure")
    monkeypatch.setattr(env.ms, "read_jsonl", lambda p: (_ for _ in ()).throw(marker))
    d = detail(env)
    assert d["assets"] == [] and d["classifier_errors"] == 0
    assert d["why"] == f"data/markets.jsonl unreadable ({kind.__name__})"
    assert d["boot"] is None


@pytest.mark.parametrize("kind", [MemoryError, RecursionError])
def test_old_classifier_resource_exceptions_are_still_counted(env, monkeypatch, kind):
    marker = kind("classification")
    monkeypatch.setattr(env.ms, "boot_eligible", lambda *a: (_ for _ in ()).throw(marker))
    d = detail(env)
    assert d["classifier_errors"] == 1 and d["assets"] == [] and d["why"]
    assert d["boot"] == BOOT


@pytest.mark.parametrize("boundary", ["parse", "select"])
def test_old_escaping_error_preserves_same_object(env, monkeypatch, boundary):
    marker = RuntimeError("original escaping boundary")
    name = "parse_book_ts" if boundary == "parse" else "select_assets"
    monkeypatch.setattr(env.ms, name, lambda *a: (_ for _ in ()).throw(marker))
    with pytest.raises(RuntimeError) as caught:
        detail(env)
    assert caught.value is marker


@pytest.mark.parametrize("kind", [MemoryError, RecursionError, RuntimeError, KeyboardInterrupt, SystemExit])
def test_observation_error_has_no_normal_adoptable_result(env, monkeypatch, kind):
    marker = kind("observation fault")

    class Bomb(datetime):
        def isoformat(self, *a, **kw):
            raise marker

    real_parse = env.ms.parse_book_ts

    def parse(value):
        result = real_parse(value)
        return Bomb.fromtimestamp(result.timestamp(), timezone.utc) if result is not None else None

    monkeypatch.setattr(env.ms, "parse_book_ts", parse)
    with pytest.raises(kind) as caught:
        detail(env)
    assert caught.value is marker


@pytest.mark.parametrize("reason", ["manual", "boot"])
def test_first_start_persists_same_batch_in_chain_and_original_seven_atomic_writes(env, reason):
    assert start(env, reason=reason) == env.ms.EXIT_OK
    rows = chain(env)
    assert len(rows) == 1 and rows[0]["assets"] == ["AY", "AN"] and rows[0]["reason"] == reason
    assert [p for p, _ in env.launched] == ["sentinel", "m1_run", "paper_cycle"]
    assert len(env.writes) == 7 and [r["status"] for r in env.writes] == ["starting"] * 6 + ["started"]
    assert env.writes[-1]["schema"] == "m2_current/1"
    assert rows[0]["boot"] == BOOT
    assert all(r["boot"] == BOOT for r in env.writes)
    assert json.loads((env.root / "runs/m2/current.json").read_text())["boot"] == BOOT


def test_valid_latest_without_usable_assets_retains_fact_but_refuses_before_writes(env):
    scan(env.root, [row(token_ids=[])])
    d = detail(env)
    assert d["assets"] == [] and d["why"]
    assert start(env) == env.ms.EXIT_REFUSED and env.launched == [] and env.writes == []
    assert not (env.root / "runs").exists()
    assert d["boot"] == BOOT


@pytest.mark.parametrize("legacy", [True, False])
def test_chain_reuse_even_reason_boot_never_reselects_and_records_null(env, monkeypatch, legacy):
    path = env.root / "runs/m2-chain.jsonl"
    path.parent.mkdir(parents=True)
    old = {"ts": TS, "run_dir": "runs/m2/m1-old", "git_sha": "old", "reason": "manual",
           "m2_end": END, "max_runtime_s": 123, "host": "old", "sentinel_pid": 7,
           "assets": ["frozenY", "frozenN"]}
    if not legacy:
        old.update(boot={"scan_ts": "old observation", "batch_rows": 99}, classifier_errors=4)
    path.write_text(json.dumps(old) + "\n")
    for name in ["read_jsonl", "boot_eligible", "select_assets"]:
        monkeypatch.setattr(env.ms, name, lambda *a: pytest.fail("chain reuse reselects"))
    assert start(env, reason="boot", m2_end=None) == env.ms.EXIT_OK
    rows = chain(env)
    assert len(rows) == 2 and rows[-1]["assets"] == old["assets"]
    assert rows[-1]["classifier_errors"] is None
    m1 = next(argv for proc, argv in env.launched if proc == "m1_run")
    assert m1[m1.index("--assets") + 1] == "frozenY,frozenN"
    assert rows[-1]["boot"] is None and all(r["boot"] is None for r in env.writes)


def test_dry_run_uses_owned_inputs_but_writes_or_launches_nothing(env):
    assert start(env, dry_run=True) == env.ms.EXIT_OK
    assert env.launched == [] and env.writes == [] and not (env.root / "runs").exists()
    line = next(x for x in env.lines if "would append" in x)
    d = json.loads(line[line.index("{"):])
    assert d["assets"] == ["AY", "AN"]
    assert d["boot"] == BOOT


@pytest.mark.parametrize("failure", ["current", "chain"])
def test_existing_write_failure_order_and_exit_five_are_preserved(env, monkeypatch, failure):
    captured = []
    if failure == "current":
        def broken(path, data):
            captured.append(copy.deepcopy(data))
            return env.ms.heartbeat.HB_WRITE_FAILED
        monkeypatch.setattr(env.ms.heartbeat, "write_record", broken)
    else:
        real_open = Path.open

        def opened(path, mode="r", *a, **kw):
            if path == env.root / "runs/m2-chain.jsonl" and mode == "a":
                raise OSError("owned chain append failure")
            return real_open(path, mode, *a, **kw)
        monkeypatch.setattr(Path, "open", opened)
    assert start(env) == env.ms.EXIT_NOT_READY
    assert [proc for proc, _ in env.launched] == ["sentinel"]
    assert not (env.root / "runs/m2-chain.jsonl").exists()
    assert not any(p == "m1_run" for p, _ in env.launched)
    snapshots = captured if failure == "current" else env.writes
    assert len(snapshots) == 1
    assert snapshots[0]["boot"] == BOOT

