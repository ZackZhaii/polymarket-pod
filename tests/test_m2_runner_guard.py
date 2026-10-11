"""Issue #119 (Task CE): Poly执行's no-live acceptance tests T1-T13 for the M2 paper runner (F2).

T8 (every dependency manifest kind, one failing fixture each) is issue #152's guard change, as the audit says
("实现见 F1 issue"); the existing guard check banned_dependencies keeps running in scripts/guard.py.
"""
from __future__ import annotations

import ast
import http.client
import io
import json
import os
import socket
import ssl
import subprocess
import sys
import textwrap
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest

import src.ledger_store as ledger_store
import src.paper_ledger as paper_ledger
from src import intent_builder, m2_runner, podlib, secret_scan
from scripts import guard
from tests import m2_runner_rig as rig

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
RUNNER = SRC / "m2_runner.py"
BANNED_MODULES = sorted(guard.ORDER_CAPABLE | {"polymarket_client", "requests", "httpx", "aiohttp", "websockets"})
# Values are built by concatenation so the repo secret scan does not flag this file (like secret_scan.self_check).
CANARIES = {"POLY_PRIVATE_KEY": "0x" + "ab" * 32, "POLY_API_KEY": "canary-api-key-" + "7f3e",
            "POLY_API_SECRET": "canary-secret-" + "91c2", "POLY_API_PASSPHRASE": "canary-pass-" + "55aa",
            "PK": "0x" + "cd" * 32, "MNEMONIC": "canary " * 11 + "word"}


def _src_trees() -> list[tuple[Path, ast.Module]]:
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in sorted(SRC.glob("*.py"))]


def _docstring_ids(tree: ast.AST) -> set[int]:
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


@pytest.fixture
def env(tmp_path, monkeypatch):
    paths = rig.isolate(monkeypatch, tmp_path)
    return {**paths, "now": datetime.now(timezone.utc), "tmp": tmp_path}


# ---------------------------------------------------------------- T1 imports


SUBPROCESS = textwrap.dedent('''
    import importlib.abc, json, sys
    from pathlib import Path
    BANNED = set(json.loads(sys.argv[1])); tmp = Path(sys.argv[2]); attempts = []

    class Block(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name.split(".")[0] in BANNED:
                attempts.append(name)
                raise ImportError("blocked by the m2_runner import test: " + name)
            return None

    sys.meta_path.insert(0, Block())
    sys.path.insert(0, sys.argv[3])
    from datetime import datetime, timezone
    from src import forecaster, halt_store, heartbeat, m2_runner, paper_ledger, podlib
    from tests import m2_runner_rig as rig
    halt_store.HALT_STATE_PATH = tmp / "halt_state.json"; halt_store.HALT_FALLBACK_PATH = tmp / "HALT"
    heartbeat.HEARTBEAT_DIR = tmp / "heartbeat"; forecaster.FORECASTS_PATH = tmp / "forecasts.jsonl"
    paper_ledger.PAPER_PATH = tmp / "paper.jsonl"; paper_ledger.QUOTES_PATH = tmp / "quotes.jsonl"
    podlib.BUS_PATH = tmp / "bus.jsonl"
    heartbeat.Heartbeat("sentinel", every_s=30.0).beat(heartbeat.STATE_RUNNING, force=True)
    now = datetime.now(timezone.utc); run = tmp / "runs" / "m2"
    rig.deliver(run, now); fake = rig.FakeGetters(now)
    code = m2_runner.main(["--run-dir", str(run), "--once"], getters=fake.getters())
    loaded = sorted(n for n in sys.modules if n.split(".")[0] in BANNED)
    fills = len(rig.read_jsonl(tmp / "paper.jsonl"))
    print(json.dumps({"code": code, "attempts": attempts, "loaded": loaded, "fills": fills}))
''')


def test_t1_a_once_run_imports_no_order_capable_or_http_client_module(tmp_path):
    result = subprocess.run([sys.executable, "-c", SUBPROCESS, json.dumps(BANNED_MODULES), str(tmp_path), str(ROOT)],
                            capture_output=True, text=True, cwd=ROOT, timeout=120)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report == {"code": 0, "attempts": [], "loaded": [], "fills": 1}


