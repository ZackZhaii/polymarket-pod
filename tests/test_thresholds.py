"""Task AC: src/thresholds.py, docs/THRESHOLDS.md and the old constant names stay in sync. Offline."""
from __future__ import annotations

import ast
import importlib
import re
from pathlib import Path

import pytest

import src.thresholds as thresholds
from src.podlib import RISK_PATH, parse_risk_toml

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "THRESHOLDS.md"
SOURCE = ROOT / "src" / "thresholds.py"

# Frozen values: exactly what the modules defined before Task AC. A change here is a threshold change.
EXPECTED = {
    "MAX_BOOK_AGE_S": 120.0,
    "MIN_EDGE": 0.02,
    "EDGE_TOLERANCE": 1e-12,
    "PAPER_EQUITY_USD": 1000.0,
    "PAPER_CYCLE_STALE_AFTER_S": 120,
    "SENTINEL_STALE_S": 120.0,
    "SENTINEL_STALE_CONSECUTIVE": 2,
    "WASH_WINDOW_S": 300.0,
    "MIN_HOURS_TO_EXPIRY": 24.0,
    "SCANNER_MIN_PRICE": 0.03,
    "SCANNER_MAX_PRICE": 0.97,
    "MIN_SCANNED_MARKETS": 5,
    "ENDGAME_PRICE_MIN": 0.98,
    "ENDGAME_PRICE_MAX": 0.99,
    "ENDGAME_WINDOW_HOURS": 48.0,
    "SPORTS_DEVIG_ASSUMED_SLIPPAGE": 0.005,
    "LEDGER_OPEN_EPS": 1e-9,
    "REFLECTOR_MAX_PER_UTC_DAY": 3,
    # Task AG: new WebSocket recorder timing (not moved from anywhere; first values).
    "WS_BACKOFF_BASE_S": 1.0,
    "WS_BACKOFF_CAP_S": 60.0,
    "WS_RESYNC_TIMEOUT_S": 30.0,
    "WS_PING_INTERVAL_S": 10.0,
    "WS_ROW_REFRESH_S": 30.0,
}

# Old import paths that callers and tests use; each must stay the very same object.
REEXPORTS = {
    "book_staleness.MAX_BOOK_AGE_S": "MAX_BOOK_AGE_S",
    "risk_gate.MAX_BOOK_AGE_S": "MAX_BOOK_AGE_S",
    "sentinel.MAX_BOOK_AGE_S": "MAX_BOOK_AGE_S",
    "risk_gate.MIN_EDGE": "MIN_EDGE",
    "risk_gate.EDGE_TOLERANCE": "EDGE_TOLERANCE",
    "podlib.PAPER_EQUITY_USD": "PAPER_EQUITY_USD",
    "risk_gate.PAPER_EQUITY_USD": "PAPER_EQUITY_USD",
    "maker.PAPER_EQUITY_USD": "PAPER_EQUITY_USD",
    "paper_cycle.STALE_AFTER_S": "PAPER_CYCLE_STALE_AFTER_S",
    "sentinel.SENTINEL_STALE_S": "SENTINEL_STALE_S",
    "sentinel.SENTINEL_STALE_CONSECUTIVE": "SENTINEL_STALE_CONSECUTIVE",
    "wash_detect.WASH_WINDOW_S": "WASH_WINDOW_S",
    "expiry_rule.MIN_HOURS_TO_EXPIRY": "MIN_HOURS_TO_EXPIRY",
    "scanner.MIN_HOURS_TO_EXPIRY": "MIN_HOURS_TO_EXPIRY",
    "endgame_scan.MIN_HOURS_TO_EXPIRY": "MIN_HOURS_TO_EXPIRY",
    "scanner.MIN_PRICE": "SCANNER_MIN_PRICE",
    "scanner.MAX_PRICE": "SCANNER_MAX_PRICE",
    "scanner.MIN_SCANNED_MARKETS": "MIN_SCANNED_MARKETS",
    "endgame_scan.MIN_SCANNED_MARKETS": "MIN_SCANNED_MARKETS",
    "paper_session.SESSION_MIN_SCANNED": "MIN_SCANNED_MARKETS",
    "endgame.PRICE_MIN": "ENDGAME_PRICE_MIN",
    "endgame.PRICE_MAX": "ENDGAME_PRICE_MAX",
    "endgame.WINDOW_HOURS": "ENDGAME_WINDOW_HOURS",
    "sports_devig.ASSUMED_SLIPPAGE": "SPORTS_DEVIG_ASSUMED_SLIPPAGE",
    "paper_ledger.OPEN_EPS": "LEDGER_OPEN_EPS",
    "reflector.MAX_PER_UTC_DAY": "REFLECTOR_MAX_PER_UTC_DAY",
}

