"""Task AH: scripts/guard.py (M0). Each check fails on a deliberately bad temp copy of the repo; the
real repo passes. Offline: no network, nothing in the repo is written."""
from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts import guard

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "guard.py"
CHECKS = ["risk_sha_worktree", "risk_sha_head", "live_enabled", "banned_dependencies", "banned_imports",
          "url_allowlist", "thresholds_doc", "e2e", "halt"]
PINNED = "0aea23af1e938d330b5b4fab2c6abdec9f475f16807d177a5860a4da32aebc04"
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


# ---------------------------------------------------------------- helpers

@pytest.fixture
def repo(tmp_path) -> Path:
    """A temp copy of everything the guard reads (not a git checkout)."""
    root = tmp_path / "repo"
    for folder in ("config", "src", "scripts", "docs", "tests/fixtures/e2e"):
        shutil.copytree(ROOT / folder, root / folder, ignore=_IGNORE)
    for path in ROOT.glob("requirements*.txt"):
        shutil.copy2(path, root / path.name)
    return root


def run(root: Path, *args: str, cwd: Path | None = None) -> tuple[int, dict[str, tuple[str, str]], str]:
    """Run the copy's own guard script in a fresh process; parse its PASS/FAIL/SKIP lines."""
    proc = subprocess.run([sys.executable, str(root / "scripts" / "guard.py"), *args], cwd=str(cwd or root / "docs"),
                          capture_output=True, text=True, timeout=120)
    lines = {}
    for line in proc.stdout.splitlines():
        status, _, rest = line.partition(" ")
        if status in {"PASS", "FAIL", "SKIP"}:
            check, _, reason = rest.partition(": ")
            lines[check] = (status, reason)
    return proc.returncode, lines, proc.stdout + proc.stderr


def results(root: Path, **kwargs) -> dict[str, guard.Result]:
    return {result.check: result for result in guard.run_checks(root, **kwargs)}


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, (path, old)
    path.write_text(text.replace(old, new), encoding="utf-8")


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def failed_only(lines: dict[str, tuple[str, str]], *checks: str) -> None:
    for check in CHECKS:
        status = lines[check][0]
        if check in checks:
            assert status == "FAIL", (check, lines[check])
        else:
            assert status in ({"PASS", "SKIP"} if check == "risk_sha_head" else {"PASS"}), (check, lines[check])


# ---------------------------------------------------------------- the real repo passes

def test_the_real_repo_passes_in_process():
    found = guard.run_checks(ROOT)
    assert [r.check for r in found] == CHECKS
    for result in found:
        assert result.status == ("SKIP" if result.status == "SKIP" and result.check == "risk_sha_head" else "PASS"), result
    assert guard.exit_code(found) == guard.EXIT_OK == 0


@pytest.mark.parametrize("cwd", ["root", "docs", "src", "outside"])
def test_the_real_repo_passes_from_any_cwd(cwd, tmp_path):
    folder = {"root": ROOT, "docs": ROOT / "docs", "src": ROOT / "src", "outside": tmp_path}[cwd]
    before = tree_bytes(ROOT / "src")
    code, lines, out = run(ROOT, cwd=folder)
    assert code == 0, out
    assert list(lines) == CHECKS
    failed_only(lines)
    assert out.splitlines()[-1].endswith("failed=0 exit=0")
    assert tree_bytes(ROOT / "src") == before  # no __pycache__ or anything else written


def test_root_may_be_any_folder_inside_the_repo(tmp_path):
    code, lines, out = run(ROOT, "--root", str(ROOT / "src"), cwd=tmp_path)
    assert code == 0, out
    assert f"root={ROOT}" in out.splitlines()[-1]


def test_the_temp_copy_passes_and_is_not_written(repo):
    before = tree_bytes(repo)
    code, lines, out = run(repo)
    assert code == 0, out
    failed_only(lines)
    assert lines["risk_sha_head"][0] == "SKIP" and "HEAD not checked" in lines["risk_sha_head"][1]
    assert tree_bytes(repo) == before


