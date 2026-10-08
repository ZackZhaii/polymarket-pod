#!/usr/bin/env python3
"""M0 guard (Task AH): local, offline safety checks for this repo. Standard library only.

Run from any directory inside the repo (or anywhere, the repo root is found from this file):

    python scripts/guard.py                 # check the checkout this script lives in
    python scripts/guard.py --root PATH     # check another checkout (PATH or any folder inside it)
    python scripts/guard.py --require-git   # a missing git or HEAD is a FAIL, not a SKIP

It prints one line per check, ``PASS <check>: <reason>`` or ``FAIL <check>: <reason>``, and exits:

    0  every check passed (risk_sha_head may be SKIP when git or a git checkout is unavailable)
    1  at least one check failed
    2  usage error, or no repo root (a folder holding src/ and config/) at or above the start path

Checks (nothing in the repo is written, nothing is fetched, no network; check 6 keeps its scratch
files in a temp folder outside the repo and removes it):

1. risk_sha_worktree / risk_sha_head: sha256 of config/risk.toml equals PINNED_RISK_SHA256, for
   the working-tree file (CRLF normalised to LF, as git stores it with autocrlf) and for the
   committed ``git show HEAD:config/risk.toml`` (raw bytes) when git is available.
2. live_enabled: risk.toml, read with the app's own parser (src.podlib.parse_risk_toml), has
   ``live_enabled = false`` (the bool), and the other src.podlib.PINS hold too.
3. banned_dependencies: no order-capable package (ORDER_CAPABLE, or any name containing "clob")
   in requirements*.txt or pyproject.toml. banned_imports: no such import in src/ or scripts/,
   including importlib.import_module / __import__ with a literal name. ORDER_CAPABLE here is the
   single list; tests/test_ws_recorder.py (Task AG) uses it too.
4. url_allowlist: every URL in src/ code is one of ALLOWED_ENDPOINTS (built from what the code
   uses today: Gamma /markets and /markets/keyset, CLOB public /book, the WS market channel), with
   no port, user info, order/auth path or credential in the query; a host-less URL piece (an
   f-string building the host) fails, and so does any bare hostname literal outside those hosts.
   Docstrings and comments may also link documentation hosts (REFERENCE_HOSTS). The Data API is
   not used today, so it is not allowlisted; adding it is a one-line change here when a task needs it.
5. thresholds_doc: docs/THRESHOLDS.md agrees with src/thresholds.py, parsed with the Task AC table
   parser (markdown_tables, shared with tests/test_thresholds.py): every code constant once with
   its value and type, the relations in thresholds.consistency_problems hold, documented
   re-exports are the modules' ``from src.thresholds import NAME as ALIAS`` lines (and every
   renamed import is documented), operational rows match their modules, the risk.toml rows match
   the file, and the doc names the pinned sha.
6. e2e (Task AJ): the end-to-end offline gate. Loads the checked repo's own scripts/e2e_offline.py
   by path and runs its gate(): the real pipeline (scanner -> /book recorder -> paper cycle and
   Sentinel -> Endgame / NewsForecaster signals -> RiskGate -> paper fill -> ledger_store ->
   conservation -> Sentinel halt) on tests/fixtures/e2e/ with a frozen clock and network
   tripwires, compared exactly with golden.json. Its scratch files live in a throwaway temp folder
   outside the repo that is removed afterwards; golden.json is never written here (only
   ``python -m pytest tests/test_e2e_offline.py --update-golden`` writes it). About 0.1 s, no pytest.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

PINNED_RISK_SHA256 = "0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04"
RISK_REL = "config/risk.toml"

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

# Order-capable or signing-capable packages (normalised: lower case, "-" and "." -> "_").
# Single source of truth: Task AG's list plus the Polymarket relayer and EIP-712 signing helpers.
ORDER_CAPABLE = frozenset({
    "py_clob_client", "py_order_utils", "py_builder_signing_sdk", "py_builder_relayer_client",
    "clob_client", "polymarket_apis", "polymarket", "web3", "eth_account", "eth_keys",
    "eip712_structs", "poly_eip712_structs", "ccxt",
})

# URLs the code may use (scheme, host, path). Query strings are allowed (token_id, paging).
ALLOWED_ENDPOINTS = frozenset({
    ("https", "gamma-api.polymarket.com", "/markets"),
    ("https", "gamma-api.polymarket.com", "/markets/keyset"),
    ("https", "clob.polymarket.com", "/book"),
    ("wss", "ws-subscriptions-clob.polymarket.com", "/ws/market"),
})
ENDPOINT_HOSTS = frozenset(host for _, host, _ in ALLOWED_ENDPOINTS)
# Documentation links, allowed only in docstrings and comments (never in code).
REFERENCE_HOSTS = frozenset({"docs.polymarket.com", "github.com"})
BANNED_PATH_SEGMENTS = frozenset({
    "order", "orders", "cancel", "cancel-all", "cancel-orders", "cancel-market-orders", "auth",
    "api-key", "api-keys", "derive-api-key", "create-api-key", "balance-allowance", "notifications",
    "withdraw", "withdrawals", "deposit", "deposits", "submit", "sign", "relayer", "user",
})
CREDENTIAL_WORDS = ("api_key", "apikey", "api-key", "secret", "passphrase", "signature", "private_key",
                    "privatekey", "poly_address", "nonce")

URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s\"'<>`()\[\]{}\\|^]*")
HOST_RE = re.compile(
    r"(?<![A-Za-z0-9.@/-])((?:[A-Za-z0-9-]+\.)+(?:com|net|org|io|co|xyz|app|dev|ai|exchange|finance|network|trade))"
    r"(?![A-Za-z0-9-])",
    re.IGNORECASE,
)

THRESHOLDS_HEADER = ["Name", "Value", "Unit", "Meaning", "Where", "Source", "M2 pending"]


@dataclass(frozen=True)
class Result:
    check: str
    status: str
    reason: str

    def line(self) -> str:
        return f"{self.status} {self.check}: {self.reason}"


def _summary(problems: list[str], limit: int = 6) -> str:
    shown = "; ".join(problems[:limit])
    more = len(problems) - limit
    return f"{len(problems)} problem(s): {shown}" + (f"; +{more} more" if more > 0 else "")


def find_root(start: Path) -> Path | None:
    """The nearest folder at or above ``start`` holding both src/ and config/."""
    start = start.resolve()
    for folder in (start, *start.parents):
        if (folder / "src").is_dir() and (folder / "config").is_dir():
            return folder
    return None


# ---------------------------------------------------------------- 1. risk.toml sha256

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_risk_sha_worktree(root: Path) -> Result:
    name = "risk_sha_worktree"
    path = root / RISK_REL
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return Result(name, FAIL, f"{RISK_REL} unreadable ({type(exc).__name__})")
    data = raw.replace(b"\r\n", b"\n")
    digest = sha256_hex(data)
    note = " (CRLF normalised to LF)" if data != raw else ""
    if digest != PINNED_RISK_SHA256:
        return Result(name, FAIL, f"{RISK_REL} sha256 {digest} != pinned {PINNED_RISK_SHA256}{note}")
    return Result(name, PASS, f"{RISK_REL} sha256 {digest} matches the pin{note}")


def _same_path(a: str | Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(str(Path(a).resolve())) == os.path.normcase(str(b.resolve()))


def check_risk_sha_head(root: Path, require_git: bool = False) -> Result:
    name = "risk_sha_head"
    missing = FAIL if require_git else SKIP
    git = shutil.which("git")
    if git is None:
        return Result(name, missing, "git not found on PATH; HEAD not checked")
    try:
        top = subprocess.run([git, "-C", str(root), "rev-parse", "--show-toplevel"], capture_output=True,
                             timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return Result(name, missing, f"git did not run ({type(exc).__name__}); HEAD not checked")
    if top.returncode != 0:
        detail = top.stderr.decode("utf-8", "replace").strip().splitlines()[:1]
        return Result(name, missing, "not a git checkout; HEAD not checked" + (f" ({detail[0]})" if detail else ""))
    toplevel = top.stdout.decode("utf-8", "replace").strip()
    if not _same_path(toplevel, root):
        return Result(name, missing, f"the git checkout top ({toplevel}) is not the repo root; HEAD not checked")
    try:
        shown = subprocess.run([git, "-C", str(root), "show", f"HEAD:{RISK_REL}"], capture_output=True,
                               timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return Result(name, FAIL, f"git show HEAD:{RISK_REL} did not run ({type(exc).__name__})")
    if shown.returncode != 0:
        detail = shown.stderr.decode("utf-8", "replace").strip().splitlines()[:1]
        return Result(name, FAIL, f"git show HEAD:{RISK_REL} failed" + (f" ({detail[0]})" if detail else ""))
    digest = sha256_hex(shown.stdout)
    if digest != PINNED_RISK_SHA256:
        return Result(name, FAIL, f"HEAD:{RISK_REL} sha256 {digest} != pinned {PINNED_RISK_SHA256}")
    return Result(name, PASS, f"HEAD:{RISK_REL} sha256 {digest} matches the pin")


E2E_HARNESS_REL = "scripts/e2e_offline.py"


# ---------------------------------------------------------------- 2. live_enabled

def _repo_module(root: Path, module: str):
    """Import src.<module> from ``root`` (main() runs in a fresh process with root first on sys.path)."""
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return importlib.import_module(module)


def check_live_enabled(root: Path) -> Result:
    name = "live_enabled"
    try:
        podlib = _repo_module(root, "src.podlib")
        cfg = podlib.parse_risk_toml((root / RISK_REL).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any failure to read the file fails closed
        return Result(name, FAIL, f"{RISK_REL} not readable by src.podlib.parse_risk_toml ({type(exc).__name__}: {exc})")
    problems = []
    if "live_enabled" not in cfg:
        problems.append("live_enabled missing")
    elif cfg["live_enabled"] is not False:
        problems.append(f"live_enabled={cfg['live_enabled']!r}, must be false")
    for key, expected in getattr(podlib, "PINS", {}).items():
        if key != "live_enabled" and (cfg.get(key) != expected or type(cfg.get(key)) is not type(expected)):
            problems.append(f"{key}={cfg.get(key)!r} expected {expected!r}")
    if problems:
        return Result(name, FAIL, _summary(problems))
    return Result(name, PASS, "live_enabled = false and the other podlib PINS hold")


# ---------------------------------------------------------------- 3. banned dependencies and imports

def normalise(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(".", "_")


def requirement_name(line: str) -> str:
    return normalise(re.split(r"[<>=!~\[;@ \t]", line.strip(), maxsplit=1)[0])


def banned_requirement(line: str) -> str | None:
    """Why a requirement line (comment already stripped) names an order-capable package, else None."""
    text = line.strip()
    if not text:
        return None
    flat = normalise(text)
    name = requirement_name(text)
    if name in ORDER_CAPABLE:
        return f"{text!r} is order-capable"
    if "clob" in flat:
        return f"{text!r} names a clob package"
    if "://" in text or " @ " in text or text.startswith(("-e", "--editable", "git+")):
        for banned in sorted(ORDER_CAPABLE):
            if re.search(rf"(?<![a-z0-9]){banned}(?![a-z0-9])", flat):
                return f"{text!r} points at {banned}"
    return None


def banned_module(dotted: str) -> str | None:
    """Why an imported module name is order-capable, else None."""
    parts = [normalise(part) for part in dotted.split(".") if part]
    if not parts:
        return None
    if "clob" in parts[0]:
        return f"{dotted} is a clob package"
    for part in parts:
        if part in ORDER_CAPABLE:
            return f"{dotted} is order-capable"
    return None


def requirement_lines(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.split(" #", 1)[0].split("\t#", 1)[0]
        line = "" if line.lstrip().startswith("#") else line.strip()
        if line:
            lines.append(line)
    return lines


def _pyproject_requirements(text: str) -> list[str]:
    try:
        import tomllib  # Python 3.11+
    except ModuleNotFoundError:  # pragma: no cover - older Python: every quoted string is checked
        return re.findall(r"[\"']([^\"'\n]+)[\"']", text)
    data = tomllib.loads(text)  # a parse error propagates and fails the check
    found: list[str] = []
    project = data.get("project", {})
    found += [d for d in project.get("dependencies", []) if isinstance(d, str)]
    for group in project.get("optional-dependencies", {}).values():
        found += [d for d in group if isinstance(d, str)]
    found += [d for d in data.get("build-system", {}).get("requires", []) if isinstance(d, str)]
    for group in data.get("dependency-groups", {}).values():
        found += [d for d in group if isinstance(d, str)]
    poetry = data.get("tool", {}).get("poetry", {})
    for key in ("dependencies", "dev-dependencies"):
        found += list(poetry.get(key, {}))
    for group in poetry.get("group", {}).values():
        found += list(group.get("dependencies", {}))
    return found


def check_banned_dependencies(root: Path) -> Result:
    name = "banned_dependencies"
    files = sorted(root.glob("requirements*.txt"))
    problems: list[str] = []
    for path in files:
        try:
            lines = requirement_lines(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{path.name} unreadable ({type(exc).__name__})")
            continue
        problems += [f"{path.name}: {why}" for why in map(banned_requirement, lines) if why]
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        files.append(pyproject)
        try:
            reqs = _pyproject_requirements(pyproject.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - unparsable pyproject fails closed
            problems.append(f"pyproject.toml unparsable ({type(exc).__name__})")
            reqs = []
        problems += [f"pyproject.toml: {why}" for why in map(banned_requirement, reqs) if why]
    if problems:
        return Result(name, FAIL, _summary(problems))
    names = ", ".join(path.name for path in files) or "none found"
    return Result(name, PASS, f"no order-capable package in {names}")


def python_files(root: Path, folders: tuple[str, ...]) -> list[Path]:
    found = []
    for folder in folders:
        base = root / folder
        if base.is_dir():
            found += [p for p in sorted(base.rglob("*.py")) if "__pycache__" not in p.parts]
    return found


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def imported_modules(tree: ast.AST) -> list[tuple[int, str]]:
    """(line, module) for every import statement and literal importlib.import_module / __import__ call."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            found.append((node.lineno, module))
            found += [(node.lineno, f"{module}.{alias.name}" if module else alias.name) for alias in node.names]
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            called = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            first = node.args[0]
            if called in {"import_module", "__import__"} and isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((node.lineno, first.value))
    return found


