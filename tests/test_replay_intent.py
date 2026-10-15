"""Issue #80: src/replay_intent.py — the single-shot offline replay bundle and its entry point."""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest

from src import replay_intent

NOW = "2026-09-01T12:00:00Z"
BOOK_TS = "2026-09-01T11:59:55.000Z"
SIGNAL = {"type": "signal", "ts": NOW, "from": "NewsForecaster", "market": "m1",
          "payload": {"forecast_id": "f-1", "model_prob": 0.47, "p_low": 0.45, "p_high": 0.49,
                      "outcome_index": 0, "token_id": "t-yes", "note": "replay fixture"}}
MARKET = {"id": "m1", "condition_id": "0xm1", "token_ids": ["t-yes", "t-no"], "outcomes": ["Yes", "No"],
          "end_date": "2026-10-15T00:00:00Z", "liquidity_usd": 52000.0, "accepting_orders": True,
          "closed": False, "tags": ["test-market"]}


def book_row(token: str, ask: float, bid: float) -> dict:
    return {"market_id": "m1", "token_id": token, "best_bid": bid, "best_ask": ask, "best_bid_size": 400.0,
            "best_ask_size": 400.0, "spread": round(ask - bid, 6), "book_ts": BOOK_TS, "tick_size": 0.01,
            "min_order_size": 5.0}


def bundle(**overrides) -> dict:
    base = {"schema_version": 1, "signal": dict(SIGNAL), "market": dict(MARKET),
            "books": [book_row("t-yes", 0.41, 0.39), book_row("t-no", 0.61, 0.59)],
            "strategy": "news-forecaster", "fee_rate": 0.0, "now": NOW}
    base.update(overrides)
    return base


def write(tmp_path: Path, payload, name: str = "bundle.json") -> Path:
    path = tmp_path / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def run(tmp_path: Path, capsys, payload) -> tuple[int, dict | None, str]:
    code = replay_intent.main(["--bundle", str(write(tmp_path, payload))])
    out, err = capsys.readouterr()
    return code, (json.loads(out) if out.strip() else None), err


# ---------------------------------------------------------------- the single shot

def test_one_valid_bundle_builds_one_shadow_intent(tmp_path, capsys):
    code, out, err = run(tmp_path, capsys, bundle())
    assert code == replay_intent.EXIT_OK and err == ""
    assert out["shadow"] is True and out["reasons"] == [] and out["first_reason"] is None
    assert out["intent"] is not None and out["intent"]["token_id"] == "t-yes"
    assert out["intent"]["side"] == "buy" and out["kelly_shadow"] == out["intent"]["kelly_frac"]


def test_the_same_bundle_replays_to_the_same_result(tmp_path, capsys):
    path = write(tmp_path, bundle())
    first = replay_intent.main(["--bundle", str(path)])
    out_first = capsys.readouterr().out
    assert replay_intent.main(["--bundle", str(path)]) == first == replay_intent.EXIT_OK
    assert capsys.readouterr().out == out_first


def test_a_stale_book_row_keeps_the_builders_own_reason(tmp_path, capsys):
    rows = [dict(book_row("t-yes", 0.41, 0.39), book_ts="2026-09-01T10:00:00.000Z"),
            book_row("t-no", 0.61, 0.59)]
    code, out, _ = run(tmp_path, capsys, bundle(books=rows))
    assert code == replay_intent.EXIT_OK and out["intent"] is None and "stale_book" in out["reasons"]


def test_a_stale_signal_keeps_the_builders_own_reason(tmp_path, capsys):
    code, out, _ = run(tmp_path, capsys, bundle(signal=dict(SIGNAL, ts="2026-08-01T12:00:00Z")))
    assert code == replay_intent.EXIT_OK and out["intent"] is None and "stale_signal" in out["reasons"]


def _without(row: dict, *keys: str) -> dict:
    return {key: value for key, value in row.items() if key not in keys}


@pytest.mark.parametrize("payload, reasons",
                         [(bundle(books=[_without(book_row("t-yes", 0.41, 0.39), "tick_size"),
                                         book_row("t-no", 0.61, 0.59)]), ["missing_tick_size"]),
                          (bundle(books=[_without(book_row("t-yes", 0.41, 0.39), "min_order_size"),
                                         book_row("t-no", 0.61, 0.59)]), ["missing_min_order_size"]),
                          (bundle(signal=dict(SIGNAL, payload=_without(SIGNAL["payload"], "token_id",
                                                                       "outcome_index"))),
                           ["missing_outcome_index", "missing_token_id"]),
                          (bundle(market=_without(MARKET, "tags")), ["missing_category"])],
                         ids=["no-tick-size", "no-min-order-size", "no-signal-metadata", "no-market-tags"])
