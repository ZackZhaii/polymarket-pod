"""Task AQ (Sentinel design gap 9): secret-string scan of the bus and logs.

Every fake secret here is generated at test time (secrets.token_hex / token_urlsafe / random BIP39
words); nothing resembling a key is committed. Tests also check that no output, halt reason,
exception or file written by the scanner contains the matched text.
"""
from __future__ import annotations

import json
import random
import secrets
import shutil
from pathlib import Path

import pytest

import src.halt_store as halt_store
import src.podlib as podlib
import src.secret_scan as secret_scan
from scripts import guard

ROOT = Path(__file__).resolve().parents[1]
DASHES = "-" * 5


def fake_hex_key() -> str:
    return "0x" + secrets.token_hex(32)


def fake_token(n: int = 32) -> str:
    return secrets.token_urlsafe(n) + "9z"  # always a letter and a digit


def fake_phrase(n: int = 12, rng: random.Random | None = None) -> list[str]:
    words = sorted(secret_scan.bip39_words())
    pick = rng or random.SystemRandom()
    return [pick.choice(words) for _ in range(n)]


def pem(kind: str) -> str:
    return DASHES + "BEGIN " + kind + "PRIVATE" + " KEY" + (" BLOCK" if kind.startswith("PGP") else "") + DASHES


def rules(text: str) -> list[str]:
    return [finding.rule for finding in secret_scan.scan_text(text, "t")]


def assert_not_echoed(secret: str, *outputs: object) -> None:
    for output in outputs:
        text = output if isinstance(output, str) else repr(output)
        assert secret not in text
        if " " in secret:  # a phrase: not even its first three words
            assert " ".join(secret.split()[:3]) not in text


# ---------------------------------------------------------------- word list

def test_word_list_is_the_pinned_bip39_english_list():
    words = secret_scan.bip39_words()
    assert len(words) == 2048 == secret_scan.WORDLIST_SIZE
    assert {"abandon", "zoo"} <= words


def test_a_changed_or_missing_word_list_is_a_scan_error(tmp_path):
    changed = tmp_path / "list.txt"
    lf = secret_scan.WORDLIST_PATH.read_bytes().replace(b"\r\n", b"\n")  # a Windows checkout may be CRLF
    changed.write_bytes(lf.replace(b"zoo\n", b"zoom\n"))
    with pytest.raises(secret_scan.ScanError, match="sha256"):
        secret_scan.bip39_words(changed)
    with pytest.raises(secret_scan.ScanError, match="unreadable"):
        secret_scan.bip39_words(tmp_path / "missing.txt")


def test_a_crlf_checkout_of_the_word_list_still_matches_the_pin(tmp_path):
    crlf = tmp_path / "list.txt"
    lf = secret_scan.WORDLIST_PATH.read_bytes().replace(b"\r\n", b"\n")
    crlf.write_bytes(lf.replace(b"\n", b"\r\n"))
    assert len(secret_scan.bip39_words(crlf)) == 2048


# ---------------------------------------------------------------- key_context

@pytest.mark.parametrize("template, label", [
    ('{{"private_key": "{v}"}}', "private_key"),
    ("PRIVATE_KEY={v}", "private_key"),
    ("POLY_PRIVATE_KEY = '{v}'", "private_key"),
    ("privateKey: {v}", "private_key"),
    ("private key: {v}", "private_key"),
    ('"{{\\"api_key\\": \\"{v}\\"}}"', "api_key"),
    ("POLY_API_KEY={v}", "api_key"),
    ("apiKey={v}", "api_key"),
    ('{{"api-key":"{v}"}}', "api_key"),
    ("CLOB_SECRET={v}", "secret"),
    ('{{"apiSecret": "{v}"}}', "secret"),
    ("POLY_PASSPHRASE={v}", "passphrase"),
    ("POLY_SIGNATURE: {v}", "signature"),
    ("POLY_ADDRESS={v}", "poly_address"),
    ("POLY_NONCE={v}", "nonce"),
    ("Authorization: Bearer {v}", "authorization"),
    ("access_token={v}", "access_token"),
    ("password={v}", "password"),
    ("mnemonic: {v}", "mnemonic"),
    ("priv_key={v}", "priv_key"),
])
@pytest.mark.parametrize("make", [fake_hex_key, fake_token])
def test_a_credential_name_with_a_long_value_is_found(template, label, make):
    value = make()
    found = secret_scan.scan_text(template.format(v=value), "logs/x.log", 7)
    assert [f.rule for f in found] == [f"key_context:{label}"]
    assert found[0].length == len(value) and found[0].line == 7
    assert_not_echoed(value, found, found[0].line_text(), secret_scan.halt_reason(found, []))