def check_banned_imports(root: Path) -> Result:
    name = "banned_imports"
    files = python_files(root, ("src", "scripts"))
    problems: list[str] = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
            problems.append(f"{_rel(root, path)} unparsable ({type(exc).__name__})")
            continue
        for line, module in imported_modules(tree):
            why = banned_module(module)
            if why:
                problems.append(f"{_rel(root, path)}:{line}: {why}")
    if problems:
        return Result(name, FAIL, _summary(problems))
    return Result(name, PASS, f"no order-capable import in {len(files)} files under src/ and scripts/")


# ---------------------------------------------------------------- 4. URL allowlist

def url_problem(url: str, *, code: bool) -> str | None:
    """Why a URL found in src/ is not allowed, else None. ``code``: a string literal (not a docstring/comment)."""
    url = url.rstrip(".,;:")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return f"{url!r} is not a valid URL"
    scheme = parts.scheme.lower()
    if not host:
        return f"{url!r} has no literal host" if code else None
    path = parts.path or ""
    segments = {segment.lower() for segment in path.split("/") if segment}
    query = (parts.query or "").lower()
    api_host = host.endswith("polymarket.com") and host not in REFERENCE_HOSTS
    if api_host or code:
        if path.lower().startswith("/ws/user"):
            return f"{url!r} is the authenticated user channel"
        if host.startswith("relayer") or segments & BANNED_PATH_SEGMENTS:
            return f"{url!r} is an order/auth endpoint"
        keys = [key for key, _ in parse_qsl(query, keep_blank_values=True)]
        if any(word in key for key in keys for word in CREDENTIAL_WORDS) or any(word in query for word in CREDENTIAL_WORDS):
            return f"{url!r} carries a credential in the query"
        if parts.username or parts.password or port is not None:
            return f"{url!r} has user info or a port"
        if (scheme, host, path) not in ALLOWED_ENDPOINTS:
            return f"{url!r} is not an allowlisted endpoint"
        return None
    if host in REFERENCE_HOSTS and scheme == "https":
        return None
    return f"{url!r} host {host} is not allowlisted"


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def url_problems_in_source(text: str, label: str) -> list[str]:
    tree = ast.parse(text)
    docstrings = _docstring_nodes(tree)
    problems: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes))):
            continue
        value = node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value
        is_code = id(node) not in docstrings
        for url in URL_RE.findall(value):
            why = url_problem(url, code=is_code)
            if why:
                problems.append(f"{label}:{node.lineno}: {why}")
        if is_code:
            for host in HOST_RE.findall(URL_RE.sub(" ", value)):
                if host.lower() not in ENDPOINT_HOSTS:
                    problems.append(f"{label}:{node.lineno}: host literal {host!r} is not allowlisted")
    for token in tokenize.generate_tokens(io.StringIO(text).readline):
        if token.type == tokenize.COMMENT:
            for url in URL_RE.findall(token.string):
                why = url_problem(url, code=False)
                if why:
                    problems.append(f"{label}:{token.start[0]}: {why} (comment)")
    return problems