def test_exit_codes_for_usage_and_no_root(tmp_path):
    assert guard.main(["--bogus"]) == guard.EXIT_USAGE == 2
    assert guard.main(["--root", str(tmp_path)]) == guard.EXIT_USAGE
    proc = subprocess.run([sys.executable, str(GUARD), "--help"], capture_output=True, text=True, timeout=60)
    help_text = " ".join(proc.stdout.split())
    assert proc.returncode == 0 and "Exit codes: 0 all checks pass" in help_text and "1 a check failed" in help_text


def test_exit_code_is_one_on_any_fail_and_skip_is_not_a_fail():
    ok = guard.Result("a", "PASS", "")
    assert guard.exit_code([ok, guard.Result("risk_sha_head", "SKIP", "")]) == 0
    assert guard.exit_code([ok, guard.Result("b", "FAIL", "")]) == guard.EXIT_FAILED == 1


def test_a_crashing_check_fails_closed(monkeypatch, repo):
    def boom(root):
        raise RuntimeError("boom")
    monkeypatch.setattr(guard, "check_banned_imports", boom)
    found = results(repo)
    assert found["banned_imports"].status == "FAIL" and "crashed" in found["banned_imports"].reason


# ---------------------------------------------------------------- 1. risk.toml sha256

def test_worktree_risk_change_fails(repo):
    path = repo / "config" / "risk.toml"
    path.write_bytes(path.read_bytes() + b"# harmless-looking comment\n")
    found = results(repo)
    assert found["risk_sha_worktree"].status == "FAIL" and PINNED in found["risk_sha_worktree"].reason
    code, lines, _ = run(repo)
    assert code == 1
    failed_only(lines, "risk_sha_worktree", "e2e", "halt")  # the e2e and halt gates pin risk.toml too


def test_worktree_threshold_change_fails(repo):
    edit(repo / "config" / "risk.toml", "max_spread = 0.04", "max_spread = 0.05")
    code, lines, _ = run(repo)
    assert code == 1 and lines["risk_sha_worktree"][0] == "FAIL"


def test_crlf_checkout_of_the_pinned_file_passes(repo):
    path = repo / "config" / "risk.toml"
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    result = guard.check_risk_sha_worktree(repo)
    assert result.status == "PASS" and "CRLF normalised" in result.reason


def test_missing_risk_file_fails(repo):
    (repo / "config" / "risk.toml").unlink()
    code, lines, _ = run(repo)
    assert code == 1
    assert lines["risk_sha_worktree"][0] == lines["live_enabled"][0] == lines["thresholds_doc"][0] == "FAIL"


def test_require_git_turns_the_skip_into_a_fail(repo):
    code, lines, _ = run(repo, "--require-git")
    assert code == 1
    failed_only(lines, "risk_sha_head")


def test_no_git_on_path_is_a_skip(monkeypatch, repo):
    monkeypatch.setattr(guard.shutil, "which", lambda name: None)
    assert guard.check_risk_sha_head(repo).status == "SKIP"
    assert guard.check_risk_sha_head(repo, require_git=True).status == "FAIL"