def test_the_vocabulary_covers_the_guard_credential_words():
    """Design: reuse guard.py's word list. Every CREDENTIAL_WORDS spelling is a key word here."""
    for word in guard.CREDENTIAL_WORDS:
        assert word in secret_scan.KEY_WORD_SPELLINGS, word
        assert rules(f"{word}={fake_token()}") == [f"key_context:{word.replace('-', '_').replace('privatekey', 'private_key').replace('apikey', 'api_key')}"]


@pytest.mark.parametrize("text", [
    # design: never a bare 0x+64hex (tx hashes, condition ids, token ids)
    '{{"transaction_hash": "{h}", "condition_id": "{h}", "asset_id": "{h}", "market": "{h}"}}',
    '{{"id": "{h}", "hash": "{h}", "token_id": "1234567890123456789012345678901234567890"}}',
    "tx {h} filled",
    # a key name but no secret-like value
    "private_key=None", 'private_key = os.environ["POLY_PRIVATE_KEY"]', "api_key=${{POLY_API_KEY}}",
    '{{"signature": "missing_required_signature_field"}}', "nonce=12", "secret=abc123",
    "password: <redacted>", "POLY_API_KEY=", "the secret is safe",
    # public keys and certificates are not private keys
    DASHES + "BEGIN PUBLIC KEY" + DASHES, DASHES + "BEGIN CERTIFICATE" + DASHES,
])
def test_no_false_positive(text):
    assert rules(text.format(h=fake_hex_key())) == []


def test_hash_prefix_is_short_and_matches_the_value():
    value = fake_hex_key()
    (found,) = secret_scan.scan_text(f"private_key={value}")
    assert len(found.digest) == secret_scan.SECRET_HASH_PREFIX_CHARS == 8
    assert found.digest == secret_scan.value_digest(value)


# ---------------------------------------------------------------- PEM

@pytest.mark.parametrize("kind", ["", "RSA ", "EC ", "OPENSSH ", "ENCRYPTED ", "DSA ", "PGP "])
def test_pem_private_key_armour_is_found(kind):
    header = pem(kind)
    assert rules(header) == ["pem_private_key"]
    assert rules(json.dumps({"key": header + "\nMIIE" + secrets.token_hex(8) + "\n"})) == ["pem_private_key"]


# ---------------------------------------------------------------- BIP39

@pytest.mark.parametrize("n", [12, 15, 18, 21, 24, 30])
def test_mnemonic_runs_are_found(n):
    words = fake_phrase(n)
    found = secret_scan.scan_text('{"note": "' + " ".join(words) + '"}')
    assert [f.rule for f in found] == ["bip39_mnemonic"]
    assert found[0].length == n
    assert_not_echoed(" ".join(words), found, secret_scan.halt_reason(found, []))


@pytest.mark.parametrize("sep", [" ", "  ", ", ", ",", "\t", "\\n", " | ", "; "])
def test_mnemonic_separators(sep):
    assert rules(sep.join(fake_phrase(12))) == ["bip39_mnemonic"]


def test_numbered_and_capitalised_mnemonics_are_found():
    words = fake_phrase(12)
    assert rules(" ".join(f"{i}. {w}" for i, w in enumerate(words, 1))) == ["bip39_mnemonic"]
    assert rules(" ".join(w.upper() for w in words)) == ["bip39_mnemonic"]
    assert rules(" ".join(w.capitalize() for w in words)) == ["bip39_mnemonic"]


def test_eleven_words_or_a_broken_run_is_not_a_mnemonic():
    words = fake_phrase(12)
    assert rules(" ".join(words[:11])) == []
    assert rules(" ".join(words[:6]) + " the " + " ".join(words[6:])) == []
    assert rules("-".join(words)) == []  # hyphens join words into one token, not a phrase


def test_prose_in_the_repo_docs_is_not_a_mnemonic():
    for path in sorted((ROOT / "docs").rglob("*.md")):
        assert secret_scan.scan_file(path) == [], path