M2_PENDING = {"MAX_BOOK_AGE_S", "MIN_EDGE", "SENTINEL_STALE_S", "SENTINEL_STALE_CONSECUTIVE", "WASH_WINDOW_S",
              "MIN_HOURS_TO_EXPIRY", "WS_ROW_REFRESH_S"}
HEADER = ["Name", "Value", "Unit", "Meaning", "Where", "Source", "M2 pending"]


def _resolve(dotted: str):
    module, name = dotted.split(".")
    return getattr(importlib.import_module(f"src.{module}"), name)


def _tables() -> dict[str, list[dict[str, str]]]:
    """Section heading -> rows of the markdown table under it."""
    tables: dict[str, list[dict[str, str]]] = {}
    section = ""
    header: list[str] | None = None
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            section, header = line[3:].strip(), None
            continue
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if header is None:
            header = cells
            assert header == HEADER, (section, header)
            tables[section] = []
        elif set("".join(cells)) <= set("-: "):
            continue
        else:
            assert len(cells) == len(header), (section, line)
            tables[section].append(dict(zip(header, cells)))
    return tables


def _table(prefix: str) -> list[dict[str, str]]:
    matches = [rows for name, rows in _tables().items() if name.startswith(prefix)]
    assert len(matches) == 1, prefix
    return matches[0]


def _code(cell: str) -> str:
    match = re.fullmatch(r"`([^`]+)`", cell)
    assert match, cell
    return match.group(1)


