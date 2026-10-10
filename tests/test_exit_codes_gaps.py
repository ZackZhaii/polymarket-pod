"""Filler tests for src/exit_codes.py: the fail-closed branches of the registry checker itself.

tests/test_exit_codes.py covers the drift the checker must catch in the real repo (an undocumented
constant, a changed value, a wrong Sentinel column, a literal exit code in ``main``, ...). This file
covers the *parser and resolver* edges that decide what the checker even sees, on tiny synthetic
roots under tmp_path: a section heading that is not a section, a duplicated section, a row it cannot
read, a row with no meaning, tuple assignments, aliased and unresolvable imports, the reversed
``__main__`` guard, and the deliberate limits of ``_walk_own`` / ``_has_literal`` (a nested function
is not ``main``; ``return exc.code`` is not a literal). Nothing here imports the real repo, a
network or the real logs; every module is a few lines of text.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src import exit_codes

MINIMAL_MODULE = '''EXIT_OK = 0
EXIT_USAGE = 5

PARSER = None


def main(argv=None):
    try:
        PARSER.parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    return EXIT_OK


if __name__ == "__main__":
    main()
'''

MINIMAL_SENTINEL = '''GLOBAL = 0
ALERT = 1

EXIT_EVENTS = {
    ("tool", 5): ALERT,
}

PROC_SPECS = {
    "tool": "python src/tool.py",
}
'''

MINIMAL_REGISTRY = '''# Exit codes

## `src/tool.py`

| Code | Name | Meaning | Sentinel |
|---|---|---|---|
| 0 | `EXIT_OK` | success | - |
| 5 | `EXIT_USAGE` | bad usage | alert |
'''


def write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def minimal_repo(root: Path) -> Path:
    """One entry point (src/tool.py) with a section that matches it and the Sentinel matrix."""
    root.mkdir(parents=True, exist_ok=True)
    write(root, "src/tool.py", MINIMAL_MODULE)
    write(root, "src/sentinel_tick.py", MINIMAL_SENTINEL)
    write(root, "docs/EXIT_CODES.md", MINIMAL_REGISTRY)
    return root


def test_the_minimal_synthetic_repo_is_clean(tmp_path):
    root = minimal_repo(tmp_path / "repo")
    assert exit_codes.registry_problems(root) == []
    assert exit_codes.entry_points(root) == ["src/tool.py"]
    assert exit_codes.summary(root) == {"entry_points": 1, "codes": 2}


def test_a_duplicate_section_is_reported_and_only_the_second_rows_count():
    sections, problems = exit_codes.parse_registry(
        "## `src/tool.py`\n"
        "| 0 | `EXIT_OK` | success | - |\n"
        "\n"
        "## `src/tool.py`\n"
        "| 5 | `EXIT_USAGE` | bad usage | - |\n")
    assert problems == ["docs/EXIT_CODES.md:4: src/tool.py listed twice"]
    # the second heading resets the section: the erased rows can no longer hide a drift
    assert sections["src/tool.py"] == [(5, "EXIT_USAGE", "bad usage", "-")]


def test_unreadable_rows_and_a_row_without_a_meaning_are_reported():
    sections, problems = exit_codes.parse_registry(
        "## `src/tool.py`\n"
        "| Code | Name | Meaning | Sentinel |\n"
        "|---|---|---|---|\n"
        "| 2 | EXIT_BAD |  | - |\n"
        "| 3 | not-a-name | broken | alert |\n")
    assert problems == ["docs/EXIT_CODES.md:4: src/tool.py EXIT_BAD has no meaning",
                        "docs/EXIT_CODES.md:5: unreadable row in src/tool.py"]
    assert sections["src/tool.py"] == [(2, "EXIT_BAD", "", "-")]


def test_a_plain_heading_ends_the_section_and_its_rows_are_dropped():
    # a heading that is not a section is not a place to hide rows: they fall out of the registry
    sections, problems = exit_codes.parse_registry(
        "## `src/tool.py`\n"
        "| 0 | `EXIT_OK` | success | - |\n"
        "## Notes\n"
        "| 5 | `EXIT_USAGE` | bad usage | - |\n")
    assert problems == []
    assert sections == {"src/tool.py": [(0, "EXIT_OK", "success", "-")]}


def test_module_constants_reads_tuples_and_aliases_and_refuses_what_it_cannot_resolve(tmp_path):
    write(tmp_path, "src/base.py", 'EXIT_OK = 0\nEXIT_BOOL = True\nEXIT_TEXT = "3"\nEXIT_FLOAT = 3.0\n')
    write(tmp_path, "src/multi.py", "EXIT_OK, EXIT_USAGE = 0, 5\n")
    assert exit_codes.module_constants(tmp_path, "src/multi.py") == {"EXIT_OK": 0, "EXIT_USAGE": 5}
    # bools, strings and floats are not exit codes: they must not be read as int constants
    assert exit_codes.module_constants(tmp_path, "src/base.py") == {"EXIT_OK": 0}

    write(tmp_path, "src/aliased.py", "from src.base import EXIT_OK as EXIT_FINE\n")
    assert exit_codes.module_constants(tmp_path, "src/aliased.py") == {"EXIT_FINE": 0}

    write(tmp_path, "src/ghost.py", "from src.base import EXIT_MISSING\n")
    with pytest.raises(ValueError, match="EXIT_MISSING is not an int constant of src.base"):
        exit_codes.module_constants(tmp_path, "src/ghost.py")

    write(tmp_path, "src/absent.py", "from src.nowhere import EXIT_OK\n")
    with pytest.raises(ValueError, match="cannot resolve src.nowhere"):
        exit_codes.module_constants(tmp_path, "src/absent.py")

    write(tmp_path, "src/loop_a.py", "from src.loop_b import EXIT_OK\n")
    write(tmp_path, "src/loop_b.py", "from src.loop_a import EXIT_OK\n")
    with pytest.raises(ValueError, match="cannot resolve src.loop_a"):  # a cycle, not a recursion
        exit_codes.module_constants(tmp_path, "src/loop_a.py")


def test_a_module_with_an_unresolvable_import_fails_the_registry_closed(tmp_path):
    root = minimal_repo(tmp_path / "repo")
    module = root / "src" / "tool.py"
    module.write_text(module.read_text(encoding="utf-8").replace(
        "EXIT_USAGE = 5", "from src.absent import EXIT_USAGE"), encoding="utf-8")
    problems = exit_codes.registry_problems(root)
    # the module's constants cannot be read, so they are not compared: the check says so instead
    assert any("src/tool.py: unreadable: ValueError" in p and "cannot resolve src.absent" in p
               for p in problems)


def test_a_nested_function_is_not_main_but_a_literal_exit_inside_it_is_still_flagged(tmp_path):
    write(tmp_path, "src/nested.py",
          "def main():\n"
          "    def helper():\n"
          "        return 2\n"
          "    return helper()\n")
    assert exit_codes.code_problems(tmp_path, "src/nested.py") == []

    write(tmp_path, "src/nested_exit.py",
          "import sys\n"
          "\n"
          "\n"
          "def main():\n"
          "    def helper():\n"
          "        sys.exit(3)\n"
          "    return helper()\n")
    assert exit_codes.code_problems(tmp_path, "src/nested_exit.py") == [
        "src/nested_exit.py:6: literal exit code in sys.exit(3)"]


@pytest.mark.parametrize("handler,guarded", [
    ("except SystemExit as exc:", True),
    ("except BaseException:", True),
    ("except (OSError, SystemExit):", True),
    ("except:", True),
    ("except ValueError:", False),
])
def test_only_a_systemexit_handler_guards_parse_args(tmp_path, handler, guarded):
    write(tmp_path, "src/g.py",
          "EXIT_OK = 0\n"
          "\n"
          "PARSER = None\n"
          "\n"
          "\n"
          "def main(argv=None):\n"
          "    try:\n"
          "        PARSER.parse_args(argv)\n"
          f"    {handler}\n"
          "        pass\n"
          "    return EXIT_OK\n")
    problems = exit_codes.code_problems(tmp_path, "src/g.py")
    assert problems == ([] if guarded else [
        "src/g.py:8: parse_args outside try/except SystemExit "
        "(argparse would exit 2 outside the registry)"])


def test_the_reversed_main_guard_is_an_entry_point(tmp_path):
    write(tmp_path, "src/rev.py",
          'EXIT_OK = 0\n\n\ndef main():\n    return EXIT_OK\n\n\nif "__main__" == __name__:\n    main()\n')
    write(tmp_path, "src/lib.py", "EXIT_OK = 0\n")
    assert exit_codes.entry_points(tmp_path) == ["src/rev.py"]


def test_a_corrupt_sentinel_matrix_or_an_unregistered_processor_fails_closed(tmp_path):
    corrupt = minimal_repo(tmp_path / "corrupt")
    write(corrupt, "src/sentinel_tick.py", "EXIT_EVENTS = {}\n")
    assert any("PROC_SPECS not found" in p for p in exit_codes.registry_problems(corrupt))

    ghost = minimal_repo(tmp_path / "ghost")
    sentinel = ghost / "src" / "sentinel_tick.py"
    sentinel.write_text(sentinel.read_text(encoding="utf-8").replace(
        '("tool", 5): ALERT,', '("tool", 5): ALERT,\n    ("ghost", 4): ALERT,'), encoding="utf-8")
    problems = exit_codes.registry_problems(ghost)
    assert any("EXIT_EVENTS names ghost, which has no section in docs/EXIT_CODES.md" in p
               for p in problems)


# --- the Sentinel column: what the registry may claim, and what it may not -------------------------

SCRIPT_MODULE = '''EXIT_OK = 0


def main():
    return EXIT_OK


if __name__ == "__main__":
    main()
'''


UNWATCHING_SENTINEL = '''EXIT_EVENTS = {}

PROC_SPECS = {
    "tool": "python src/tool.py",
}
'''


def script_repo(root: Path, sentinel_word: str) -> Path:
    """One entry point under scripts/, where the Sentinel watches nothing (PROC_SPECS is keyed by src/)."""
    write(root, "src/sentinel_tick.py", UNWATCHING_SENTINEL)
    write(root, "scripts/tool.py", SCRIPT_MODULE)
    write(root, "docs/EXIT_CODES.md",
          "## `scripts/tool.py`\n"
          "\n"
          "| Code | Name | Meaning | Sentinel |\n"
          "|---|---|---|---|\n"
          f"| 0 | `EXIT_OK` | success | {sentinel_word} |\n")
    return root


def test_a_scripts_entry_point_can_only_be_documented_as_unwatched(tmp_path):
    # the Sentinel's PROC_SPECS names src/ processors only, so "-" is the only honest column here
    assert exit_codes.registry_problems(script_repo(tmp_path / "clean", "-")) == []
    assert exit_codes.registry_problems(script_repo(tmp_path / "watched", "alert")) == [
        "docs/EXIT_CODES.md: scripts/tool.py 0 (EXIT_OK): Sentinel column 'alert', sentinel_tick says '-'"]


SCANNER_MODULE = '''EXIT_OK = 0
EXIT_INCOMPLETE = 3


def main():
    return EXIT_INCOMPLETE


if __name__ == "__main__":
    main()
'''

SCANNER_SENTINEL = '''GLOBAL = 0
ALERT = 1

EXIT_EVENTS = {
    ("scanner", 3): ALERT,
}

PROC_SPECS = {
    "scanner": "python src/scanner.py",
}
'''


def scanner_repo(root: Path, sentinel_word: str) -> Path:
    write(root, "src/sentinel_tick.py", SCANNER_SENTINEL)
    write(root, "src/scanner.py", SCANNER_MODULE)
    write(root, "docs/EXIT_CODES.md",
          "## `src/scanner.py`\n"
          "\n"
          "| Code | Name | Meaning | Sentinel |\n"
          "|---|---|---|---|\n"
          "| 0 | `EXIT_OK` | ok | - |\n"
          f"| 3 | `EXIT_INCOMPLETE` | no complete market | {sentinel_word} |\n")
    return root


def test_scanner_3_is_alert_x2_and_a_plain_alert_row_is_reported(tmp_path):
    # the matrix says ALERT, but the Sentinel only alerts after N in a row: the registry must say so
    assert exit_codes.expected_sentinel("scanner", 3, {("scanner", 3): "alert"}, {"scanner"}) == "alert x2"
    assert exit_codes.expected_sentinel("scanner", 0, {("scanner", 3): "alert"}, {"scanner"}) == "-"
    assert exit_codes.expected_sentinel("watched_but_absent", 4, {}, {"other"}) == "-"
    assert exit_codes.registry_problems(scanner_repo(tmp_path / "clean", "alert x2")) == []
    assert exit_codes.registry_problems(scanner_repo(tmp_path / "plain", "alert")) == [
        "docs/EXIT_CODES.md: src/scanner.py 3 (EXIT_INCOMPLETE): Sentinel column 'alert', "
        "sentinel_tick says 'alert x2'"]


def test_an_event_word_outside_global_alert_log_fails_the_whole_matrix_closed(tmp_path):
    # a word the Sentinel does not define must stop the check, not silently become "alert"
    root = tmp_path / "repo"
    write(root, "src/sentinel_tick.py",
          MINIMAL_SENTINEL.replace("ALERT = 1", "ALERT = 1\nBANANA = 2").replace(
              '("tool", 5): ALERT,', '("tool", 5): BANANA,'))
    write(root, "src/tool.py", MINIMAL_MODULE)
    write(root, "docs/EXIT_CODES.md", MINIMAL_REGISTRY)
    assert exit_codes.registry_problems(root) == [
        "src/sentinel_tick.py: EXIT_EVENTS / PROC_SPECS unreadable: KeyError: 'BANANA'"]


def test_an_annotated_matrix_is_still_read_and_still_checked(tmp_path):
    root = minimal_repo(tmp_path / "repo")
    sentinel = root / "src" / "sentinel_tick.py"
    sentinel.write_text(sentinel.read_text(encoding="utf-8").replace(
        "EXIT_EVENTS = {", "EXIT_EVENTS: dict = {").replace("PROC_SPECS = {", "PROC_SPECS: dict = {"),
        encoding="utf-8")
    assert exit_codes.registry_problems(root) == []
    registry = root / "docs" / "EXIT_CODES.md"
    registry.write_text(registry.read_text(encoding="utf-8").replace(
        "| 5 | `EXIT_USAGE` | bad usage | alert |", "| 5 | `EXIT_USAGE` | bad usage | log |"),
        encoding="utf-8")
    assert exit_codes.registry_problems(root) == [
        "docs/EXIT_CODES.md: src/tool.py 5 (EXIT_USAGE): Sentinel column 'log', sentinel_tick says 'alert'"]


# --- literal exit codes, in every spelling ---------------------------------------------------------

def test_every_literal_exit_spelling_is_caught_and_a_named_code_is_not(tmp_path):
    write(tmp_path, "src/ways.py",
          "import os\n"
          "import sys\n"
          "\n"
          "EXIT_OK = 0\n"
          "EXIT_USAGE = 5\n"
          "\n"
          "\n"
          "def main(argv=None):\n"
          "    os._exit(2)\n"
          "    raise SystemExit(3)\n"
          "    quit(4)\n"
          "    exit(5)\n"
          "    sys.exit(EXIT_USAGE)\n"
          "    return EXIT_OK\n")
    assert exit_codes.code_problems(tmp_path, "src/ways.py") == [
        "src/ways.py:9: literal exit code in os._exit(2)",
        "src/ways.py:10: literal exit code in SystemExit(3)",
        "src/ways.py:11: literal exit code in quit(4)",
        "src/ways.py:12: literal exit code in exit(5)"]


def test_a_negative_code_cannot_be_smuggled_into_the_registry(tmp_path):
    root = minimal_repo(tmp_path / "repo")
    registry = root / "docs" / "EXIT_CODES.md"
    registry.write_text(registry.read_text(encoding="utf-8") +
                        "| -1 | `EXIT_NEG` | never | alert |\n", encoding="utf-8")
    # a documented row the module cannot back with an int constant is a problem, negative or not
    assert exit_codes.registry_problems(root) == [
        "docs/EXIT_CODES.md: src/tool.py EXIT_NEG is not an EXIT_* constant of the module"]
    # and the module side cannot express one either: -1 is a UnaryOp, not an int constant
    write(root, "src/neg.py", "EXIT_NEG = -1\nEXIT_ZERO = 0\n")
    assert exit_codes.module_constants(root, "src/neg.py") == {"EXIT_ZERO": 0}
