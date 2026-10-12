"""Task AT: one assumed-slippage constant and a ROLES.md that matches RiskGate. Offline."""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

import src.intent_builder as intent_builder
import src.paper_ledger as paper_ledger
import src.sports_devig as sports_devig
import src.thresholds as thresholds

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "thresholds.py"
SLIPPAGE = 0.005  # the value every copy had before Task AT; unification moved no number


def _assignments(tree: ast.Module) -> dict[str, ast.expr]:
    return {
        target.id: node.value
        for node in tree.body if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Name)
    }


def test_one_source_of_truth_with_the_unchanged_value():
    assert thresholds.ASSUMED_SLIPPAGE == SLIPPAGE
    assert type(thresholds.ASSUMED_SLIPPAGE) is float
    assert thresholds.SPORTS_DEVIG_ASSUMED_SLIPPAGE == thresholds.ASSUMED_SLIPPAGE
    assert thresholds.INTENT_ASSUMED_SLIPPAGE == thresholds.ASSUMED_SLIPPAGE


def test_the_old_names_are_aliases_not_second_literals():
    values = _assignments(ast.parse(SOURCE.read_text(encoding="utf-8")))
    assert isinstance(values["ASSUMED_SLIPPAGE"], ast.Constant)
    for alias in ("SPORTS_DEVIG_ASSUMED_SLIPPAGE", "INTENT_ASSUMED_SLIPPAGE"):
        assert isinstance(values[alias], ast.Name) and values[alias].id == "ASSUMED_SLIPPAGE", alias
    slippage_literals = [name for name, value in values.items()
                         if "SLIPPAGE" in name and isinstance(value, ast.Constant)]
    assert slippage_literals == ["ASSUMED_SLIPPAGE"]


@pytest.mark.parametrize("alias", ["SPORTS_DEVIG_ASSUMED_SLIPPAGE", "INTENT_ASSUMED_SLIPPAGE"])
@pytest.mark.parametrize("value", ["0.004", "0.006", "0.0"])
def test_a_drifting_copy_fails_at_import(alias, value):
    source = SOURCE.read_text(encoding="utf-8")
    line = f"{alias} = ASSUMED_SLIPPAGE\n"
    assert source.count(line) == 1
    with pytest.raises(RuntimeError, match="thresholds inconsistent"):
        exec(compile(source.replace(line, f"{alias} = {value}\n"), "thresholds_edit", "exec"), {"__name__": "x"})


def _code_files() -> list[Path]:
    files = sorted((ROOT / "src").glob("*.py")) + sorted((ROOT / "scripts").glob("*.py"))
    return [p for p in files if p.name != "thresholds.py"]


@pytest.mark.parametrize("path", [p.relative_to(ROOT).as_posix() for p in _code_files()])
def test_no_code_file_hardcodes_the_slippage(path):
    """A literal 0.005 outside src/thresholds.py is a second copy that can drift."""
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    literals = [node.lineno for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and type(node.value) is float and node.value == SLIPPAGE]
    assert literals == [], (path, literals)


def test_every_user_of_the_slippage_reads_the_one_constant():
    assert sports_devig.ASSUMED_SLIPPAGE == thresholds.ASSUMED_SLIPPAGE
    assert intent_builder.INTENT_ASSUMED_SLIPPAGE == thresholds.ASSUMED_SLIPPAGE
    assert paper_ledger.ASSUMED_SLIPPAGE == thresholds.ASSUMED_SLIPPAGE
    args = paper_ledger.build_parser().parse_args([])
    assert args.slippage == thresholds.ASSUMED_SLIPPAGE
    assert paper_ledger._intent().assumed_slippage == thresholds.ASSUMED_SLIPPAGE


def _slippage_values(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "assumed_slippage":
                yield value
            else:
                yield from _slippage_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from _slippage_values(item)


def test_e2e_fixture_forecasts_use_the_one_constant():
    forecasts = json.loads((ROOT / "tests" / "fixtures" / "e2e" / "forecasts.json").read_text(encoding="utf-8"))
    values = list(_slippage_values(forecasts))
    assert values and all(v == thresholds.ASSUMED_SLIPPAGE for v in values), values


def test_doc_documents_the_aliases():
    doc = (ROOT / "docs" / "THRESHOLDS.md").read_text(encoding="utf-8")
    for alias in ("SPORTS_DEVIG_ASSUMED_SLIPPAGE", "INTENT_ASSUMED_SLIPPAGE"):
        row = next(line for line in doc.splitlines() if line.startswith(f"| `{alias}` |"))
        assert "alias of `assumed_slippage`" in row.lower(), alias
    assert "fixed 0.005 slippage" not in doc  # paper_ledger's self-check now reads ASSUMED_SLIPPAGE


# --- ROLES.md Kelly fix -------------------------------------------------------------------------

def _roles_row(role: str) -> str:
    text = (ROOT / "docs" / "ROLES.md").read_text(encoding="utf-8")
    return next(line for line in text.splitlines() if re.match(rf"\|\s*{role}\s*\|", line))


def test_roles_does_not_claim_riskgate_sizes_with_kelly():
    row = _roles_row("RiskGate")
    assert "Fractional Kelly," not in row
    assert "1%" in row
    assert "shadow" in row.lower() and "intent_builder" in row
    assert "never" in row.lower()


def test_riskgate_really_does_not_use_kelly_for_size():
    tree = ast.parse((ROOT / "src" / "risk_gate.py").read_text(encoding="utf-8"))
    used = [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == "kelly_fraction"]
    used += [node.lineno for node in ast.walk(tree)
             if isinstance(node, ast.Attribute) and node.attr == "kelly_fraction"]
    assert used == []