def test_t1_the_runner_module_imports_nothing_banned():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    names = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    names |= {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    roots = {name.split(".")[0] for name in names}
    assert not roots & set(BANNED_MODULES)
    assert "ws_recorder" not in str(sorted(names)) and "urllib.request" not in names  # T13: no WS; no raw HTTP


# ---------------------------------------------------------------- T2 write requests


def test_t2_no_write_request_anywhere_in_src():
    problems = []
    for path, tree in _src_trees():
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr in ("post", "put", "patch", "delete", "request"):
                    problems.append(f"{path.name}:{node.lineno} .{func.attr}(")
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                if name == "Request":
                    kw = {k.arg: k.value for k in node.keywords}
                    method = kw.get("method")
                    if not (isinstance(method, ast.Constant) and method.value == "GET") or "data" in kw \
                            or len(node.args) > 1:
                        problems.append(f"{path.name}:{node.lineno} Request not a literal GET without data")
                if name == "urlopen" and path.name != "podlib.py":
                    problems.append(f"{path.name}:{node.lineno} urlopen outside podlib")
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                text = node.value.lower()
                if "clob." in text and any(word in text for word in ("/order", "/cancel", "/auth", "api-key",
                                                                     "balance-allowance")):
                    problems.append(f"{path.name}:{node.lineno} CLOB order/auth string")
    assert problems == []


# ---------------------------------------------------------------- T3 endpoint allowlist


REFUSED_URLS = [
    "https://clob.polymarket.com/order", "https://clob.polymarket.com/orders",
    "https://clob.polymarket.com/cancel-all", "https://clob.polymarket.com/cancel",
    "https://clob.polymarket.com/auth/api-key", "https://clob.polymarket.com/auth/derive-api-key",
    "https://clob.polymarket.com/balance-allowance", "wss://ws-subscriptions-clob.polymarket.com/ws/user",
    "https://relayer-v2.polymarket.com/submit", "https://clob.polymarket.com/clob-markets/0xabc",
    "https://clob.polymarket.com/book/extra", "http://clob.polymarket.com/book?token_id=1",
    "https://clob.polymarket.com:8443/book?token_id=1", "https://u:p@clob.polymarket.com/book?token_id=1",
    "https://gamma-api.polymarket.com/events", "https://data-api.polymarket.com/v2/positions?user=0x1",
]


@pytest.mark.parametrize("url", REFUSED_URLS)
def test_t3_the_runner_refuses_order_auth_user_and_unlisted_urls_without_a_call(url):
    calls = []
    guarded = m2_runner.GuardedGetter(calls.append)
    assert not m2_runner.url_allowed(url)
    with pytest.raises(m2_runner.RefusedURL):
        guarded(url)
    assert calls == [] and guarded.refused == [url]


@pytest.mark.parametrize("url", [u for u in REFUSED_URLS if "clob-markets" not in u and "/book" not in u
                                 and "gamma" not in u and "data-api" not in u])
def test_t3_public_get_refuses_them_too_with_zero_network(url, monkeypatch):
    touched = []
    monkeypatch.setattr(podlib, "urlopen", lambda *a, **k: touched.append(a))
    with pytest.raises(RuntimeError):
        podlib.public_get(url)
    assert touched == []


def test_t3_the_runner_allowlist_is_inside_the_guard_allowlist():
    assert m2_runner.ALLOWED_GETS <= guard.ALLOWED_ENDPOINTS
    assert {path for _, _, path in m2_runner.ALLOWED_GETS} == {"/markets", "/markets/keyset", "/book"}


# ---------------------------------------------------------------- T4 credentials / environment


def test_t4_no_environment_or_dotenv_read_in_src():
    problems = []
    for path, tree in _src_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("environ", "getenv", "environb", "putenv"):
                problems.append(f"{path.name}:{node.lineno} os.{node.attr}")
            if isinstance(node, ast.Name) and node.id in ("environ", "getenv"):
                problems.append(f"{path.name}:{node.lineno} {node.id}")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
                if any("dotenv" in n for n in names) or (isinstance(node, ast.ImportFrom) and node.module == "os"
                                                         and {"environ", "getenv"} & {a.name for a in node.names}):
                    problems.append(f"{path.name}:{node.lineno} env import")
            if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "open":
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant) and isinstance(first.value, str) and \
                        Path(first.value).name.startswith(".env"):
                    problems.append(f"{path.name}:{node.lineno} open(.env)")
    assert problems == []