def test_missing_metadata_keeps_the_builders_own_reasons(tmp_path, capsys, payload, reasons):
    # Issue #80 acceptance 2/5: nothing is filled in with a default — the builder's own reasons come back.
    code, out, _ = run(tmp_path, capsys, payload)
    assert code == replay_intent.EXIT_OK and out["intent"] is None
    assert all(reason in out["reasons"] for reason in reasons), out["reasons"]


def test_no_book_rows_is_missing_book_not_a_default(tmp_path, capsys):
    code, out, _ = run(tmp_path, capsys, bundle(books=[]))
    assert code == replay_intent.EXIT_OK and out["intent"] is None and "missing_book" in out["reasons"]


def test_a_null_fee_rate_keeps_missing_fee_data(tmp_path, capsys):
    code, out, _ = run(tmp_path, capsys, bundle(fee_rate=None))
    assert code == replay_intent.EXIT_OK and out["intent"] is None and "missing_fee_data" in out["reasons"]


# ---------------------------------------------------------------- refusals

@pytest.mark.parametrize("payload", ["{not json", json.dumps({"schema_version": 1}),
                                     json.dumps(bundle(schema_version=2)), json.dumps(bundle(now="2026-09-01T12:00:00"))],
                         ids=["not-json", "missing-keys", "wrong-schema", "naive-now"])
def test_a_corrupt_bundle_is_refused_with_its_own_code(tmp_path, capsys, payload):
    code, out, err = run(tmp_path, capsys, payload)
    assert code == replay_intent.EXIT_CORRUPT_BUNDLE and out is None and "replay_intent:" in err


@pytest.mark.parametrize("payload",
                         [bundle(signal=[SIGNAL]), bundle(history=[{"ts": NOW}]), bundle(books=[book_row("t-x", 0.5, 0.4)])],
                         ids=["two-signals", "history", "another-market"])
def test_anything_but_one_signal_and_one_market_is_unsupported(tmp_path, capsys, payload):
    if payload["books"] and payload["books"][0]["token_id"] == "t-x":
        payload["books"][0]["market_id"] = "m2"
    code, out, err = run(tmp_path, capsys, payload)
    assert code == replay_intent.EXIT_UNSUPPORTED_BUNDLE and out is None and "replay_intent:" in err


def test_a_usage_error_is_the_entrys_own_code(tmp_path, capsys):
    assert replay_intent.main(["--bundle"]) == replay_intent.EXIT_USAGE
    assert replay_intent.main([]) == replay_intent.EXIT_USAGE
    capsys.readouterr()


def test_pin_drift_is_refused_before_anything_is_replayed(tmp_path, monkeypatch, capsys):
    from src import pins

    monkeypatch.setattr(pins, "PINNED_RISK_SHA256", "0" * 64)  # config/risk.toml reads as off its pins

    def boom(*args, **kwargs):
        raise AssertionError("this read-only entry records no halt")

    monkeypatch.setattr(pins, "record_drift", boom)
    out_path = tmp_path / "result.json"
    code = replay_intent.main(["--bundle", str(write(tmp_path, bundle())), "--out", str(out_path)])
    out, err = capsys.readouterr()
    assert code == replay_intent.EXIT_PIN_DRIFT and out == "" and "off its pins" in err
    assert not out_path.exists()  # refused before the replay; no result published


# ---------------------------------------------------------------- the published result JSON

def test_out_publishes_one_result_json_with_the_input_sha256(tmp_path, capsys):
    path = write(tmp_path, bundle())
    out_path = tmp_path / "result.json"
    code = replay_intent.main(["--bundle", str(path), "--out", str(out_path)])
    captured = capsys.readouterr()
    assert code == replay_intent.EXIT_OK and captured.err == ""
    published = json.loads(out_path.read_text(encoding="utf-8"))
    printed = json.loads(captured.out)
    assert published["input_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert published["input_sha256"] == printed["input_sha256"]
    assert published["shadow"] is True and published["intent"] == printed["intent"]
    assert published["reasons"] == [] and published["first_reason"] is None
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]  # no temp file left behind


def test_a_second_run_never_overwrites_an_existing_output(tmp_path, capsys):
    out_path = tmp_path / "result.json"
    out_path.write_bytes(b"the previous result\n")
    code = replay_intent.main(["--bundle", str(write(tmp_path, bundle())), "--out", str(out_path)])
    out, err = capsys.readouterr()
    assert code == replay_intent.EXIT_OUTPUT_EXISTS and "replay_intent:" in err and out == ""
    assert out_path.read_bytes() == b"the previous result\n"