def test_seeded_random_word_streams_hit_only_on_long_enough_runs():
    rng = random.Random(20261009)
    words = sorted(secret_scan.bip39_words())
    for _ in range(200):
        n = rng.randint(1, 30)
        text = " ".join(rng.choice(words) for _ in range(n))
        assert (rules(text) == ["bip39_mnemonic"]) == (n >= 12)


# ---------------------------------------------------------------- self-test and canaries

def test_self_test_passes_and_canaries_are_fresh():
    assert secret_scan.self_test() == []
    assert secret_scan.canaries() != secret_scan.canaries()


def test_a_weakened_rule_fails_the_self_test(monkeypatch):
    import re

    monkeypatch.setattr(secret_scan, "PEM_RE", re.compile(r"(?!x)x"))
    assert secret_scan.self_test() == ["canary for pem_private_key not detected"]


def test_a_rule_that_flags_bare_hashes_fails_the_self_test(monkeypatch):
    import re

    monkeypatch.setattr(secret_scan, "PEM_RE", re.compile(r"0x[0-9a-f]{64}|" + secret_scan.PEM_RE.pattern))
    assert any("bare 0x+64hex" in p for p in secret_scan.self_test())


# ---------------------------------------------------------------- files and the CLI

@pytest.fixture
def logs(tmp_path, monkeypatch):
    folder = tmp_path / "logs"
    folder.mkdir()
    monkeypatch.setattr(podlib, "LOG_DIR", folder)
    monkeypatch.setattr(podlib, "BUS_PATH", folder / "bus.jsonl")
    return folder


def write_bus(folder: Path, payloads: list[dict]) -> None:
    with (folder / "bus.jsonl").open("w", encoding="utf-8") as handle:
        for payload in payloads:
            handle.write(json.dumps({"ts": "2026-10-09T00:00:00Z", "from": "Quant", "to": "RiskGate", "type": "signal",
                                     "market": "m", "payload": payload}) + "\n")


def test_scan_paths_reports_file_line_rule_only(logs):
    key = fake_hex_key()
    write_bus(logs, [{"tx": fake_hex_key()}, {"private_key": key}])
    (logs / "sub").mkdir()
    (logs / "sub" / "run.log").write_text("ok\n" + pem("RSA ") + "\n", encoding="utf-8")
    findings, errors = secret_scan.scan_paths([logs], logs.parent)
    assert errors == []
    assert [(f.location, f.line, f.rule) for f in findings] == [
        ("logs/bus.jsonl", 2, "key_context:private_key"), ("logs/sub/run.log", 2, "pem_private_key")]
    assert_not_echoed(key, findings)


def test_env_files_are_never_opened(logs, monkeypatch):
    (logs / ".env").write_text(f"PRIVATE_KEY={fake_hex_key()}\n", encoding="utf-8")
    (logs / ".env.local").write_text(f"PRIVATE_KEY={fake_hex_key()}\n", encoding="utf-8")
    opened: list[str] = []
    real_open = Path.open

    def spy(self, *args, **kwargs):
        opened.append(self.name)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)
    assert secret_scan.scan_paths([logs, logs / ".env"], logs.parent) == ([], [])
    assert not [name for name in opened if name.startswith(".env")]


def test_cli_clean_exit_0_and_no_halt(logs, capsys):
    write_bus(logs, [{"tx": fake_hex_key(), "note": "fill ok"}])
    assert secret_scan.main([]) == 0
    assert "findings=0 errors=0 halt=no exit=0" in capsys.readouterr().out
    assert halt_store.active_halts() == []


