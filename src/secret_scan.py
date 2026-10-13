"""Task AQ (Sentinel design gap 9): secret-string scan of the bus and the logs. Standard library only.

Design (Poly哨兵 review, event matrix): a private key or other secret seen in a log or on the bus
is SEV-1, a global halt. Only ``file:line`` (or the bus), the rule (category) and a short sha256
prefix of the matched value are reported. The matched text itself is never echoed: not in a
Finding, not in a halt reason, not in an exception, not on stdout.

Rules (no bare ``0x`` + 64 hex: transaction hashes, condition ids and token ids look like that):

* ``key_context:<word>``: a credential key name, then ``:`` or ``=`` (quotes, JSON escaping and a
  ``Bearer`` prefix allowed), then a value of at least SECRET_VALUE_MIN_LEN characters from the
  token alphabet that holds at least one letter and one digit. The key-name vocabulary is
  scripts/guard.py's CREDENTIAL_WORDS (each one, with ``-``, ``_`` or nothing between its parts)
  plus private/secret key, mnemonic, seed/recovery phrase, password, access/auth/refresh token,
  authorization. tests/test_secret_scan.py keeps it a superset of the guard's list.
* ``pem_private_key``: a PEM armour line ``BEGIN <type> PRIVATE KEY`` between five-dash fences
  (any type: RSA, EC, OPENSSH, ENCRYPTED, PGP ... BLOCK).
* ``bip39_mnemonic``: MNEMONIC_MIN_WORDS (12) or more consecutive BIP39 English words on one line,
  separated only by whitespace, commas, semicolons, bars or list numbers (so 12, 15, 18, 21 and
  24-word phrases all hit; no checksum test, so a mistyped phrase still hits). The word list is
  src/bip39_english.txt, pinned by sha256; a changed or missing list is a scan error.

Where it runs:

* ``podlib.bus()`` scans every message before it is written. A hit is not written; a global halt is
  recorded (halt_store.record_halt, source Sentinel) and SecretRefused is raised.
* ``python -m src.secret_scan`` scans logs/ (and any ``--path``). A hit or a file it cannot read is a
  global Sentinel halt (sentinel.halt: halt record, paper quotes cancelled, halt bus line);
  ``--no-halt`` only reports. Exit 0 clean, 1 secret found, 2 scan error (unreadable file or
  directory, bad word list), 3 usage.
* scripts/guard.py check ``secret_scan`` scans logs/ and every tracked file (read only, no halt).

Lifting the halt is the Operator's resume after the key is rotated and the logs are cleaned
(design: 若密钥泄露则已轮换并清理日志). Nothing here talks to the network or the exchange.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.thresholds import MNEMONIC_MIN_WORDS, SECRET_HASH_PREFIX_CHARS, SECRET_VALUE_MIN_LEN  # noqa: E402

WORDLIST_PATH = Path(__file__).resolve().with_name("bip39_english.txt")
# sha256 of the official BIP39 English list (bitcoin/bips bip-0039/english.txt, 2048 words, LF).
WORDLIST_SHA256 = "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"
WORDLIST_SIZE = 2048

RULE_KEY_CONTEXT = "key_context"
RULE_PEM = "pem_private_key"
RULE_MNEMONIC = "bip39_mnemonic"
RULE_SCAN_ERROR = "scan_error"
RULES = (RULE_KEY_CONTEXT, RULE_PEM, RULE_MNEMONIC)

EXIT_CLEAN = 0
EXIT_SECRET = 1
EXIT_SCAN_ERROR = 2
EXIT_USAGE = 3

HALT_SOURCE = "Sentinel"
HALT_REASON_PREFIX = "secret_suspected"

# Credential key names. Each entry is (label, parts): the parts may be joined by "-", "_", " " or
# nothing, case-insensitive. The first ten are scripts/guard.py CREDENTIAL_WORDS (Task AH).
KEY_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("api_key", ("api", "key")),
    ("secret", ("secret",)),
    ("passphrase", ("passphrase",)),
    ("signature", ("signature",)),
    ("private_key", ("private", "key")),
    ("poly_address", ("poly", "address")),
    ("nonce", ("nonce",)),
    ("priv_key", ("priv", "key")),
    ("mnemonic", ("mnemonic",)),
    ("seed_phrase", ("seed", "phrase")),
    ("recovery_phrase", ("recovery", "phrase")),
    ("password", ("password",)),
    ("passwd", ("passwd",)),
    ("access_token", ("access", "token")),
    ("auth_token", ("auth", "token")),
    ("refresh_token", ("refresh", "token")),
    ("authorization", ("authorization",)),
)
# Spellings of the guard's CREDENTIAL_WORDS this vocabulary covers (checked by the tests).
KEY_WORD_SPELLINGS = frozenset(
    sep.join(parts) for _, parts in KEY_WORDS for sep in ("", "_", "-", " ")
)

_VALUE_CHARS = r"A-Za-z0-9+/_\-.~"
_KEY_ALTS = "|".join(
    f"(?P<k{i}>{'[-_ ]?'.join(re.escape(p) for p in parts)})" for i, (_, parts) in enumerate(KEY_WORDS)
)
KEY_CONTEXT_RE = re.compile(
    r"(?:" + _KEY_ALTS + r")"            # the key word (maybe inside a longer name: POLY_API_KEY, apiSecret)
    r"[A-Za-z0-9_.\-]*"                    # the rest of that name
    r"\\*[\"']?\s*[:=]\s*\\*[\"']?"        # separator, quotes and JSON escaping
    r"(?:bearer\s+)?"
    r"(?P<value>[" + _VALUE_CHARS + r"]+=*)",
    re.IGNORECASE,
)
PEM_RE = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_WORD_RE = re.compile(r"[A-Za-z]+")
_MNEMONIC_SEP_RE = re.compile(r"[\s,;|]*(?:\d{1,2}[.):]?)?[\s,;|]*")


class ScanError(RuntimeError):
    """The scan itself could not run (unreadable file, unenumerable directory, bad word list).
    Fail closed: treat as a hit."""


class SecretRefused(RuntimeError):
    """bus() refused a message that holds a secret-looking string. The message text is never included."""


@dataclass(frozen=True)
class Finding:
    """One hit. Holds no matched text: location, rule, value length and a sha256 prefix only."""

    location: str
    line: int
    rule: str
    length: int
    digest: str

    def line_text(self) -> str:
        where = f"{self.location}:{self.line}" if self.line else self.location
        return f"{where} rule={self.rule} len={self.length} sha256={self.digest}"

    __str__ = line_text


def value_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()[:SECRET_HASH_PREFIX_CHARS]


_WORDS: frozenset[str] | None = None


def bip39_words(path: Path | None = None) -> frozenset[str]:
    """The pinned BIP39 English list. Raises ScanError if it is missing, changed or not 2048 words."""
    global _WORDS
    if path is None and _WORDS is not None:
        return _WORDS
    target = Path(path) if path is not None else WORDLIST_PATH
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise ScanError(f"bip39 word list unreadable ({type(exc).__name__})") from exc
    data = raw.replace(b"\r\n", b"\n")
    if hashlib.sha256(data).hexdigest() != WORDLIST_SHA256:
        raise ScanError("bip39 word list sha256 does not match the pin")
    words = frozenset(data.decode("ascii").split())
    if len(words) != WORDLIST_SIZE:
        raise ScanError("bip39 word list must hold 2048 words")
    if path is None:
        _WORDS = words
    return words


def _key_label(match: re.Match[str]) -> str:
    for i, (label, _) in enumerate(KEY_WORDS):
        if match.group(f"k{i}") is not None:
            return label
    return "key"  # pragma: no cover - one alternative always matched


def _secret_like(value: str) -> bool:
    core = value.rstrip("=")
    return (len(core) >= SECRET_VALUE_MIN_LEN and any(c.isalpha() for c in core)
            and any(c.isdigit() for c in core))


def _mnemonic_prefilter(text: str) -> bool:
    """Cheap superset gate for the mnemonic rule (issue #65).

    A run of MNEMONIC_MIN_WORDS BIP39 words holds at least that many letter tokens, so a text with
    fewer letter tokens cannot hide one. Separator-agnostic by construction: only
    ``_MNEMONIC_SEP_RE`` decides what joins words, so every separator form the exact rule allows is
    covered (the old bounded candidate regex capped each gap at six non-letters and dropped phrases
    the exact rule accepts: 7 spaces, long tabs, long numbered gaps). One linear pass over
    ``_WORD_RE`` matches -- no regex backtracking, bounded or unbounded.
    """
    count = 0
    for _ in _WORD_RE.finditer(text):
        count += 1
        if count >= MNEMONIC_MIN_WORDS:
            return True
    return False


def _mnemonic_runs(text: str, words: frozenset[str]) -> Iterator[str]:
    """Each run of >= MNEMONIC_MIN_WORDS BIP39 words joined only by allowed separators (normalised)."""
    text = re.sub(r"\\[nrt]", "  ", text)  # JSON-escaped line breaks count as whitespace
    run: list[str] = []
    last_end = 0
    for match in _WORD_RE.finditer(text):
        word = match.group(0)
        lower = word.lower()
        joined = bool(run) and _MNEMONIC_SEP_RE.fullmatch(text, last_end, match.start()) is not None
        if lower in words and word in (lower, lower.upper(), lower.capitalize()):
            if not joined and run:
                if len(run) >= MNEMONIC_MIN_WORDS:
                    yield " ".join(run)
                run = []
            run.append(lower)
        else:
            if len(run) >= MNEMONIC_MIN_WORDS:
                yield " ".join(run)
            run = []
        last_end = match.end()
    if len(run) >= MNEMONIC_MIN_WORDS:
        yield " ".join(run)


def scan_text(text: str, location: str = "<text>", line: int = 0,
              words: frozenset[str] | None = None) -> list[Finding]:
    """Findings in one piece of text (one log line or one serialised bus message)."""
    found: list[Finding] = []
    for match in PEM_RE.finditer(text):
        found.append(Finding(location, line, RULE_PEM, len(match.group(0)), value_digest(match.group(0))))
    for match in KEY_CONTEXT_RE.finditer(text):
        value = match.group("value")
        if _secret_like(value):
            found.append(Finding(location, line, f"{RULE_KEY_CONTEXT}:{_key_label(match)}", len(value),
                                 value_digest(value)))
    if _mnemonic_prefilter(text):
        vocab = words if words is not None else bip39_words()
        for phrase in _mnemonic_runs(text, vocab):
            found.append(Finding(location, line, RULE_MNEMONIC, len(phrase.split()), value_digest(phrase)))
    return found


def scan_file(path: Path, label: str | None = None, words: frozenset[str] | None = None) -> list[Finding]:
    """Findings in a file, line by line (bytes decoded as UTF-8 with replacement). Raises ScanError."""
    name = label or str(path)
    vocab = words if words is not None else bip39_words()
    found: list[Finding] = []
    try:
        with Path(path).open("rb") as handle:
            for lineno, raw in enumerate(handle, 1):
                found += scan_text(raw.decode("utf-8", "replace"), name, lineno, vocab)
    except OSError as exc:
        raise ScanError(f"{name} unreadable ({type(exc).__name__})") from exc
    return found


def is_env_file(name: str) -> bool:
    """``.env`` and ``.env.*`` hold credentials by convention: never opened by this scanner."""
    return name == ".env" or name.startswith(".env.")


def iter_files(base: Path) -> Iterator[Path]:
    """Every regular file under ``base`` (or ``base`` itself), sorted; symlinks are not followed.

    A directory that can be stat-ed but not enumerated is a :class:`ScanError` (issue #53): the
    walk's ``onerror`` converts the OSError into an explicit scan error instead of letting the walk
    drop it silently, so a scan that cannot see every file fails closed instead of returning clean.
    """
    if base.is_file():
        if not is_env_file(base.name):
            yield base
        return
    if not base.is_dir():
        return

    def _onerror(exc: OSError) -> None:
        name = Path(exc.filename).as_posix() if getattr(exc, "filename", None) else Path(base).as_posix()
        raise ScanError(f"{name} unreadable ({type(exc).__name__})") from exc

    for folder, dirs, files in os.walk(base, followlinks=False, onerror=_onerror):
        dirs.sort()
        for name in sorted(files):
            if is_env_file(name):
                continue  # never read .env files
            path = Path(folder) / name
            if path.is_file() and not path.is_symlink():
                yield path


def scan_paths(paths: Iterable[Path], root: Path | None = None) -> tuple[list[Finding], list[str]]:
    """``(findings, errors)`` for every file under ``paths``. Errors are short, never file contents.

    Errors are collected at both scan boundaries (issue #53): per file (``scan_file``) and per
    traversal (``iter_files`` cannot enumerate a directory), so an unreadable file or an
    unenumerable directory can never come back as a clean result.
    """
    findings: list[Finding] = []
    errors: list[str] = []
    try:
        vocab = bip39_words()
    except ScanError as exc:
        return [], [str(exc)]
    for base in paths:
        try:
            for path in iter_files(Path(base)):
                label = _label(path, root)
                try:
                    findings += scan_file(path, label, vocab)
                except ScanError as exc:
                    errors.append(str(exc))  # one unreadable file: keep scanning the rest
        except ScanError as exc:
            errors.append(str(exc))  # the traversal itself failed (issue #53): fail closed
    return findings, errors


def _label(path: Path, root: Path | None) -> str:
    if root is not None:
        try:
            return path.resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            pass
    return Path(path).as_posix()


def halt_reason(findings: list[Finding], errors: list[str]) -> str:
    """One deterministic line (so a rescan dedupes in halt_store). Locations, rules, digests only."""
    parts = [f"{HALT_REASON_PREFIX}: {len(findings)} finding(s), {len(errors)} scan error(s)"]
    parts += [finding.line_text() for finding in findings[:3]]
    if errors:
        parts.append("first error " + errors[0])
    return "; ".join(parts)


def check_message(message: dict) -> None:
    """bus() hook: refuse a message with a secret-looking string. Never raises anything but SecretRefused.

    On a hit (or if the scan itself fails) a global halt is recorded first and the message is not
    written. Only rules, lengths and digests are in the halt and the exception.
    """
    import json

    try:
        text = json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str)
        findings = scan_text(text, "bus")
        errors: list[str] = []
    except Exception as exc:  # noqa: BLE001 - a scan that cannot run is a hit (fail closed)
        findings, errors = [], [f"bus scan failed ({type(exc).__name__})"]
    if not findings and not errors:
        return
    reason = halt_reason(findings, errors)
    try:
        from src import halt_store  # local: halt_store imports podlib

        halt_store.record_halt(scope=halt_store.SCOPE_GLOBAL, reason=reason, source=HALT_SOURCE)
    except Exception:  # noqa: BLE001 - the refusal below still stops the write
        pass
    raise SecretRefused(f"bus message refused: {reason}")


def canaries() -> list[tuple[str, str]]:
    """Fresh random fake secrets, one per rule (built at run time; nothing like them is committed)."""
    words = sorted(bip39_words())
    pick = secrets.SystemRandom()
    hex_key = "0x" + secrets.token_hex(32)
    pem = "-" * 5 + "BEGIN EC " + "PRIVATE" + " KEY" + "-" * 5
    phrase = " ".join(pick.choice(words) for _ in range(12))
    return [
        (RULE_KEY_CONTEXT, '{"' + "private" + '_key": "' + hex_key + '"}'),
        (RULE_KEY_CONTEXT, "POLY_API_" + "SECRET=" + secrets.token_urlsafe(32) + "7a"),
        (RULE_PEM, pem),
        (RULE_MNEMONIC, '{"note": "' + phrase + '"}'),
    ]


def self_test() -> list[str]:
    """Problems if the scanner misses a fresh canary or flags a bare 0x+64hex hash. Never echoes them."""
    problems: list[str] = []
    try:
        planted = canaries()
    except ScanError as exc:
        return [f"cannot run ({exc})"]
    for rule, text in planted:
        hits = [f.rule.split(":")[0] for f in scan_text(text, "canary")]
        if rule not in hits:
            problems.append(f"canary for {rule} not detected")
    bare = '{"transaction_hash": "0x' + secrets.token_hex(32) + '", "condition_id": "0x' + secrets.token_hex(32) + '"}'
    if scan_text(bare, "canary"):
        problems.append("a bare 0x+64hex hash was flagged (design: never match it without a key name)")
    return problems


# ---------------------------------------------------------------- CLI (Sentinel / Operator)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.secret_scan",
        description="Scan logs/ (the bus included) for private-key / secret strings. A hit is a global "
                    "Sentinel halt (SEV-1). Only file:line, rule, length and a sha256 prefix are printed.",
        epilog="Exit codes: 0 clean; 1 secret found; 2 scan error (unreadable file, bad word list); 3 usage.",
    )
    parser.add_argument("--path", action="append", type=Path, default=None,
                        help="file or folder to scan (repeatable; default: logs/)")
    parser.add_argument("--no-halt", action="store_true", help="report only; do not record a halt")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_CLEAN if exc.code == 0 else EXIT_USAGE
    from src.podlib import LOG_DIR  # local: podlib imports this module for the bus hook

    paths = args.path or [LOG_DIR]
    findings, errors = scan_paths(paths, ROOT)
    for finding in findings:
        print(f"SECRET {finding.line_text()}")
    for error in errors:
        print(f"ERROR {error}")
    code = EXIT_SECRET if findings else EXIT_SCAN_ERROR if errors else EXIT_CLEAN
    halted = "no"
    if code != EXIT_CLEAN and not args.no_halt:
        from src import sentinel

        message = sentinel.halt(reason=halt_reason(findings, errors), scope="global", source=HALT_SOURCE)
        halted = str(message.get("payload", {}).get("halt_id") or "fallback")
    print(f"secret_scan: findings={len(findings)} errors={len(errors)} halt={halted} exit={code} exchange_action=none")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