class SpyEnviron(dict):
    def __init__(self, base, reads):
        super().__init__(base)
        self.reads = reads

    def __getitem__(self, key):
        if key in CANARIES:
            self.reads.append(key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in CANARIES:
            self.reads.append(key)
        return super().get(key, default)


def test_t4_canary_credentials_are_never_read_or_written(env, monkeypatch, capsys):
    reads: list[str] = []
    for key, value in CANARIES.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(os, "environ", SpyEnviron(os.environ, reads))
    monkeypatch.setattr(os, "getenv", lambda key, default=None: os.environ.get(key, default))
    rig.deliver(env["run"], env["now"])
    assert rig.run_once(env["run"], rig.FakeGetters(env["now"])) == m2_runner.EXIT_OK
    assert reads == []
    out = capsys.readouterr()
    written = [p for p in env["tmp"].rglob("*") if p.is_file()]
    from src import heartbeat
    written += [p for p in Path(heartbeat.HEARTBEAT_DIR).rglob("*") if p.is_file()]
    blobs = [p.read_bytes() for p in written] + [out.out.encode(), out.err.encode()]
    for value in CANARIES.values():
        assert not any(value.encode() in blob for blob in blobs)
    findings, errors = secret_scan.scan_paths(written)
    assert findings == [] and errors == []


# ---------------------------------------------------------------- T5 executor hard-off


def test_t5_executor_enabled_is_one_literal_false_and_never_rebound():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    assigns = [node for node in ast.walk(tree)
               if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
               and any(isinstance(t, ast.Name) and t.id == "EXECUTOR_ENABLED"
                       for t in (node.targets if isinstance(node, ast.Assign) else [node.target]))]
    assert len(assigns) == 1 and isinstance(assigns[0], ast.Assign)
    assert isinstance(assigns[0].value, ast.Constant) and assigns[0].value.value is False
    assert assigns[0] in tree.body  # module level
    assert not any(isinstance(node, (ast.Global, ast.Nonlocal)) and "EXECUTOR_ENABLED" in node.names
                   for node in ast.walk(tree))
    assert m2_runner.EXECUTOR_ENABLED is False
    # nothing else in src/ assigns it (no setattr / attribute rebinding)
    for path, other in _src_trees():
        for node in ast.walk(other):
            if isinstance(node, ast.Attribute) and node.attr == "EXECUTOR_ENABLED" and isinstance(node.ctx, ast.Store):
                pytest.fail(f"{path.name}:{node.lineno} rebinds EXECUTOR_ENABLED")
            if isinstance(node, ast.Constant) and node.value == "EXECUTOR_ENABLED":
                pytest.fail(f"{path.name}:{node.lineno} names EXECUTOR_ENABLED as a string (setattr?)")


def test_t5_the_cli_has_only_run_dir_max_runtime_and_once():
    options = {opt for action in m2_runner.build_parser()._actions for opt in action.option_strings}
    assert options == {"-h", "--help", "--run-dir", "--max-runtime", "--once"}


def test_t5_the_runner_defines_no_order_or_send_function():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    words = {"order", "orders", "submit", "sign", "signed", "send", "cancel", "post", "execute", "executor", "place"}
    names = [node.name.lower() for node in ast.walk(tree)
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    assert [n for n in names if words & set(n.strip("_").split("_"))] == []


# ---------------------------------------------------------------- T6 live flags (and a relaxed pin) are still refused


RISK_TEXT = Path(podlib.RISK_PATH).read_text(encoding="utf-8")


@pytest.mark.parametrize("old,new", [("live_enabled = false", "live_enabled = true"),
                                     ('mode = "paper"', 'mode = "live"'),
                                     ("require_human_gate = true", "require_human_gate = false")])
def test_t6_a_live_flag_exits_6_with_a_global_halt_and_no_getter_or_commit(env, monkeypatch, old, new):
    commits = []
    monkeypatch.setattr(paper_ledger, "commit_if_allowed", lambda *a, **k: commits.append(a))
    risk = env["tmp"] / "risk.toml"
    risk.write_text(RISK_TEXT.replace(old, new), encoding="utf-8")
    fake = rig.FakeGetters(env["now"], fail=True)
    rig.deliver(env["run"], env["now"])
    assert rig.run_once(env["run"], fake, risk_path=risk) == m2_runner.EXIT_PIN_OR_LIVE
    assert fake.calls == 0 and commits == []
    from src import halt_store
    assert any(h["reason"] == m2_runner.LIVE_HALT_REASON for h in halt_store.active_halts())


def test_t6_with_the_pins_relaxed_intents_are_still_built_paper_only(env, monkeypatch):
    monkeypatch.setattr(podlib, "PINS", {})  # a relaxed pin table: the runner must still build paper only
    seen = []
    real = intent_builder.build_order_intent

    def spy(*args, **kwargs):
        seen.append(kwargs.get("paper"))
        return real(*args, **kwargs)
    monkeypatch.setattr(intent_builder, "build_order_intent", spy)
    rig.deliver(env["run"], env["now"])
    assert rig.run_once(env["run"], rig.FakeGetters(env["now"])) == m2_runner.EXIT_OK
    assert seen == [True]


# ---------------------------------------------------------------- T7 socket-level zero network


def test_t7_a_once_run_touches_no_socket_tls_http_or_urlopen(env, monkeypatch):
    attempts: list[str] = []

    def trip(label):
        def blocked(*args, **kwargs):
            attempts.append(label)
            raise OSError(f"tripwire {label}")
        return blocked
    for owner, name in [(socket, "socket"), (socket, "create_connection"), (socket, "getaddrinfo"),
                        (ssl.SSLContext, "wrap_socket"), (http.client.HTTPConnection, "connect"),
                        (urllib.request, "urlopen"), (podlib, "urlopen")]:
        monkeypatch.setattr(owner, name, trip(f"{getattr(owner, '__name__', owner)}.{name}"))
    rig.deliver(env["run"], env["now"])
    assert rig.run_once(env["run"], rig.FakeGetters(env["now"])) == m2_runner.EXIT_OK
    assert attempts == []
    assert len(rig.read_jsonl(env["paper"])) == 1


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_t7_through_public_get_every_request_is_an_allowlisted_get(env, monkeypatch):
    fake = rig.FakeGetters(env["now"])
    requests = []

    def recorder(request, timeout=None):
        requests.append(request)
        url = request.full_url
        body = fake.book(url) if "/book" in url else fake.gamma(url)
        return _Response(json.dumps(body).encode("utf-8"))
    monkeypatch.setattr(podlib, "urlopen", recorder)
    rig.deliver(env["run"], env["now"])
    assert m2_runner.main(["--run-dir", str(env["run"]), "--once"]) == m2_runner.EXIT_OK  # production getters
    assert requests and all(r.get_method() == "GET" and r.data is None for r in requests)
    from urllib.parse import urlsplit
    for r in requests:
        parts = urlsplit(r.full_url)
        assert (parts.scheme, parts.hostname, parts.path) in guard.ALLOWED_ENDPOINTS
    assert len(rig.read_jsonl(env["paper"])) == 1


def test_t7_the_network_guard_refuses_other_hosts():
    with m2_runner.network_guard() as refused:
        with pytest.raises(OSError):
            socket.getaddrinfo("example.com", 443)
    assert refused == ["example.com"]


# ---------------------------------------------------------------- T9 getter injection


def test_t9_production_getters_are_public_get():
    defaults = m2_runner.Getters()
    assert defaults.gamma is podlib.public_get and defaults.book is podlib.public_get


def test_t9_the_runner_reaches_getters_only_through_the_guard():
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    direct = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
              and getattr(node.func, "attr", getattr(node.func, "id", "")) in ("public_get", "urlopen")]
    assert direct == []
    wrapped = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
               and getattr(node.func, "id", "") == "GuardedGetter"]
    assert len(wrapped) == 3  # iteration: gamma+book; resume_detected path: gamma only (issue #120)