def check_url_allowlist(root: Path) -> Result:
    name = "url_allowlist"
    files = python_files(root, ("src",))
    problems: list[str] = []
    for path in files:
        try:
            problems += url_problems_in_source(path.read_text(encoding="utf-8"), _rel(root, path))
        except (OSError, UnicodeDecodeError, SyntaxError, ValueError, tokenize.TokenError) as exc:
            problems.append(f"{_rel(root, path)} unparsable ({type(exc).__name__})")
    if problems:
        return Result(name, FAIL, _summary(problems))
    return Result(name, PASS, f"every URL in {len(files)} src files is allowlisted "
                              f"({len(ALLOWED_ENDPOINTS)} endpoints, read-only)")


# ---------------------------------------------------------------- 5. THRESHOLDS.md vs src/thresholds.py

def markdown_tables(text: str, header: list[str] = THRESHOLDS_HEADER) -> dict[str, list[dict[str, str]]]:
    """Task AC parser: section heading -> rows of the markdown table under it. Raises ValueError on a bad table."""
    tables: dict[str, list[dict[str, str]]] = {}
    section = ""
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("## "):
            section, current = line[3:].strip(), None
            continue
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if current is None:
            current = cells
            if current != header:
                raise ValueError(f"section {section!r}: table header {current} != {header}")
            tables[section] = []
        elif set("".join(cells)) <= set("-: "):
            continue
        else:
            if len(cells) != len(current):
                raise ValueError(f"section {section!r}: row has {len(cells)} cells: {line}")
            tables[section].append(dict(zip(current, cells)))
    return tables


