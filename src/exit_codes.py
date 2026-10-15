"""Task AS (Poly哨兵 design gap 4): the exit-code registry docs/EXIT_CODES.md and its checker.

Exit codes collide across modules (scanner 2 = no market passed, loop_claim 2 = pin drift), so a code
only means something together with its module. docs/EXIT_CODES.md lists, for every entry point (a
file under src/ or scripts/ with ``if __name__ == "__main__":``), each code, its EXIT_* name, its
meaning and what the resident Sentinel (src/sentinel_tick.py) does with it.

``registry_problems(root)`` (used by tests/test_exit_codes.py and the guard check ``exit_codes``)
reads the code with ``ast`` only (nothing is imported or run) and fails when:

- an entry point has no section, or a section names a file that is not an entry point;
- the documented (name, code) rows differ from the module's EXIT_* constants (assigned in the module
  or imported from another src module), either way, or one code has two names in one module;
- ``main`` / ``_main`` returns an integer literal, ``sys.exit`` / ``SystemExit`` / ``exit`` /
  ``os._exit`` gets one, ``parser.error`` is called, or ``parse_args`` runs outside a ``try`` that
  catches ``SystemExit`` (argparse would exit 2 behind the registry's back);
- the Sentinel column disagrees with sentinel_tick.EXIT_EVENTS / PROC_SPECS (``global halt``,
  ``alert``, ``alert x2`` for scanner 3, ``log``; ``-`` for 0 and for modules the Sentinel does not
  watch), or an EXIT_EVENTS entry has no documented row.
Stdlib only; read only.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

REGISTRY_REL = "docs/EXIT_CODES.md"
SENTINEL_REL = "src/sentinel_tick.py"
ENTRY_DIRS = ("src", "scripts")
EXIT_NAME = re.compile(r"^EXIT_[A-Z0-9_]+$")
SECTION = re.compile(r"^## `((?:src|scripts)/[A-Za-z0-9_]+\.py)`\s*$")
ROW = re.compile(r"^\|\s*(-?\d+)\s*\|\s*`?(EXIT_[A-Z0-9_]+)`?\s*\|(.*)\|\s*(.+?)\s*\|\s*$")
ACTION_WORDS = {"GLOBAL": "global halt", "ALERT": "alert", "LOG": "log"}
SENTINEL_WORDS = frozenset({"global halt", "alert", "alert x2", "log", "-", "不可见"})
# Issue #245 / PR #338 (Poly风控 6180433724): m2_runner returns these before _run creates its Heartbeat,
# so no heartbeat carries the code and the resident Sentinel never sees the exit ("不可见" = invisible).
PRE_HEARTBEAT_EXITS = frozenset({("m2_runner", 2), ("m2_runner", 3), ("m2_runner", 7)})
EXIT_CALLS = frozenset({"sys.exit", "SystemExit", "exit", "os._exit", "quit"})


def _is_main_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    text = ast.unparse(node.test).replace("'", '"')
    return text in ('__name__ == "__main__"', '"__main__" == __name__')


def entry_points(root: Path) -> list[str]:
    found = []
    for folder in ENTRY_DIRS:
        for path in sorted((Path(root) / folder).glob("*.py")):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError):
                found.append(f"{folder}/{path.name}")  # unreadable: checked (and failed) below
                continue
            if any(_is_main_guard(node) for node in tree.body):
                found.append(f"{folder}/{path.name}")
    return found


def _int_value(node: ast.AST) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return node.value
    return None


def module_constants(root: Path, rel: str, _seen: frozenset[str] = frozenset()) -> dict[str, int]:
    """EXIT_* names bound at module level: ``X = 3``, ``A, B = 0, 1`` or ``from src.m import X``."""
    tree = ast.parse((Path(root) / rel).read_text(encoding="utf-8"))
    out: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
            if isinstance(target, ast.Name) and EXIT_NAME.match(target.id) and _int_value(value) is not None:
                out[target.id] = _int_value(value)
            elif isinstance(target, ast.Tuple) and isinstance(value, ast.Tuple) and len(target.elts) == len(value.elts):
                for name, item in zip(target.elts, value.elts):
                    if isinstance(name, ast.Name) and EXIT_NAME.match(name.id) and _int_value(item) is not None:
                        out[name.id] = _int_value(item)
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("src."):
            names = [alias for alias in node.names if EXIT_NAME.match(alias.asname or alias.name)]
            if not names:
                continue
            source = "src/" + node.module.split(".", 1)[1].replace(".", "/") + ".py"
            if source in _seen or not (Path(root) / source).is_file():
                raise ValueError(f"{rel}: cannot resolve {node.module}")
            theirs = module_constants(root, source, _seen | {rel})
            for alias in names:
                if alias.name not in theirs:
                    raise ValueError(f"{rel}: {alias.name} is not an int constant of {node.module}")
                out[alias.asname or alias.name] = theirs[alias.name]
    return out


def _walk_own(node: ast.AST):
    """ast.walk without descending into nested functions, lambdas or classes."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        item = stack.pop()
        yield item
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(item))