# ---------------------------------------------------------------- T10 single writer of the paper ledger


WRITE_CALLS = {"append_jsonl", "write_text", "write_bytes", "book", "replace", "rename", "open", "unlink",
               "append_synced", "write_record"}


def test_t10_only_ledger_store_and_paper_ledger_write_paper_path():
    problems = []
    for path, tree in _src_trees():
        if path.name in ("ledger_store.py", "paper_ledger.py"):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name not in WRITE_CALLS:
                continue
            if name == "open" and not any(isinstance(a, ast.Constant) and isinstance(a.value, str)
                                          and set(a.value) & set("wax+") for a in node.args[1:]):
                continue
            parts = [node.func, *node.args, *[k.value for k in node.keywords]]
            if any(isinstance(n, (ast.Name, ast.Attribute)) and getattr(n, "id", getattr(n, "attr", "")) == "PAPER_PATH"
                   for part in parts for n in ast.walk(part)):
                problems.append(f"{path.name}:{node.lineno} {name}(... PAPER_PATH ...)")
    assert problems == []


def test_t10_the_runner_never_names_the_ledger_file_for_writing():
    text = RUNNER.read_text(encoding="utf-8")
    tree = ast.parse(text)
    assert not any(isinstance(n, ast.Attribute) and n.attr == "book" and getattr(n.value, "id", "") == "ledger_store"
                   for n in ast.walk(tree))
    assert "paper.jsonl" not in text


