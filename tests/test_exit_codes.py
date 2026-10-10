"""Task AS (Sentinel design gap 4): docs/EXIT_CODES.md stays in sync with every entry point."""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from src import exit_codes

ROOT = Path(__file__).resolve().parents[1]
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


@pytest.fixture
def repo(tmp_path) -> Path:
    root = tmp_path / "repo"
    for folder in ("src", "scripts", "docs"):
        shutil.copytree(ROOT / folder, root / folder, ignore=_IGNORE)
    return root


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, (path, old)
    path.write_text(text.replace(old, new), encoding="utf-8")


def problems_with(repo: Path, needle: str) -> list[str]:
    problems = exit_codes.registry_problems(repo)
    assert problems, "expected a problem"
    hits = [p for p in problems if needle in p]
    assert hits, problems
    return problems


# ---------------------------------------------------------------- the real repo

def test_the_real_registry_matches_the_code():
    assert exit_codes.registry_problems(ROOT) == []


def test_every_entry_point_is_registered():
    found = exit_codes.entry_points(ROOT)
    assert len(found) == 23  # issue #80: + src/replay_intent.py; Task BR: + src/m2_start.py; Task BB: + src/reflector_proposals.py;  # Task AY: src/ws_hash_check.py; Task AZ: src/calibration.py
    assert {"src/scanner.py", "src/m1_run.py", "src/sentinel_tick.py", "scripts/guard.py",
            "scripts/e2e_offline.py"} <= set(found)
    sections, problems = exit_codes.parse_registry((ROOT / exit_codes.REGISTRY_REL).read_text(encoding="utf-8"))
    assert problems == [] and set(sections) == set(found)


def test_the_pin_drift_codes_are_global_halts():
    events, procs = exit_codes.sentinel_matrix(ROOT)
    for proc, code in [("scanner", 6), ("ws_recorder", 6), ("m1_run", 6), ("paper_cycle", 1),
                       ("loop_claim", 2), ("endgame_scan", 4), ("m1_run", 4), ("ws_recorder", 4),
                       ("resolutions", 6), ("resolutions", 5)]:
        assert events[(proc, code)] == "global halt", (proc, code)
    assert exit_codes.module_constants(ROOT, "src/loop_claim.py")["EXIT_USAGE"] == 5  # no longer argparse's 2


def test_resolutions_exit_4_is_split():
    # Task AV: one code per meaning; pin drift and a bad snapshot clock are both global halts
    sections, problems = exit_codes.parse_registry((ROOT / exit_codes.REGISTRY_REL).read_text(encoding="utf-8"))
    assert problems == []
    rows = {code: (name, word) for code, name, _, word in sections["src/resolutions.py"]}
    assert 4 not in rows
    assert rows[6] == ("EXIT_PIN_DRIFT", "global halt") and rows[5] == ("EXIT_BAD_CLOCK", "global halt")
    assert exit_codes.module_constants(ROOT, "src/resolutions.py") == {
        "EXIT_OK": 0, "EXIT_INCOMPLETE": 3, "EXIT_BAD_CLOCK": 5, "EXIT_PIN_DRIFT": 6}


def test_resolutions_exit_3_registers_the_history_failure():
    # issue #57: exit 3 also covers an unusable stored data/resolutions.jsonl; it stays an alert
    sections, problems = exit_codes.parse_registry((ROOT / exit_codes.REGISTRY_REL).read_text(encoding="utf-8"))
    assert problems == []
    rows = {code: (name, meaning, word) for code, name, meaning, word in sections["src/resolutions.py"]}
    name, meaning, word = rows[3]
    assert name == "EXIT_INCOMPLETE" and word == "alert"
    assert "paging" in meaning and "history" in meaning


def test_constants_resolve_through_imports():
    # m1_run imports its codes from ws_recorder
    consts = exit_codes.module_constants(ROOT, "src/m1_run.py")
    assert consts["EXIT_NO_WEBSOCKETS"] == 3 and consts["EXIT_PIN_DRIFT"] == 6 and consts["EXIT_INTERRUPTED"] == 5


@pytest.mark.parametrize("argv", [["--bogus"], ["status", "--bogus"]])
def test_argparse_errors_use_the_registered_usage_code(argv):
    from src import halt_store, loop_claim, paper_ledger

    assert halt_store.main(["--bogus"]) == halt_store.EXIT_USAGE
    assert paper_ledger.main(["--bogus"]) == paper_ledger.EXIT_USAGE
    assert loop_claim.main(argv) == loop_claim.EXIT_USAGE == 5


def test_help_exits_zero(capsys):
    from src import halt_store, loop_claim

    assert halt_store.main(["--help"]) == 0
    assert loop_claim.main(["--help"]) == 0


# ---------------------------------------------------------------- drift is caught

def test_an_undocumented_constant(repo):
    edit(repo / "src" / "book.py", "EXIT_NO_MARKETS = 2\n", "EXIT_NO_MARKETS = 2\nEXIT_SURPRISE = 7\n")
    problems_with(repo, "src/book.py: EXIT_SURPRISE=7 is not in docs/EXIT_CODES.md")


def test_a_changed_value(repo):
    edit(repo / "src" / "book.py", "EXIT_NO_MARKETS = 2\n", "EXIT_NO_MARKETS = 7\n")
    problems_with(repo, "EXIT_NO_MARKETS is 7 in code but 2 in")