def _module_constants() -> dict[str, object]:
    return {
        name: value
        for name, value in vars(thresholds).items()
        if name.isupper() and not name.startswith("_")
        and isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def test_values_equal_the_constants_they_replace():
    assert _module_constants() == EXPECTED
    for name, value in EXPECTED.items():
        assert type(getattr(thresholds, name)) is type(value), name


@pytest.mark.parametrize("dotted", sorted(REEXPORTS))
def test_old_names_still_work_and_are_the_same_value(dotted):
    name = REEXPORTS[dotted]
    assert _resolve(dotted) is getattr(thresholds, name)
    assert _resolve(dotted) == EXPECTED[name]


def test_doc_lists_every_module_constant_once_with_its_value():
    rows = _table("Code constants")
    names = [_code(row["Name"]) for row in rows]
    assert len(names) == len(set(names))
    assert set(names) == set(_module_constants())
    for row in rows:
        name = _code(row["Name"])
        value = ast.literal_eval(_code(row["Value"]))
        assert value == getattr(thresholds, name) and type(value) is type(getattr(thresholds, name)), name
        assert row["Unit"] and row["Meaning"] and row["Source"], name
        assert row["Where"].startswith("`src/thresholds.py`"), name


def test_doc_reexports_match_the_code():
    documented = {}
    for row in _table("Code constants"):
        name = _code(row["Name"])
        for dotted in re.findall(r"`([a-z_]+\.[A-Z_0-9]+)`", row["Where"]):
            documented[dotted] = name
    assert documented == REEXPORTS


def test_doc_m2_column_matches_the_pending_list():
    rows = _table("Code constants")
    pending = {_code(row["Name"]) for row in rows if row["M2 pending"] == "yes"}
    assert pending == M2_PENDING
    for row in rows:
        assert row["M2 pending"] == "yes" or row["M2 pending"].startswith("no"), row["Name"]


def test_operational_rows_match_their_modules():
    rows = _table("Operational limits")
    assert rows
    for row in rows:
        dotted = _code(row["Name"])
        value = ast.literal_eval(_code(row["Value"]))
        actual = _resolve(dotted)
        assert actual == value and type(actual) is type(value), dotted
        assert row["Where"] == f"`src/{dotted.split('.')[0]}.py`", dotted
        assert not hasattr(thresholds, dotted.split(".")[1]), dotted


def test_risk_toml_rows_match_the_file_and_are_read_only():
    before = RISK_PATH.read_bytes()
    parsed = parse_risk_toml(before.decode("utf-8"))
    rows = _table("config/risk.toml")
    names = [_code(row["Name"]) for row in rows]
    assert sorted(names) == sorted(parsed)
    for row in rows:
        name = _code(row["Name"])
        documented = parse_risk_toml(f"{name} = {_code(row['Value'])}\n")[name]
        assert documented == parsed[name] and type(documented) is type(parsed[name]), name
        assert row["Where"] == f"`risk.toml: {name}`"
        assert row["M2 pending"] == "file-governed"
        assert not hasattr(thresholds, name.upper()), name  # risk.toml parameters are not moved into code
    assert parsed["live_enabled"] is False
    assert RISK_PATH.read_bytes() == before


def test_doc_names_the_pinned_risk_sha():
    text = DOC.read_text(encoding="utf-8")
    assert "0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04" in text


def test_relations_hold():
    assert thresholds.consistency_problems() == []
    assert thresholds.SENTINEL_STALE_S == thresholds.MAX_BOOK_AGE_S
    assert thresholds.PAPER_CYCLE_STALE_AFTER_S == thresholds.MAX_BOOK_AGE_S


@pytest.mark.parametrize(
    ("line", "replacement"),
    [
        ("SENTINEL_STALE_S = 120.0", "SENTINEL_STALE_S = 60.0"),
        ("MAX_BOOK_AGE_S = 120.0", "MAX_BOOK_AGE_S = 300.0"),
        ("PAPER_CYCLE_STALE_AFTER_S = 120", "PAPER_CYCLE_STALE_AFTER_S = 300"),
        ("SENTINEL_STALE_CONSECUTIVE = 2", "SENTINEL_STALE_CONSECUTIVE = 0"),
        ("SENTINEL_STALE_CONSECUTIVE = 2", "SENTINEL_STALE_CONSECUTIVE = 1.5"),
        ("SCANNER_MIN_PRICE = 0.03", "SCANNER_MIN_PRICE = 0.97"),
        ("ENDGAME_PRICE_MIN = 0.98", "ENDGAME_PRICE_MIN = 0.96"),
        ("ENDGAME_PRICE_MAX = 0.99", "ENDGAME_PRICE_MAX = 1.0"),
        ("MIN_HOURS_TO_EXPIRY = 24.0", "MIN_HOURS_TO_EXPIRY = 48.0"),
        ("WS_BACKOFF_CAP_S = 60.0", "WS_BACKOFF_CAP_S = 0.5"),
        ("WS_BACKOFF_BASE_S = 1.0", "WS_BACKOFF_BASE_S = 0.0"),
        ("WS_PING_INTERVAL_S = 10.0", "WS_PING_INTERVAL_S = 30.0"),
        ("WS_ROW_REFRESH_S = 30.0", "WS_ROW_REFRESH_S = 120.0"),
    ],
)
def test_an_inconsistent_edit_fails_at_import(line, replacement):
    source = SOURCE.read_text(encoding="utf-8")
    assert source.count(line + "\n") == 1
    with pytest.raises(RuntimeError, match="thresholds inconsistent"):
        exec(compile(source.replace(line + "\n", replacement + "\n"), "thresholds_edit", "exec"), {"__name__": "x"})


def test_sentinel_keeps_its_own_guard():
    source = (ROOT / "src" / "sentinel.py").read_text(encoding="utf-8")
    assert "if SENTINEL_STALE_S != MAX_BOOK_AGE_S:" in source


def test_thresholds_module_has_no_imports_and_reads_nothing():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imports = [node for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))]
    assert [(node.module, [alias.name for alias in node.names]) for node in imports] == [("__future__", ["annotations"])]
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    assert not names & {"open", "load_risk", "RISK_PATH", "Path", "environ"}


OLD_LITERAL_NAMES = {
    "MAX_BOOK_AGE_S", "MIN_EDGE", "EDGE_TOLERANCE", "PAPER_EQUITY_USD", "STALE_AFTER_S", "SENTINEL_STALE_S",
    "SENTINEL_STALE_CONSECUTIVE", "WASH_WINDOW_S", "MIN_HOURS_TO_EXPIRY", "MIN_PRICE", "MAX_PRICE", "PRICE_MIN",
    "PRICE_MAX", "WINDOW_HOURS", "ASSUMED_SLIPPAGE", "OPEN_EPS", "MAX_PER_UTC_DAY", "SESSION_MIN_SCANNED",
    "MIN_SCANNED_MARKETS",
} | set(EXPECTED)


@pytest.mark.parametrize("path", sorted(p.name for p in (ROOT / "src").glob("*.py") if p.name != "thresholds.py"))
def test_no_module_defines_a_gathered_threshold_again(path):
    """A second literal definition would let the copies drift apart."""
    tree = ast.parse((ROOT / "src" / path).read_text(encoding="utf-8"))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            assert not (isinstance(target, ast.Name) and target.id in OLD_LITERAL_NAMES), (path, target.id)
    text = (ROOT / "src" / path).read_text(encoding="utf-8")
    assert '["scanned"] < 5' not in text