def _has_literal(node: ast.AST) -> bool:
    if _int_value(node) is not None:
        return True
    if isinstance(node, ast.Compare):
        return False  # ``exc.code == 0`` is a test, not an exit code
    return any(_has_literal(child) for child in ast.iter_child_nodes(node))


def _catches_system_exit(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    names = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(ast.unparse(name) in ("SystemExit", "BaseException") for name in names)


def code_problems(root: Path, rel: str) -> list[str]:
    tree = ast.parse((Path(root) / rel).read_text(encoding="utf-8"))
    problems: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func) in EXIT_CALLS and node.args \
                and _has_literal(node.args[0]):
            problems.append(f"{rel}:{node.lineno}: literal exit code in {ast.unparse(node)[:60]}")
    for func in ast.walk(tree):
        if not isinstance(func, ast.FunctionDef) or func.name not in ("main", "_main"):
            continue
        guarded: set[int] = set()
        for node in _walk_own(func):
            if isinstance(node, ast.Try) and any(_catches_system_exit(h) for h in node.handlers):
                for inner in node.body:
                    guarded.update(id(child) for child in ast.walk(inner))
        for node in _walk_own(func):
            if isinstance(node, ast.Return) and node.value is not None and _has_literal(node.value):
                problems.append(f"{rel}:{node.lineno}: {func.name} returns a literal exit code "
                                f"({ast.unparse(node.value)[:60]}); use an EXIT_* name")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "error" and "pars" in ast.unparse(node.func.value):
                    problems.append(f"{rel}:{node.lineno}: parser.error exits 2 outside the registry")
                if node.func.attr == "parse_args" and id(node) not in guarded:
                    problems.append(f"{rel}:{node.lineno}: parse_args outside try/except SystemExit "
                                    f"(argparse would exit 2 outside the registry)")
    return problems


def parse_registry(text: str) -> tuple[dict[str, list[tuple[int, str, str, str]]], list[str]]:
    sections: dict[str, list[tuple[int, str, str, str]]] = {}
    problems: list[str] = []
    current: str | None = None
    for lineno, line in enumerate(text.splitlines(), 1):
        match = SECTION.match(line)
        if match:
            current = match.group(1)
            if current in sections:
                problems.append(f"{REGISTRY_REL}:{lineno}: {current} listed twice")
            sections[current] = []
            continue
        if line.startswith("## "):
            current = None
            continue
        if current is None or not line.startswith("|"):
            continue
        row = ROW.match(line)
        if row is None:
            if re.match(r"^\|\s*-?\d", line):
                problems.append(f"{REGISTRY_REL}:{lineno}: unreadable row in {current}")
            continue
        code, name, meaning, sentinel_word = int(row.group(1)), row.group(2), row.group(3).strip(), row.group(4)
        sentinel_word = sentinel_word.strip("` ")
        if not meaning:
            problems.append(f"{REGISTRY_REL}:{lineno}: {current} {name} has no meaning")
        if sentinel_word not in SENTINEL_WORDS:
            problems.append(f"{REGISTRY_REL}:{lineno}: {current} {name}: Sentinel column {sentinel_word!r} "
                            f"is not one of {sorted(SENTINEL_WORDS)}")
        sections[current].append((code, name, meaning, sentinel_word))
    return sections, problems