def test_cli_hit_is_a_global_sentinel_halt_without_echo(logs, capsys):
    key, phrase = fake_hex_key(), " ".join(fake_phrase(24))
    write_bus(logs, [{"POLY_PRIVATE_KEY": key}, {"note": phrase}])
    assert secret_scan.main([]) == secret_scan.EXIT_SECRET == 1
    out = capsys.readouterr().out
    assert "logs/bus.jsonl:1 rule=key_context:private_key len=66 sha256=" in out
    assert "logs/bus.jsonl:2 rule=bip39_mnemonic len=24 sha256=" in out
    (record,) = halt_store.active_halts()
    assert record["scope"] == "global" and record["source"] == "Sentinel"
    assert record["reason"].startswith("secret_suspected: 2 finding(s)")
    assert halt_store.halt_problems("any-market") == [f"halt_global:{record['id']}"]
    halt_line = json.loads((logs / "bus.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert halt_line["type"] == "halt" and halt_line["payload"]["exchange_action"] == "none"
    for text in (out, halt_store.HALT_STATE_PATH.read_text(encoding="utf-8"), json.dumps(halt_line)):
        assert_not_echoed(key, text)
        assert_not_echoed(phrase, text)
    # a rescan finds the same thing and does not stack a second halt
    assert secret_scan.main([]) == 1
    assert len(halt_store.active_halts()) == 1


def test_cli_no_halt_only_reports(logs, capsys):
    write_bus(logs, [{"api_secret": fake_token()}])
    assert secret_scan.main(["--no-halt"]) == 1
    assert halt_store.active_halts() == []


def test_cli_scan_error_fails_closed(logs, monkeypatch, capsys, tmp_path):
    bad = tmp_path / "list.txt"
    bad.write_text("abandon\n", encoding="utf-8")
    monkeypatch.setattr(secret_scan, "WORDLIST_PATH", bad)
    monkeypatch.setattr(secret_scan, "_WORDS", None)
    write_bus(logs, [{"note": "ok"}])
    assert secret_scan.main([]) == secret_scan.EXIT_SCAN_ERROR == 2
    assert "ERROR bip39 word list" in capsys.readouterr().out
    # fail closed twice over: the halt record, then the halt bus line is refused too (its scan cannot
    # run without the list), so logs/HALT is written as well. Every one is global.
    active = halt_store.active_halts()
    assert active and {r["scope"] for r in active} == {"global"}
    assert halt_store.fallback_active()


def test_cli_extra_path_and_usage(logs, tmp_path, capsys):
    run_dir = tmp_path / "RUN"
    run_dir.mkdir()
    (run_dir / "events.jsonl").write_text(json.dumps({"passphrase": secrets.token_hex(32)}) + "\n", encoding="utf-8")
    assert secret_scan.main(["--path", str(run_dir), "--no-halt"]) == 1
    assert secret_scan.main(["--bogus"]) == secret_scan.EXIT_USAGE == 3


# ---------------------------------------------------------------- the bus hook

def test_bus_refuses_a_secret_writes_nothing_and_halts_globally(tmp_path):
    path = tmp_path / "bus.jsonl"
    key = fake_hex_key()
    with pytest.raises(secret_scan.SecretRefused) as caught:
        podlib.bus("Quant", "RiskGate", "signal", "m", {"edge": 0.03, "debug": {"private_key": key}}, path=path)
    assert not path.exists()
    assert_not_echoed(key, str(caught.value))
    (record,) = halt_store.active_halts()
    assert record["scope"] == "global" and "key_context:private_key" in record["reason"]
    assert_not_echoed(key, halt_store.HALT_STATE_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize("payload", [
    lambda: {"note": " ".join(fake_phrase(12))},
    lambda: {"blob": pem("OPENSSH ")},
    lambda: {"headers": {"Authorization": "Bearer " + fake_token()}},
])
def test_bus_refuses_every_rule(tmp_path, payload):
    with pytest.raises(secret_scan.SecretRefused):
        podlib.bus("NewsForecaster", "RiskGate", "signal", "m", payload(), path=tmp_path / "bus.jsonl")
    assert not (tmp_path / "bus.jsonl").exists()


def test_bus_still_writes_hashes_and_ids(tmp_path):
    path = tmp_path / "bus.jsonl"
    h = fake_hex_key()
    message = podlib.bus("Executor", "Sentinel", "fill", "m",
                         {"risk_decision": "allow", "transaction_hash": h, "condition_id": h, "token_id": "7" * 77},
                         path=path)
    assert json.loads(path.read_text(encoding="utf-8")) == message
    assert halt_store.active_halts() == []


def test_a_failing_bus_scan_fails_closed(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise MemoryError("boom")

    monkeypatch.setattr(secret_scan, "scan_text", broken)
    with pytest.raises(secret_scan.SecretRefused, match="bus scan failed"):
        podlib.bus("Quant", "RiskGate", "signal", "m", {"edge": 0.03}, path=tmp_path / "bus.jsonl")
    assert not (tmp_path / "bus.jsonl").exists()
    assert [r["scope"] for r in halt_store.active_halts()] == ["global"]


def test_the_repo_tracked_files_are_clean():
    files, _ = guard.repo_files(ROOT)
    assert len(files) > 50
    findings, errors = secret_scan.scan_paths(files, ROOT)
    assert (findings, errors) == ([], [])