def test_an_output_appearing_after_the_check_is_never_overwritten(tmp_path, monkeypatch, capsys):
    # Output collision at the atomic link: another run publishes between the exists() check and
    # os.link — the link refuses it (FileExistsError) and the earlier result stands.
    out_path = tmp_path / "result.json"
    bundle_path = write(tmp_path, bundle())
    out_path.write_bytes(b"the previous result\n")
    real_exists = Path.exists
    checked: list[str] = []

    def racing(self):
        if self == out_path and not checked:  # the pre-check still sees a free slot
            checked.append("seen")
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", racing)
    code = replay_intent.main(["--bundle", str(bundle_path), "--out", str(out_path)])
    out, err = capsys.readouterr()
    assert code == replay_intent.EXIT_OUTPUT_EXISTS and "never overwriting" in err
    assert json.loads(out)["shadow"] is True  # the replay itself ran; only the file publish is refused
    assert out_path.read_bytes() == b"the previous result\n"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]  # no temp file left behind


def test_the_same_bundle_and_now_publish_byte_identical_results(tmp_path, capsys):
    path = write(tmp_path, bundle())
    first, second = tmp_path / "r1.json", tmp_path / "r2.json"
    assert replay_intent.main(["--bundle", str(path), "--out", str(first)]) == replay_intent.EXIT_OK
    capsys.readouterr()
    assert replay_intent.main(["--bundle", str(path), "--out", str(second)]) == replay_intent.EXIT_OK
    capsys.readouterr()
    assert first.read_bytes() == second.read_bytes()


def test_an_out_path_in_config_a_toml_or_thresholds_is_a_usage_error(tmp_path, monkeypatch, capsys):
    from src import podlib

    monkeypatch.setattr(podlib, "ROOT", tmp_path)
    for rel in ("config/result.json", "config/risk.toml", "risk.toml", "src/thresholds.py"):
        code = replay_intent.main(["--bundle", str(write(tmp_path, bundle())), "--out", str(tmp_path / rel)])
        assert code == replay_intent.EXIT_USAGE, rel
        assert "replay_intent:" in capsys.readouterr().err, rel
        assert not (tmp_path / rel).exists(), rel


def test_a_result_that_cannot_be_published_fails_closed(tmp_path, capsys):
    out_path = tmp_path / "no-such-dir" / "result.json"
    code = replay_intent.main(["--bundle", str(write(tmp_path, bundle())), "--out", str(out_path)])
    out, err = capsys.readouterr()
    assert code == replay_intent.EXIT_WRITE_FAILED and not out_path.exists()
    assert "replay_intent:" in err and json.loads(out)["shadow"] is True


# ---------------------------------------------------------------- read-only surface

WRITE_ROOTS = ("os", "tempfile")
WRITE_ATTRS = ("write", "flush")


def _file_calls(names: set[str]) -> set[str]:
    """Calls that can touch a file: the os/tempfile modules, or any .write()/.flush()."""
    return {name for name in names
            if name.split(".")[0] in WRITE_ROOTS or name.split(".")[-1] in WRITE_ATTRS}


def _called(node: ast.AST) -> set[str]:
    called: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            called.add(ast.unparse(sub.func))
    return called


def test_the_entry_has_no_execution_or_network_surface():
    tree = ast.parse(Path(replay_intent.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported |= {node.module.split(".")[0]} | {alias.name for alias in node.names}
    assert imported.isdisjoint({"socket", "urllib", "http", "requests", "subprocess", "risk_gate", "executor",
                                "halt_store", "sentinel", "bus", "ledger_store", "paper_ledger"})


def test_the_only_write_is_the_atomic_result_publish():
    # Issue #80 acceptance 3: beside printing, the entry's one write is --out's atomic, no-clobber
    # publish (temp file, fsync, hardlink into place, temp removed on failure) — nothing else.
    tree = ast.parse(Path(replay_intent.__file__).read_text(encoding="utf-8"))
    whole = _called(tree)
    assert whole.isdisjoint({"open", "evaluate", "halt", "commit", "record_drift", "refuse_start"})
    assert not [name for name in whole if name.endswith(("write_text", "write_bytes", "mkdir", "touch"))]
    writes = _file_calls(whole)
    publishers = [node for node in ast.walk(tree)
                  if isinstance(node, ast.FunctionDef) and node.name == "publish_result"]
    assert len(publishers) == 1
    assert writes and writes == _file_calls(_called(publishers[0]))
