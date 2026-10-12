"""Task V: the skill security auditor stays inside the target and never turns an error into PASS.

Before: containment was ``str(target).startswith(str(root))``, so ``target-evil/.env`` counted as
inside ``target``; files behind outside symlinks were still read; read, stat and decode errors were
swallowed and the verdict could stay PASS. Offline, no credentials, tmp dirs only.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".grok" / "skills" / "cx-skill-security-auditor" / "scripts" / "skill_security_auditor.py"
MARKER = "LEAK_MARKER_7f3c"

_spec = importlib.util.spec_from_file_location("skill_security_auditor_under_test", SCRIPT)
auditor = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules[_spec.name] = auditor
_previous_flag = sys.dont_write_bytecode
sys.dont_write_bytecode = True  # leave no __pycache__ inside the skill directory
try:
    _spec.loader.exec_module(auditor)
finally:
    sys.dont_write_bytecode = _previous_flag


def _skill(base: Path, name: str = "target") -> Path:
    skill = base / name
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Demo skill\n\nReads its own files only.\n", encoding="utf-8")
    (skill / "scripts" / "tool.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    return skill


def _evil(base: Path) -> Path:
    evil = base / "target-evil"
    evil.mkdir()
    (evil / ".env").write_text(f"API_KEY={MARKER}\n", encoding="utf-8")
    # A line the code scanner would quote back into the report if it ever read this file.
    (evil / "leak.py").write_text(f"import os\nos.system('{MARKER}')\n", encoding="utf-8")
    return evil


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # Windows without the symlink privilege
        pytest.skip(f"symlinks unavailable here: {exc}")


def _dump(report) -> str:
    return json.dumps(report.to_dict(), ensure_ascii=False)


# --- containment --------------------------------------------------------------------------


def test_prefix_sibling_is_not_inside(tmp_path):
    skill = _skill(tmp_path)
    evil = _evil(tmp_path)
    assert auditor.is_within(skill / "scripts" / "tool.py", skill) is True
    assert auditor.is_within(skill, skill) is True
    assert auditor.is_within(evil / ".env", skill) is False
    assert auditor.is_within(evil, skill) is False
    assert auditor.is_within(tmp_path, skill) is False


def test_clean_skill_still_passes(tmp_path):
    report = auditor.scan_skill(_skill(tmp_path))
    assert report.verdict == "PASS", _dump(report)
    assert report.errors == [] and report.skipped == []
    assert report.scripts_scanned == 1 and report.md_files_scanned == 1


def test_scan_never_lists_a_prefix_sibling(tmp_path):
    skill = _skill(tmp_path)
    _evil(tmp_path)
    report = auditor.scan_skill(skill)
    assert report.verdict == "PASS"
    assert "target-evil" not in _dump(report) and MARKER not in _dump(report)


def test_symlink_into_prefix_sibling_is_skipped_and_fails(tmp_path):
    skill = _skill(tmp_path)
    evil = _evil(tmp_path)
    _symlink(skill / "config.txt", evil / ".env")
    _symlink(skill / "scripts" / "helper.py", evil / "leak.py")
    report = auditor.scan_skill(skill)
    dumped = _dump(report)
    assert report.verdict == "FAIL"
    assert sorted(report.skipped) == ["config.txt", str(Path("scripts") / "helper.py")]
    outside = [f for f in report.findings if f.category == "FS-SYMLINK"]
    assert {f.file for f in outside} == set(report.skipped)
    assert all(f.severity == auditor.Severity.CRITICAL for f in outside)
    assert MARKER not in dumped  # the outside file was never read
    assert report.scripts_scanned == 1


def test_symlinked_directory_escape_is_not_descended(tmp_path):
    skill = _skill(tmp_path)
    _evil(tmp_path)
    _symlink(skill / "vendor", tmp_path / "target-evil", directory=True)
    report = auditor.scan_skill(skill)
    assert report.verdict == "FAIL"
    assert report.skipped == ["vendor"]
    assert MARKER not in _dump(report)
    assert report.scripts_scanned == 1


def test_symlink_to_an_unrelated_dir_is_not_read(tmp_path):
    skill = _skill(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "secret.py").write_text(f"os.system('{MARKER}')\n", encoding="utf-8")
    _symlink(skill / "scripts" / "secret.py", other / "secret.py")
    report = auditor.scan_skill(skill)
    assert report.verdict == "FAIL"
    assert MARKER not in _dump(report)


def test_symlink_that_stays_inside_is_fine(tmp_path):
    skill = _skill(tmp_path)
    _symlink(skill / "scripts" / "alias.py", skill / "scripts" / "tool.py")
    report = auditor.scan_skill(skill)
    assert report.verdict == "PASS", _dump(report)
    assert report.skipped == []


# --- read / decode errors -------------------------------------------------------------------


def _fail_reads_of(monkeypatch, name: str) -> None:
    original_bytes = Path.read_bytes
    original_text = Path.read_text

    def read_bytes(self):
        if self.name == name:
            raise PermissionError(13, "Permission denied", str(self))
        return original_bytes(self)

    def read_text(self, *args, **kwargs):
        if self.name == name:
            raise PermissionError(13, "Permission denied", str(self))
        return original_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)
    monkeypatch.setattr(Path, "read_text", read_text)


@pytest.mark.parametrize("name", ["tool.py", "SKILL.md"])
def test_read_error_turns_into_fail(tmp_path, monkeypatch, name):
    skill = _skill(tmp_path)
    _fail_reads_of(monkeypatch, name)
    report = auditor.scan_skill(skill)
    assert report.verdict == "FAIL"
    assert report.errors and any(name in error and "PermissionError" in error for error in report.errors)
    data = report.to_dict()
    assert data["verdict"] == "FAIL" and data["errors"] == report.errors
    assert data["summary"]["errors"] == len(report.errors)


def test_requirements_read_error_turns_into_fail(tmp_path, monkeypatch):
    skill = _skill(tmp_path)
    (skill / "requirements.txt").write_text("requests==2.32.3\n", encoding="utf-8")
    _fail_reads_of(monkeypatch, "requirements.txt")
    report = auditor.scan_skill(skill)
    assert report.verdict == "FAIL"
    assert any("requirements.txt" in error for error in report.errors)


def test_decode_error_turns_into_fail(tmp_path):
    skill = _skill(tmp_path)
    (skill / "scripts" / "bad.py").write_bytes(b"x = 1\n\xff\xfe\xfa not utf-8\n")
    report = auditor.scan_skill(skill)
    assert report.verdict == "FAIL"
    assert any("bad.py" in error and "decode" in error for error in report.errors)


def test_stat_error_turns_into_fail(tmp_path, monkeypatch):
    skill = _skill(tmp_path)
    original = Path.stat

    def stat(self, *args, **kwargs):
        if self.name == "tool.py":
            raise PermissionError(13, "Permission denied", str(self))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    report = auditor.scan_skill(skill)
    assert report.verdict == "FAIL"
    assert any("tool.py" in error for error in report.errors)


def test_cli_exit_code_and_json_for_read_error(tmp_path):
    skill = _skill(tmp_path)
    (skill / "scripts" / "bad.py").write_bytes(b"\xff\xfe\xfa\n")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(skill), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 1, proc.stderr
    data = json.loads(proc.stdout)
    assert data["verdict"] == "FAIL" and data["errors"]


def test_cli_clean_skill_exit_zero(tmp_path):
    skill = _skill(tmp_path)
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(skill), "--json"],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    data = json.loads(proc.stdout)
    assert data["verdict"] == "PASS" and data["errors"] == [] and data["skipped"] == []