# ---------------------------------------------------------------- T11 zero bypass


def test_t11_every_booked_fill_is_paired_with_an_allow_in_the_same_commit_call(env, monkeypatch):
    real_commit, real_evaluate, real_book = paper_ledger.commit_if_allowed, paper_ledger.evaluate, ledger_store.book
    call: dict = {"open": False, "decisions": []}
    booked: list[list[str]] = []

    def commit(*args, **kwargs):
        call.update(open=True, decisions=[])
        try:
            return real_commit(*args, **kwargs)
        finally:
            call["open"] = False

    def evaluate(*args, **kwargs):
        decision = real_evaluate(*args, **kwargs)
        call["decisions"].append("allow" if decision.allow else "veto")
        return decision

    def book(*args, **kwargs):
        assert call["open"], "ledger_store.book outside commit_if_allowed"
        booked.append(list(call["decisions"]))
        return real_book(*args, **kwargs)
    monkeypatch.setattr(paper_ledger, "commit_if_allowed", commit)
    monkeypatch.setattr(paper_ledger, "evaluate", evaluate)
    monkeypatch.setattr(ledger_store, "book", book)
    rig.deliver(env["run"], env["now"])
    rig.deliver(env["run"], env["now"], name="0002.json", p=0.6)
    assert rig.run_once(env["run"], rig.FakeGetters(env["now"])) == m2_runner.EXIT_OK
    fills = rig.read_jsonl(env["paper"])
    assert len(booked) == len(fills) >= 1
    assert all(decisions == ["allow"] for decisions in booked)  # exactly one allow, in that same call
    journal = [row["step"] for row in rig.read_jsonl(env["run"] / "journal.jsonl")]
    assert journal.count("built") == journal.count("committed") + journal.count("vetoed")
    assert journal.count("committed") == len(fills)


# ---------------------------------------------------------------- T12 no money-movement words


MONEY_NAMES = {"approve", "setapprovalforall", "transfer", "transferfrom", "withdraw", "redeem", "redeempositions",
               "split", "splitposition", "merge", "mergepositions", "deposit"}
# The existing deny-list in podlib.public_get names "withdraw" in order to refuse it.
ALLOWED_DENY_LISTS = {("podlib.py", "withdraw"), ("m2_runner.py", "withdraw")}


def test_t12_no_money_movement_identifier_or_string_in_src():
    problems = []
    for path, tree in _src_trees():
        docs = _docstring_ids(tree)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
            elif isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute) and not (node.attr == "split" and isinstance(node.ctx, ast.Load)):
                names.append(node.attr)  # str.split is a string method, not a CTF split
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                names.append(node.value.strip())
            for name in names:
                if name.lower() in MONEY_NAMES and (path.name, name.lower()) not in ALLOWED_DENY_LISTS:
                    problems.append(f"{path.name}:{getattr(node, 'lineno', '?')} {name}")
    assert problems == []


# ---------------------------------------------------------------- T13 no WebSocket


def test_t13_the_runner_opens_no_websocket():
    assert not m2_runner.url_allowed("wss://ws-subscriptions-clob.polymarket.com/ws/market")
    assert not m2_runner.url_allowed("wss://ws-subscriptions-clob.polymarket.com/ws/user")
    text = RUNNER.read_text(encoding="utf-8")
    assert "ws_recorder" not in text.split('"""', 2)[2] and "check_outgoing_frame" not in text
