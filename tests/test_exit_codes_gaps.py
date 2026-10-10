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