def test_a_documented_code_the_module_lacks(repo):
    edit(repo / "docs" / "EXIT_CODES.md", "| 3 | `EXIT_ACTIVE` | status: at least one halt is active | - |",
         "| 3 | `EXIT_ACTIVE` | status: at least one halt is active | - |\n| 9 | `EXIT_GHOST` | never | - |")
    problems_with(repo, "src/halt_store.py EXIT_GHOST is not an EXIT_* constant")


def test_a_missing_section(repo):
    text = (repo / "docs" / "EXIT_CODES.md").read_text(encoding="utf-8")
    edit(repo / "docs" / "EXIT_CODES.md", "## `src/reflector.py`", "## reflector (heading broken)")
    assert "## `src/reflector.py`" in text
    problems_with(repo, "entry point src/reflector.py has no section")


def test_a_new_entry_point_needs_a_section(repo):
    (repo / "src" / "newtool.py").write_text("def main():\n    return 0\n\nif __name__ == '__main__':\n    main()\n",
                                            encoding="utf-8")
    problems = problems_with(repo, "entry point src/newtool.py has no section")
    assert any("newtool.py" in p and "literal" in p for p in problems)


def test_a_section_for_a_non_entry_point(repo):
    with (repo / "docs" / "EXIT_CODES.md").open("a", encoding="utf-8") as handle:
        handle.write("\n## `src/podlib.py`\n\n| Code | Name | Meaning | Sentinel |\n|---|---|---|---|\n")
    problems_with(repo, "src/podlib.py is not an entry point")


def test_a_literal_return(repo):
    edit(repo / "src" / "book.py", "return EXIT_NO_MARKETS", "return 2")
    problems_with(repo, "returns a literal exit code")


def test_a_literal_sys_exit(repo):
    edit(repo / "src" / "book.py", "return EXIT_NO_MARKETS", "sys.exit(2)")
    problems_with(repo, "literal exit code in sys.exit(2)")


def test_an_unguarded_parse_args(repo):
    edit(repo / "src" / "halt_store.py",
         "    try:\n        args = build_parser().parse_args(argv)\n    except SystemExit as exc:\n"
         "        return EXIT_OK if exc.code == 0 else EXIT_USAGE\n",
         "    args = build_parser().parse_args(argv)\n")
    problems_with(repo, "parse_args outside try/except SystemExit")


def test_parser_error(repo):
    edit(repo / "src" / "halt_store.py", "    if args.command == \"status\":\n",
         "    if args.command == \"nope\":\n        build_parser().error(\"nope\")\n    if args.command == \"status\":\n")
    problems_with(repo, "parser.error exits 2 outside the registry")


def test_a_wrong_sentinel_column(repo):
    edit(repo / "docs" / "EXIT_CODES.md", "| 3 | `EXIT_BUSY` | another surface holds the claim | alert |",
         "| 3 | `EXIT_BUSY` | another surface holds the claim | log |")
    problems_with(repo, "Sentinel column 'log', sentinel_tick says 'alert'")


def test_an_unknown_sentinel_word(repo):
    edit(repo / "docs" / "EXIT_CODES.md", "| 3 | `EXIT_BUSY` | another surface holds the claim | alert |",
         "| 3 | `EXIT_BUSY` | another surface holds the claim | panic |")
    problems_with(repo, "Sentinel column 'panic' is not one of")


def test_a_matrix_entry_without_a_code(repo):
    edit(repo / "src" / "sentinel_tick.py", '("paper_cycle", 1): GLOBAL,', '("paper_cycle", 1): GLOBAL, ("paper_cycle", 9): GLOBAL,')
    problems_with(repo, "EXIT_EVENTS (paper_cycle, 9) is not a documented code of src/paper_cycle.py")


def test_changing_the_matrix_breaks_the_registry(repo):
    edit(repo / "src" / "sentinel_tick.py", '("paper_cycle", 1): GLOBAL,', "")
    problems_with(repo, "src/paper_cycle.py 1 (EXIT_PIN): Sentinel column 'global halt', sentinel_tick says 'alert'")


def test_a_duplicate_code_name(repo):
    edit(repo / "src" / "book.py", "EXIT_NO_MARKETS = 2\n", "EXIT_NO_MARKETS = 2\nEXIT_EMPTY = 2\n")
    edit(repo / "docs" / "EXIT_CODES.md", "| 2 | `EXIT_NO_MARKETS` |", "| 2 | `EXIT_EMPTY` | empty | - |\n| 2 | `EXIT_NO_MARKETS` |")
    problems_with(repo, "src/book.py: code 2 has more than one name")


def test_a_missing_registry_fails_closed(repo):
    (repo / "docs" / "EXIT_CODES.md").unlink()
    problems_with(repo, "docs/EXIT_CODES.md unreadable")


def test_an_unparsable_module_fails_closed(repo):
    (repo / "src" / "book.py").write_text("def main(:\nif __name__ == '__main__':\n", encoding="utf-8")
    problems_with(repo, "src/book.py")


def test_the_checker_never_imports_the_modules(repo):
    # ast only: a module that would crash on import is still checked
    edit(repo / "src" / "book.py", "EXIT_NO_MARKETS = 2\n", "EXIT_NO_MARKETS = 2\nraise SystemExit('imported')\n")
    assert exit_codes.registry_problems(repo) == []
    proc = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); from src import exit_codes;"
                           "from pathlib import Path; print(exit_codes.registry_problems(Path(sys.argv[1])))", str(repo)],
                          capture_output=True, text=True, timeout=60)
    assert proc.stdout.strip() == "[]", proc.stdout + proc.stderr