def _git(root: Path, *args: str) -> None:
    hooks = root.parent / "no-hooks"
    hooks.mkdir(exist_ok=True)
    subprocess.run(["git", "-c", "user.name=guard-test", "-c", "user.email=guard-test@example.invalid",
                    "-c", "commit.gpgsign=false", "-c", "core.autocrlf=false", "-c", f"core.hooksPath={hooks}",
                    "-C", str(root), *args], check=True, capture_output=True, timeout=60)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_head_is_checked_separately_from_the_worktree(repo):
    path = repo / "config" / "risk.toml"
    path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n"))  # commit the LF bytes git stores (autocrlf off here)
    _git(repo, "init", "-q")
    _git(repo, "add", "config/risk.toml")
    _git(repo, "commit", "-q", "-m", "pinned")
    head = guard.check_risk_sha_head(repo)
    assert head.status == "PASS" and PINNED in head.reason
    pinned = path.read_bytes()
    path.write_bytes(pinned.replace(b"max_open_positions = 5", b"max_open_positions = 6"))
    _git(repo, "commit", "-q", "-am", "drift")
    path.write_bytes(pinned)  # the working tree looks fine, the commit does not
    code, lines, _ = run(repo)
    assert code == 1
    failed_only(lines, "risk_sha_head")
    assert "HEAD:config/risk.toml sha256" in lines["risk_sha_head"][1]


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_risk_file_not_committed_fails(repo):
    _git(repo, "init", "-q")
    _git(repo, "add", "docs")
    _git(repo, "commit", "-q", "-m", "no risk file")
    assert guard.check_risk_sha_head(repo).status == "FAIL"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_a_copy_nested_in_another_checkout_is_not_checked_against_that_head(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    _git(outer, "init", "-q")
    inner = outer / "inner"
    for folder in ("config", "src"):
        shutil.copytree(ROOT / folder, inner / folder, ignore=_IGNORE)
    result = guard.check_risk_sha_head(inner)
    assert result.status == "SKIP" and "not the repo root" in result.reason


# ---------------------------------------------------------------- 2. live_enabled

@pytest.mark.parametrize("old,new,why", [
    ("live_enabled = false", "live_enabled = true", "live_enabled=True"),
    ("live_enabled = false", 'live_enabled = "false"', "live_enabled='false'"),
    ("live_enabled = false\n", "", "live_enabled missing"),
    ('mode = "paper"', 'mode = "live"', "mode='live'"),
    ("require_human_gate = true", "require_human_gate = false", "require_human_gate=False"),
])
def test_live_enabled_and_pins_fail(repo, old, new, why):
    edit(repo / "config" / "risk.toml", old, new)
    code, lines, _ = run(repo)
    assert code == 1
    assert lines["live_enabled"][0] == "FAIL" and why in lines["live_enabled"][1]
    assert lines["risk_sha_worktree"][0] == "FAIL"


def test_unparsable_risk_file_fails_live_enabled(repo):
    path = repo / "config" / "risk.toml"
    path.write_text(path.read_text(encoding="utf-8") + "live_enabled = [true]\n", encoding="utf-8")
    code, lines, _ = run(repo)
    assert code == 1 and lines["live_enabled"][0] == "FAIL"


# ---------------------------------------------------------------- 3. banned dependencies and imports

BAD_REQUIREMENTS = [
    "py-clob-client==0.20.0",
    "py_clob_client",
    "Py.Clob.Client>=0.1",
    "py-clob-client-v2==1.0.0",
    "some-clob-sdk",
    "web3>=6",
    "eth-account==0.11.0",
    "eth_keys",
    "ccxt",
    "polymarket",
    "polymarket-apis",
    "py-order-utils",
    "py-builder-signing-sdk",
    "py-builder-relayer-client",
    "poly-eip712-structs",
    "web3[tester]==6.0",
    "git+https://github.com/Polymarket/py-clob-client.git",
    "-e git+https://github.com/example/web3.py.git#egg=web3",
    "client @ https://example.invalid/web3-6.0.tar.gz",
]


@pytest.mark.parametrize("line", BAD_REQUIREMENTS)
def test_banned_requirement_lines(line):
    assert guard.banned_requirement(line)


@pytest.mark.parametrize("line", ["websockets==17.2.0", "pytest==8.3.5", "requests>=2", "", "numpy"])
def test_allowed_requirement_lines(line):
    assert guard.banned_requirement(line) is None


@pytest.mark.parametrize("line", ["py-clob-client==0.20.0", "web3>=6", "ccxt  # comment", "eth-account"])
@pytest.mark.parametrize("filename", ["requirements.txt", "requirements-ws.txt", "requirements-extra.txt"])
def test_a_banned_dependency_in_any_requirements_file_fails(repo, filename, line):
    path = repo / filename
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(old + line + "\n", encoding="utf-8")
    result = guard.check_banned_dependencies(repo)
    assert result.status == "FAIL" and filename in result.reason


def test_the_banned_dependency_fixture_fails_only_that_check(repo):
    (repo / "requirements-extra.txt").write_text("web3==6.0.0\n", encoding="utf-8")
    code, lines, _ = run(repo)
    assert code == 1
    failed_only(lines, "banned_dependencies")


def test_comments_naming_banned_packages_are_not_dependencies(repo):
    text = (repo / "requirements.txt").read_text(encoding="utf-8")
    assert "py-clob-client" in text  # the existing warning comment
    assert guard.check_banned_dependencies(repo).status == "PASS"


@pytest.mark.parametrize("pyproject", [
    '[project]\nname = "x"\ndependencies = ["web3>=6"]\n',
    '[project]\nname = "x"\n[project.optional-dependencies]\ntrade = ["py-clob-client"]\n',
    '[build-system]\nrequires = ["eth-account"]\n',
    '[dependency-groups]\ndev = ["ccxt"]\n',
    '[tool.poetry.dependencies]\npy-clob-client = "^0.20"\n',
    '[tool.poetry.group.x.dependencies]\nweb3 = "*"\n',
    '[project\nbroken',
])
def test_pyproject_is_scanned_and_unparsable_fails(repo, pyproject):
    (repo / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    result = guard.check_banned_dependencies(repo)
    assert result.status == "FAIL" and "pyproject.toml" in result.reason


def test_a_clean_pyproject_passes(repo):
    (repo / "pyproject.toml").write_text('[project]\nname = "pod"\ndependencies = ["websockets==17.2.0"]\n',
                                         encoding="utf-8")
    result = guard.check_banned_dependencies(repo)
    assert result.status == "PASS" and "pyproject.toml" in result.reason


BAD_IMPORTS = [
    "import web3\n",
    "import eth_account.messages\n",
    "from py_clob_client.client import ClobClient\n",
    "from py_clob_client_v2 import x\n",
    "import ccxt as exchange\n",
    "from polymarket import Client\n",
    "from x import web3\n",
    "def f():\n    import web3\n",
    "try:\n    import eth_keys\nexcept ImportError:\n    pass\n",
    "import importlib\nclient = importlib.import_module('py_clob_client')\n",
    "from importlib import import_module\nimport_module('web3.auto')\n",
    "__import__('eth_account')\n",
]


@pytest.mark.parametrize("folder", ["src", "scripts"])
@pytest.mark.parametrize("source", BAD_IMPORTS)
def test_a_banned_import_fails(repo, folder, source):
    (repo / folder / "sneaky.py").write_text(source, encoding="utf-8")
    result = guard.check_banned_imports(repo)
    assert result.status == "FAIL" and f"{folder}/sneaky.py" in result.reason


def test_a_banned_import_in_a_subpackage_fails_only_that_check(repo):
    (repo / "src" / "pkg").mkdir()
    (repo / "src" / "pkg" / "deep.py").write_text("import web3\n", encoding="utf-8")
    code, lines, _ = run(repo)
    assert code == 1
    failed_only(lines, "banned_imports")
    assert "src/pkg/deep.py:1" in lines["banned_imports"][1]


def test_an_unparsable_python_file_fails_closed(repo):
    (repo / "scripts" / "broken.py").write_text("def (:\n", encoding="utf-8")
    result = guard.check_banned_imports(repo)
    assert result.status == "FAIL" and "unparsable" in result.reason


def test_strings_naming_banned_packages_are_not_imports(repo):
    (repo / "src" / "words.py").write_text('NOTE = "never import web3 or py_clob_client"\n', encoding="utf-8")
    assert guard.check_banned_imports(repo).status == "PASS"


# ---------------------------------------------------------------- 4. URL allowlist

BAD_CODE_URLS = [
    'URL = "https://clob.polymarket.com/order"\n',
    'URL = "https://clob.polymarket.com/orders?market=x"\n',
    'URL = "https://clob.polymarket.com/auth/api-key"\n',
    'URL = "https://clob.polymarket.com/auth/derive-api-key"\n',
    'URL = "https://clob.polymarket.com/cancel-all"\n',
    'URL = "https://clob.polymarket.com/balance-allowance"\n',
    'URL = "https://clob.polymarket.com/data/trades"\n',
    'URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"\n',
    'URL = "https://relayer-v2.polymarket.com/submit"\n',
    'URL = "https://data-api.polymarket.com/positions"\n',
    'URL = "https://clob.polymarket.com/prices-history"\n',
    'URL = "http://gamma-api.polymarket.com/markets"\n',
    'URL = "ws://ws-subscriptions-clob.polymarket.com/ws/market"\n',
    'URL = "https://gamma-api.polymarket.com:8443/markets"\n',
    'URL = "https://user:pw@gamma-api.polymarket.com/markets"\n',
    'URL = "https://clob.polymarket.com/book?token_id=1&api_key=abc"\n',
    'URL = "https://gamma-api.polymarket.com.evil.example/markets"\n',
    'URL = "https://evil.example.com/markets"\n',
    'URL = "https://docs.polymarket.com/quickstart"\n',
    'URL = "https://api.binance.com/api/v3/order"\n',
    'HOST = "relayer-v2.polymarket.com"\n',
    'HOST = "data-api.polymarket.com"\n',
    'HOST = "api.binance.com"\n',
    'def url(host):\n    return f"https://{host}/markets"\n',
    'BASE = "https://" + "evil.example.com"\n',
    'URL = b"https://clob.polymarket.com/order"\n',
]


@pytest.mark.parametrize("source", BAD_CODE_URLS)
def test_a_banned_or_unlisted_url_in_src_code_fails(repo, source):
    (repo / "src" / "client.py").write_text(source, encoding="utf-8")
    result = guard.check_url_allowlist(repo)
    assert result.status == "FAIL" and "src/client.py" in result.reason


BAD_REFERENCE_URLS = [
    '"""See https://clob.polymarket.com/order for placing orders."""\n',
    '"""The user channel wss://ws-subscriptions-clob.polymarket.com/ws/user."""\n',
    '"""Mirror: https://evil.example/markets."""\n',
    "# fetch from https://data-api.polymarket.com/trades\nX = 1\n",
    "X = 1  # https://clob.polymarket.com/auth/api-key\n",
    '"""Docs over http: http://docs.polymarket.com/x."""\n',
]


@pytest.mark.parametrize("source", BAD_REFERENCE_URLS)
def test_banned_urls_in_docstrings_and_comments_fail_too(repo, source):
    (repo / "src" / "client.py").write_text(source, encoding="utf-8")
    assert guard.check_url_allowlist(repo).status == "FAIL"


GOOD_SOURCES = [
    'URL = "https://gamma-api.polymarket.com/markets?closed=false&order=volume24hr"\n',
    'URL = "https://gamma-api.polymarket.com/markets/keyset" "?closed=false"\n',
    'URL = "https://clob.polymarket.com/book?" + "token_id=1"\n',
    'URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"\n',
    'HOST = "ws-subscriptions-clob.polymarket.com"\n',
    '"""Source: https://docs.polymarket.com/api-reference/wss/market (AsyncAPI)."""\n',
    "# https://github.com/Polymarket/agent-skills/blob/main/websocket.md\nX = 1\n",
    '"""Refuses ws://, other hosts and the user channel (/ws/user)."""\n',
    'BANNED = ("/order", "/auth", "api-key")\n',
    'PATH = "data/books.jsonl"\n',
]


@pytest.mark.parametrize("source", GOOD_SOURCES)
def test_allowlisted_endpoints_and_doc_links_pass(repo, source):
    (repo / "src" / "client.py").write_text(source, encoding="utf-8")
    result = guard.check_url_allowlist(repo)
    assert result.status == "PASS", result.reason


def test_the_url_fixture_fails_only_that_check(repo):
    edit(repo / "src" / "podlib.py", 'BOOK_PREFIX = "https://clob.polymarket.com/book?"',
         'BOOK_PREFIX = "https://clob.polymarket.com/order?"')
    code, lines, _ = run(repo)
    assert code == 1
    failed_only(lines, "url_allowlist")
    assert "order/auth endpoint" in lines["url_allowlist"][1]


def test_the_allowlist_is_exactly_what_src_uses_today():
    used = set()
    for path in guard.python_files(ROOT, ("src",)):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = guard._docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                for url in guard.URL_RE.findall(node.value):
                    parts = guard.urlsplit(url)
                    used.add((parts.scheme, parts.hostname, parts.path))
    assert used == set(guard.ALLOWED_ENDPOINTS)
    assert not any("data-api" in host for _, host, _ in guard.ALLOWED_ENDPOINTS)  # unused today, so not allowed


# ---------------------------------------------------------------- 5. THRESHOLDS.md vs src/thresholds.py

THRESHOLD_FIXTURES = [
    ("docs/THRESHOLDS.md", "| `MIN_EDGE` | `0.02` |", "| `MIN_EDGE` | `0.03` |", "MIN_EDGE: doc 0.03 != code 0.02"),
    ("docs/THRESHOLDS.md", "| `MIN_EDGE` | `0.02` |", "| `MIN_EDGE` | `2e-2` |", None),  # same value: passes
    ("docs/THRESHOLDS.md", "| `MIN_SCANNED_MARKETS` | `5` |", "| `MIN_SCANNED_MARKETS` | `5.0` |",
     "MIN_SCANNED_MARKETS: doc 5.0 != code 5"),
    ("src/thresholds.py", "WASH_WINDOW_S = 300.0", "WASH_WINDOW_S = 301.0", "WASH_WINDOW_S: doc 300.0 != code 301.0"),
    ("src/thresholds.py", "REFLECTOR_MAX_PER_UTC_DAY = 3", "REFLECTOR_MAX_PER_UTC_DAY = 3\nNEW_LIMIT_S = 9.0",
     "NEW_LIMIT_S in src/thresholds.py but not in THRESHOLDS.md"),
    ("src/thresholds.py", "WS_BACKOFF_BASE_S = 1.0", "WS_BACKOFF_BASE_S = 0.0", "thresholds inconsistent"),
    ("src/endgame.py", "from src.thresholds import ENDGAME_PRICE_MIN as PRICE_MIN",
     "from src.thresholds import ENDGAME_PRICE_MAX as PRICE_MIN", "endgame.PRICE_MIN: documented as ENDGAME_PRICE_MIN"),
    ("src/maker.py", "from src.podlib import PAPER_EQUITY_USD,", "PAPER_EQUITY_USD = 1000.0\nfrom src.podlib import",
     "maker.PAPER_EQUITY_USD: documented as PAPER_EQUITY_USD"),
    ("src/book_staleness.py", "from src.thresholds import MAX_BOOK_AGE_S",
     "from src.thresholds import MAX_BOOK_AGE_S\nfrom src.thresholds import MAX_BOOK_AGE_S as BOOK_LIMIT_S",
     "book_staleness.BOOK_LIMIT_S: renamed import of MAX_BOOK_AGE_S is not documented"),
    ("src/sports_devig.py", "from src.podlib import", "from src.endgame import PRICE_MIN as FLOOR\nfrom src.podlib import",
     "sports_devig.FLOOR: renamed import of ENDGAME_PRICE_MIN is not documented"),
    ("src/scanner.py", "PAGE_LIMIT = 100", "PAGE_LIMIT = 200", "scanner.PAGE_LIMIT: doc 100 != code 200"),
    ("docs/THRESHOLDS.md", "| `max_spread` | `0.04` |", "| `max_spread` | `0.05` |",
     "risk.toml max_spread: doc 0.05 != file 0.04"),
    ("docs/THRESHOLDS.md", "| no (operational) |", "| maybe |", "is not yes/no"),
    ("docs/THRESHOLDS.md", "| Name | Value | Unit | Meaning | Where | Source | M2 pending |\n|---|---|---|---|---|---|---|\n"
     "| `MAX_BOOK_AGE_S`", "| Name | Value | Unit | Meaning | Where | Source |\n|---|---|---|---|---|---|\n| `MAX_BOOK_AGE_S`",
     "table header"),
]


@pytest.mark.parametrize("rel,old,new,why", THRESHOLD_FIXTURES)
def test_thresholds_doc_disagreement_fails(repo, rel, old, new, why):
    path = repo / rel
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    assert count >= 1, (rel, old)
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    code, lines, out = run(repo)
    if why is None:
        assert code == 0, out
        return
    assert code == 1, out
    assert lines["thresholds_doc"][0] == "FAIL" and why in lines["thresholds_doc"][1], lines["thresholds_doc"]
    if rel == "docs/THRESHOLDS.md":
        failed_only(lines, "thresholds_doc")


def test_a_missing_doc_row_fails(repo):
    path = repo / "docs" / "THRESHOLDS.md"
    lines_ = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text("".join(line for line in lines_ if not line.startswith("| `WS_PING_INTERVAL_S`")), encoding="utf-8")
    code, lines, _ = run(repo)
    assert code == 1
    failed_only(lines, "thresholds_doc")
    assert "WS_PING_INTERVAL_S in src/thresholds.py but not in THRESHOLDS.md" in lines["thresholds_doc"][1]


def test_a_doc_without_the_pinned_sha_fails(repo):
    path = repo / "docs" / "THRESHOLDS.md"
    path.write_text(path.read_text(encoding="utf-8").replace(PINNED, "0" * 64), encoding="utf-8")
    code, lines, _ = run(repo)
    assert code == 1 and "does not name the pinned" in lines["thresholds_doc"][1]


def test_a_missing_doc_fails_closed(repo):
    (repo / "docs" / "THRESHOLDS.md").unlink()
    code, lines, _ = run(repo)
    assert code == 1
    failed_only(lines, "thresholds_doc")


def test_markdown_tables_is_the_task_ac_parser():
    text = "## A\n\n| Name | Value | Unit | Meaning | Where | Source | M2 pending |\n|---|---|---|---|---|---|---|\n" \
           "| `X` | `1` | s | m | w | s | no |\n"
    assert guard.markdown_tables(text) == {"A": [dict(zip(guard.THRESHOLDS_HEADER, ["`X`", "`1`", "s", "m", "w", "s", "no"]))]}
    with pytest.raises(ValueError):
        guard.markdown_tables(text.replace("| no |", "|"))
    with pytest.raises(ValueError):
        guard.markdown_tables(text.replace("M2 pending", "M2"))


# ---------------------------------------------------------------- the guard itself

def test_the_imported_guard_is_this_repos_script():
    assert Path(guard.__file__).resolve() == GUARD.resolve()


def test_the_guard_uses_the_standard_library_only():
    tree = ast.parse(GUARD.read_text(encoding="utf-8"))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            roots.add((node.module or "").split(".")[0])
    assert roots <= set(sys.stdlib_module_names) | {"__future__"}, roots - set(sys.stdlib_module_names)


def test_the_guard_never_writes_or_fetches():
    text = GUARD.read_text(encoding="utf-8")
    tree = ast.parse(text)
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    attrs = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    assert not names & {"open", "urlopen", "socket", "Request"}
    assert not attrs & {"write_text", "write_bytes", "mkdir", "unlink", "rmtree", "replace", "rename", "touch",
                        "urlopen", "connect"} - {"replace"}
    for node in ast.walk(tree):  # str.replace is used for CRLF and names; Path.replace (a move) is not
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "replace":
            assert len(node.args) == 2, ast.unparse(node)


def test_no_ci_config_is_added():
    assert not (ROOT / ".github" / "workflows" / "guard.yml").exists()
    workflows = ROOT / ".github" / "workflows"
    if workflows.is_dir():
        assert not any("guard" in p.read_text(encoding="utf-8", errors="replace") for p in workflows.iterdir() if p.is_file())


def test_the_pin_matches_the_rest_of_the_repo():
    assert guard.PINNED_RISK_SHA256 == PINNED
    for rel in ("tests/test_fees.py", "docs/THRESHOLDS.md", "docs/PHASE0_RUN.md"):
        assert PINNED in (ROOT / rel).read_text(encoding="utf-8"), rel


# ---------------------------------------------------------------- 6. e2e (Task AJ)

def test_the_e2e_check_passes_on_the_real_repo_and_names_golden():
    found = results(ROOT)["e2e"]
    assert found.status == "PASS" and "tests/fixtures/e2e/golden.json" in found.reason, found


def test_a_golden_edit_fails_only_e2e(repo):
    path = repo / "tests" / "fixtures" / "e2e" / "golden.json"
    edit(path, '"edge_below_min"', '"edge_ok"')
    code, lines, out = run(repo)
    assert code == 1, out
    failed_only(lines, "e2e")
    assert "golden" in lines["e2e"][1]


@pytest.mark.parametrize("rel", ["scripts/e2e_offline.py", "tests/fixtures/e2e/forecasts.json",
                                 "tests/fixtures/e2e/golden.json"])
def test_a_missing_harness_or_fixture_fails_e2e(repo, rel):
    (repo / rel).unlink()
    assert results(repo)["e2e"].status == "FAIL"
    code, lines, out = run(repo)
    assert code == 1, out
    failed_only(lines, "e2e", "halt")  # Task AL: the halt check runs the same harness and fixtures


def test_a_loosened_risk_gate_rule_fails_e2e(repo):
    edit(repo / "src" / "risk_gate.py", 'reasons.append("edge_below_min")', "pass")
    code, lines, out = run(repo)
    assert code == 1, out
    failed_only(lines, "e2e")


def test_a_src_regression_that_fetches_quietly_fails_e2e(repo):
    path = repo / "src" / "forecaster.py"
    text = path.read_text(encoding="utf-8")
    marker = "    if not 0.0 < model_prob < 1.0:\n"
    assert text.count(marker) == 1
    path.write_text(text.replace(marker, "    try:\n        from src.podlib import public_get\n"
                                         "        public_get('https://clob.polymarket.com/book?token_id=1')\n"
                                         "    except Exception:\n        pass\n" + marker), encoding="utf-8")
    code, lines, out = run(repo)
    assert code == 1, out
    failed_only(lines, "e2e", "halt")
    assert "network touched" in lines["e2e"][1] and "network touched" in lines["halt"][1]


# ---------------------------------------------------------------- 7. halt (Task AL)

def test_the_halt_check_passes_on_the_real_repo():
    found = results(ROOT)["halt"]
    assert found.status == "PASS" and "corrupt halt file = global halt" in found.reason, found


def test_a_risk_gate_that_ignores_halts_fails_halt_and_e2e(repo):
    edit(repo / "src" / "risk_gate.py",
         "reasons.extend(halt_store.halt_problems(intent.market, halt_path, halt_fallback_path))", "pass")
    code, lines, out = run(repo)
    assert code == 1, out
    failed_only(lines, "e2e", "halt")
    assert "(a)" in lines["halt"][1] or "(b)" in lines["halt"][1]


def test_a_market_resume_that_lifts_a_global_halt_fails_halt(repo):
    edit(repo / "src" / "halt_store.py",
         '        if found["scope"] == SCOPE_GLOBAL and scope != SCOPE_GLOBAL:\n'
         '            raise ResumeRejected("market_resume_cannot_lift_global", halt_id)\n'
         '        if found["scope"] != scope:\n',
         '        if found["scope"] != scope and found["scope"] != SCOPE_GLOBAL:\n')
    code, lines, out = run(repo)
    assert code == 1, out
    failed_only(lines, "e2e", "halt")
    assert "market_resume_of_global" in lines["halt"][1]


def test_a_bus_that_accepts_any_resume_fails_halt(repo):
    edit(repo / "src" / "podlib.py",
         '    if kind == "resume" or frm == RESUME_FROM:\n        _check_resume(frm, to, kind, payload)\n', "")
    code, lines, out = run(repo)
    assert code == 1, out
    failed_only(lines, "e2e", "halt")
    assert "(d) the bus accepted a non-Operator resume" in lines["halt"][1]


def test_a_halt_golden_edit_fails_e2e_and_halt(repo):
    path = repo / "tests" / "fixtures" / "e2e" / "golden.json"
    edit(path, '"market_resume_cannot_lift_global"', '"released"')
    code, lines, out = run(repo)
    assert code == 1, out
    failed_only(lines, "e2e", "halt")


def test_a_harness_without_halt_gate_fails_halt(repo):
    path = repo / "scripts" / "e2e_offline.py"
    path.write_text(path.read_text(encoding="utf-8").replace("def halt_gate(", "def _old_halt_gate("),
                    encoding="utf-8")
    code, lines, out = run(repo)
    assert code == 1, out
    failed_only(lines, "halt")


def test_the_e2e_check_loads_the_checked_repos_own_harness(repo):
    path = repo / "scripts" / "e2e_offline.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n\ndef gate(root=None):\n    return ['copy harness ran'], ''\n",
                    encoding="utf-8")
    found = results(repo)["e2e"]  # in-process: the real repo's scripts package is already imported
    assert found.status == "FAIL" and "copy harness ran" in found.reason
    assert results(ROOT)["e2e"].status == "PASS"