def table(tables: dict[str, list[dict[str, str]]], prefix: str) -> list[dict[str, str]]:
    matches = [rows for section, rows in tables.items() if section.startswith(prefix)]
    if len(matches) != 1:
        raise ValueError(f"expected one section starting {prefix!r}, found {len(matches)}")
    return matches[0]


def code_cell(cell: str) -> str:
    match = re.fullmatch(r"`([^`]+)`", cell)
    if not match:
        raise ValueError(f"cell {cell!r} is not one code span")
    return match.group(1)


def number_constants(module_vars: dict[str, object]) -> dict[str, object]:
    return {
        name: value
        for name, value in module_vars.items()
        if name.isupper() and not name.startswith("_")
        and isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _module_literals(tree: ast.Module) -> dict[str, object]:
    found: dict[str, object] = {}
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        for target in targets:
            if isinstance(target, ast.Name) and node.value is not None:
                try:
                    found[target.id] = ast.literal_eval(node.value)
                except ValueError:
                    pass
    return found


def _src_imports(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """Local name -> (src module, name) for every ``from src.<module> import NAME [as ALIAS]``."""
    found = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("src."):
            for alias in node.names:
                found[alias.asname or alias.name] = (node.module.split(".", 1)[1], alias.name)
    return found


def threshold_reexports(root: Path) -> dict[str, tuple[str, bool]]:
    """``module.LOCAL`` -> (thresholds name, renamed anywhere on the way) for names that come from
    src/thresholds.py, directly or through another src module (``from src.podlib import X``)."""
    graph: dict[str, dict[str, tuple[str, str]]] = {}
    for path in sorted((root / "src").glob("*.py")):
        if path.name != "thresholds.py":
            graph[path.stem] = _src_imports(ast.parse(path.read_text(encoding="utf-8")))
    resolved: dict[str, tuple[str, bool]] = {}
    for module, names in graph.items():
        for local in names:
            current, renamed, seen = (module, local), False, set()
            while current[0] in graph and current[1] in graph[current[0]] and current not in seen:
                seen.add(current)
                source = graph[current[0]][current[1]]
                renamed = renamed or source[1] != current[1]
                current = source
            if current[0] == "thresholds":
                resolved[f"{module}.{local}"] = (current[1], renamed)
    return resolved


def thresholds_doc_problems(root: Path) -> list[str]:
    problems: list[str] = []
    thresholds = _repo_module(root, "src.thresholds")  # raises if a relation is broken (consistency_problems)
    problems += [f"relation: {p}" for p in thresholds.consistency_problems()]
    constants = number_constants(vars(thresholds))
    doc_text = (root / "docs" / "THRESHOLDS.md").read_text(encoding="utf-8")
    tables = markdown_tables(doc_text)

    rows = table(tables, "Code constants")
    names = [code_cell(row["Name"]) for row in rows]
    problems += [f"{n} documented twice" for n in sorted({n for n in names if names.count(n) > 1})]
    problems += [f"{n} in src/thresholds.py but not in THRESHOLDS.md" for n in sorted(set(constants) - set(names))]
    problems += [f"{n} in THRESHOLDS.md but not in src/thresholds.py" for n in sorted(set(names) - set(constants))]
    documented_reexports: dict[str, str] = {}
    for row in rows:
        name = code_cell(row["Name"])
        value = ast.literal_eval(code_cell(row["Value"]))
        if name in constants and (value != constants[name] or type(value) is not type(constants[name])):
            problems.append(f"{name}: doc {value!r} != code {constants[name]!r}")
        if not (row["Unit"] and row["Meaning"] and row["Source"]):
            problems.append(f"{name}: empty Unit, Meaning or Source")
        if not row["Where"].startswith("`src/thresholds.py`"):
            problems.append(f"{name}: Where does not start with `src/thresholds.py`")
        if not (row["M2 pending"] == "yes" or row["M2 pending"].startswith("no")):
            problems.append(f"{name}: M2 pending {row['M2 pending']!r} is not yes/no")
        for dotted in re.findall(r"`([a-z_]+\.[A-Z_0-9]+)`", row["Where"]):
            documented_reexports[dotted] = name

    reexports = threshold_reexports(root)
    actual_reexports = {dotted: name for dotted, (name, _) in reexports.items()}
    renamed = {dotted: name for dotted, (name, was_renamed) in reexports.items() if was_renamed}
    for dotted, name in sorted(documented_reexports.items()):
        if actual_reexports.get(dotted) != name:
            problems.append(f"{dotted}: documented as {name}, but src/{dotted.split('.')[0]}.py does not import it as that")
    for dotted, name in sorted(renamed.items()):
        if documented_reexports.get(dotted) != name:
            problems.append(f"{dotted}: renamed import of {name} is not documented")

    for row in table(tables, "Operational limits"):
        dotted = code_cell(row["Name"])
        module, attr = dotted.split(".")
        value = ast.literal_eval(code_cell(row["Value"]))
        path = root / "src" / f"{module}.py"
        literals = _module_literals(ast.parse(path.read_text(encoding="utf-8"))) if path.exists() else {}
        if attr not in literals or literals[attr] != value or type(literals[attr]) is not type(value):
            problems.append(f"{dotted}: doc {value!r} != code {literals.get(attr, 'missing')!r}")
        if row["Where"] != f"`src/{module}.py`":
            problems.append(f"{dotted}: Where should be `src/{module}.py`")
        if attr in constants:
            problems.append(f"{dotted}: also a src/thresholds.py constant")

    podlib = _repo_module(root, "src.podlib")
    parsed = podlib.parse_risk_toml((root / RISK_REL).read_text(encoding="utf-8"))
    risk_rows = table(tables, "config/risk.toml")
    risk_names = [code_cell(row["Name"]) for row in risk_rows]
    if sorted(risk_names) != sorted(parsed):
        problems.append(f"risk.toml rows {sorted(risk_names)} != file keys {sorted(parsed)}")
    for row in risk_rows:
        key = code_cell(row["Name"])
        documented = podlib.parse_risk_toml(f"{key} = {code_cell(row['Value'])}\n")[key]
        if key in parsed and (documented != parsed[key] or type(documented) is not type(parsed[key])):
            problems.append(f"risk.toml {key}: doc {documented!r} != file {parsed[key]!r}")
        if row["M2 pending"] != "file-governed":
            problems.append(f"risk.toml {key}: M2 pending should be file-governed")
    if PINNED_RISK_SHA256 not in doc_text:
        problems.append("THRESHOLDS.md does not name the pinned risk.toml sha256")
    return problems


def check_thresholds_doc(root: Path) -> Result:
    name = "thresholds_doc"
    try:
        problems = thresholds_doc_problems(root)
    except Exception as exc:  # noqa: BLE001 - a doc or module that cannot be read fails closed
        return Result(name, FAIL, f"{type(exc).__name__}: {exc}")
    if problems:
        return Result(name, FAIL, _summary(problems))
    return Result(name, PASS, "docs/THRESHOLDS.md agrees with src/thresholds.py, its modules and risk.toml")


# ---------------------------------------------------------------- runner

# ---------------------------------------------------------------- 6. e2e (Task AJ)

def _load_e2e(root: Path):
    """Load ``root``'s own scripts/e2e_offline.py by path (never another checkout's cached copy).

    Its src imports resolve like check_live_enabled's (root first on sys.path in a fresh process).
    No bytecode is written while it loads.
    """
    path = root / E2E_HARNESS_REL
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    name = "_guard_e2e_" + hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:16]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {E2E_HARNESS_REL}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    saved = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    finally:
        sys.dont_write_bytecode = saved
    return module


def check_e2e(root: Path) -> Result:
    name = "e2e"
    if not (root / E2E_HARNESS_REL).is_file():
        return Result(name, FAIL, f"{E2E_HARNESS_REL} is missing")
    problems, summary = _load_e2e(root).gate(root)
    if problems:
        return Result(name, FAIL, _summary(problems))
    return Result(name, PASS, f"offline pipeline matches tests/fixtures/e2e/golden.json: {summary}")


def run_checks(root: Path, require_git: bool = False) -> list[Result]:
    checks = [
        lambda: check_risk_sha_worktree(root),
        lambda: check_risk_sha_head(root, require_git),
        lambda: check_live_enabled(root),
        lambda: check_banned_dependencies(root),
        lambda: check_banned_imports(root),
        lambda: check_url_allowlist(root),
        lambda: check_thresholds_doc(root),
        lambda: check_e2e(root),
    ]
    results = []
    names = ["risk_sha_worktree", "risk_sha_head", "live_enabled", "banned_dependencies", "banned_imports",
             "url_allowlist", "thresholds_doc", "e2e"]
    for name, check in zip(names, checks):
        try:
            results.append(check())
        except Exception as exc:  # noqa: BLE001 - a crashing check is a failing check
            results.append(Result(name, FAIL, f"check crashed: {type(exc).__name__}: {exc}"))
    return results


def exit_code(results: list[Result]) -> int:
    return EXIT_FAILED if any(result.status == FAIL for result in results) else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="guard.py",
        description="M0 guard: offline, read-only safety checks (risk.toml pin, live_enabled, banned "
                    "packages and imports, URL allowlist, THRESHOLDS.md, the end-to-end offline gate).",
        epilog="Exit codes: 0 all checks pass (risk_sha_head may SKIP without git); 1 a check failed; "
               "2 usage error or no repo root found.",
    )
    parser.add_argument("--root", type=Path, default=None,
                        help="repo to check, or any folder inside it (default: the repo holding this script)")
    parser.add_argument("--require-git", action="store_true",
                        help="fail risk_sha_head instead of skipping it when git or a checkout is unavailable")
    return parser


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True  # read-only: importing src.* must not leave __pycache__ behind
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code == 0 else EXIT_USAGE
    start = args.root if args.root is not None else Path(__file__).resolve().parents[1]
    root = find_root(start)
    if root is None:
        print(f"guard: no repo root (a folder with src/ and config/) at or above {start}", file=sys.stderr)
        return EXIT_USAGE
    results = run_checks(root, require_git=args.require_git)
    for result in results:
        print(result.line())
    code = exit_code(results)
    failed = sum(result.status == FAIL for result in results)
    print(f"guard: root={root} checks={len(results)} failed={failed} exit={code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