def sentinel_matrix(root: Path) -> tuple[dict[tuple[str, int], str], set[str]]:
    """(EXIT_EVENTS as {(proc, code): word}, PROC_SPECS keys) read from src/sentinel_tick.py with ast."""
    tree = ast.parse((Path(root) / SENTINEL_REL).read_text(encoding="utf-8"))
    events: dict[tuple[str, int], str] = {}
    procs: set[str] = set()
    for node in tree.body:
        target = node.target if isinstance(node, ast.AnnAssign) else (
            node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None)
        if not isinstance(target, ast.Name) or not isinstance(node.value, ast.Dict):
            continue
        if target.id == "EXIT_EVENTS":
            for key, value in zip(node.value.keys, node.value.values):
                proc, code = key.elts  # type: ignore[union-attr]
                events[(proc.value, code.value)] = ACTION_WORDS[value.id]  # type: ignore[union-attr]
        elif target.id == "PROC_SPECS":
            procs = {key.value for key in node.value.keys}  # type: ignore[union-attr]
    if not procs:
        raise ValueError(f"{SENTINEL_REL}: PROC_SPECS not found")
    return events, procs


def expected_sentinel(proc: str, code: int, events: dict[tuple[str, int], str], procs: set[str]) -> str:
    if proc not in procs or code == 0:
        return "-"
    if (proc, code) == ("scanner", 3):
        return "alert x2"  # sentinel_tick: logged once, alert at SCANNER_INCOMPLETE_CONSECUTIVE in a row
    if (proc, code) in PRE_HEARTBEAT_EXITS:
        return "不可见"  # exits before any heartbeat exists: the EXIT_EVENTS default never fires
    return events.get((proc, code), "alert")


def registry_problems(root: Path) -> list[str]:
    root = Path(root)
    try:
        text = (root / REGISTRY_REL).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{REGISTRY_REL} unreadable: {type(exc).__name__}"]
    sections, problems = parse_registry(text)
    try:
        events, procs = sentinel_matrix(root)
    except (OSError, SyntaxError, ValueError, AttributeError, KeyError, TypeError) as exc:
        return problems + [f"{SENTINEL_REL}: EXIT_EVENTS / PROC_SPECS unreadable: {type(exc).__name__}: {exc}"]
    entries = entry_points(root)
    for rel in sorted(set(sections) - set(entries)):
        problems.append(f"{REGISTRY_REL}: {rel} is not an entry point (no __main__ block)")
    for rel in entries:
        try:
            constants = module_constants(root, rel)
            problems += code_problems(root, rel)
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError) as exc:
            problems.append(f"{rel}: unreadable: {type(exc).__name__}: {exc}")
            constants = None
        if rel not in sections:
            problems.append(f"{REGISTRY_REL}: entry point {rel} has no section")
            continue
        if constants is None:
            continue
        rows = sections[rel]
        documented = {name: code for code, name, _, _ in rows}
        for name, value in sorted(constants.items()):
            if name not in documented:
                problems.append(f"{rel}: {name}={value} is not in {REGISTRY_REL}")
            elif documented[name] != value:
                problems.append(f"{rel}: {name} is {value} in code but {documented[name]} in {REGISTRY_REL}")
        for name in sorted(set(documented) - set(constants)):
            problems.append(f"{REGISTRY_REL}: {rel} {name} is not an EXIT_* constant of the module")
        by_code: dict[int, set[str]] = {}
        for code, name, _, _ in rows:
            by_code.setdefault(code, set()).add(name)
        for code, names in sorted(by_code.items()):
            if len(names) > 1:
                problems.append(f"{rel}: code {code} has more than one name: {sorted(names)}")
        proc = Path(rel).stem if rel.startswith("src/") else ""
        for code, name, _, word in rows:
            want = expected_sentinel(proc, code, events, procs)
            if word != want:
                problems.append(f"{REGISTRY_REL}: {rel} {code} ({name}): Sentinel column {word!r}, "
                                f"sentinel_tick says {want!r}")
    for (proc, code), _ in sorted(events.items()):
        rel = f"src/{proc}.py"
        if rel in sections and code not in {c for c, _, _, _ in sections[rel]}:
            problems.append(f"{SENTINEL_REL}: EXIT_EVENTS ({proc}, {code}) is not a documented code of {rel}")
        elif rel not in sections:
            problems.append(f"{SENTINEL_REL}: EXIT_EVENTS names {proc}, which has no section in {REGISTRY_REL}")
    return problems


def summary(root: Path) -> dict[str, Any]:
    sections, _ = parse_registry((Path(root) / REGISTRY_REL).read_text(encoding="utf-8"))
    return {"entry_points": len(entry_points(root)), "codes": sum(len(rows) for rows in sections.values())}
