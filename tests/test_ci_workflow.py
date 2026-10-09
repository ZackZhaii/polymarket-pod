"""Task AM: GitHub Actions workflow scope and SHA pins. Offline, no network."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows" / "ci.yml"
SHA = re.compile(r"^[0-9a-f]{40}$")
USES = re.compile(r"^\s+uses:\s+(\S+)", re.MULTILINE)


def test_ci_workflow_runs_guard_and_pytest_on_dev_and_main():
    text = WF.read_text(encoding="utf-8")
    assert "permissions:" in text
    assert "contents: read" in text
    assert "python scripts/guard.py --require-git" in text
    assert "python -m pytest" in text
    assert "secrets:" not in text
    assert "workflow_dispatch" not in text
    assert "schedule:" not in text
    assert "persist-credentials: false" in text
    assert re.search(r"pull_request:\s+branches:", text)
    assert re.search(r"push:\s+branches:", text)
    for name in ("dev", "main"):
        assert re.search(rf"^\s+-\s+{name}\s*$", text, re.MULTILINE)


def test_ci_workflow_pins_every_action_to_a_full_commit_sha():
    text = WF.read_text(encoding="utf-8")
    specs = USES.findall(text)
    assert specs, "workflow must use at least one action"
    for spec in specs:
        assert "@" in spec, spec
        _name, sha = spec.split("@", 1)
        sha = sha.split("#", 1)[0].strip()
        assert SHA.fullmatch(sha), spec
        assert not re.search(r"@v\d", spec)
