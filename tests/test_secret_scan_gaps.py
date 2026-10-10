"""Gap fill: the rule boundaries and the fail-closed file handling of src/secret_scan.py.

Covered elsewhere (tests/test_secret_scan.py): the credential vocabulary, the PEM armour, the
mnemonic separators and the issue #65 prefilter, the word-list pin, the CLI exit codes and the
``podlib.bus()`` hook. Missing there, and added here:

* ``_secret_like`` (secret_scan.py:176-179): the SECRET_VALUE_MIN_LEN boundary (15 characters is not
  a secret, 16 is), the ``rstrip("=")`` rule (base64 padding does not count toward the minimum but is
  part of the reported length) and the "at least one letter and one digit" rule. A longer value is
  still not a secret without both.
* ``_mnemonic_runs`` (secret_scan.py:209-214): only lower case, UPPER CASE and Capitalised BIP39
  words join a run. A word in any other case breaks it, and two halves below MNEMONIC_MIN_WORDS are
  no phrase.
* ``halt_reason`` (secret_scan.py:306-312): bounded to the first three findings plus the first error
  text, so the reason stays deterministic (halt_store dedupes on it) and never echoes a value.
* ``Finding.line_text`` (secret_scan.py:134-136): a finding without a line number (the bus) reports
  its location alone, never a ``:0``.
* ``_label`` (secret_scan.py:297-303) and ``scan_file`` (secret_scan.py:247-252): a path outside the
  scan root keeps its full location instead of a wrong relative one, a missing path scans nothing,
  and an unreadable path is a ScanError (CLI exit 2), never a silent clean scan.

Offline: temp files only, no network, no orders, no secrets (every fake value is built here at test
time).
"""
from __future__ import annotations

import pytest

import src.secret_scan as secret_scan

# The first twelve BIP39 English words: a valid phrase, fixed so the test never depends on a draw.
WORDS12 = ("abandon ability able about above absent absorb abstract absurd abuse access accident").split()


def rules(text: str) -> list[str]:
    return [finding.rule for finding in secret_scan.scan_text(text, "t")]


# ---------------------------------------------------------------- key_context value boundaries

def test_a_value_needs_the_full_minimum_length_before_it_is_a_secret():
    short = "abcdefghij1234A"   # 15 characters: under SECRET_VALUE_MIN_LEN
    long = "abcdefghij12345A"   # 16 characters: at the minimum
    assert len(short) == secret_scan.SECRET_VALUE_MIN_LEN - 1
    assert len(long) == secret_scan.SECRET_VALUE_MIN_LEN
    assert rules(f"api_key={short}") == []
    (found,) = secret_scan.scan_text(f"api_key={long}", "logs/x.log", 3)
    assert (found.rule, found.length, found.line) == ("key_context:api_key", 16, 3)
    assert long not in found.line_text()  # the value itself is never echoed


def test_base64_padding_is_stripped_for_the_length_rule_but_counted_in_the_report():
    assert rules("api_key=abcdefghij1234==") == []  # a 14-character core, whatever the padding
    (found,) = secret_scan.scan_text("api_key=abcdefghij123456==")  # a 16-character core
    assert (found.rule, found.length) == ("key_context:api_key", 18)
    assert found.digest == secret_scan.value_digest("abcdefghij123456==")


@pytest.mark.parametrize("value", [
    "abcdefghijklmnopqrst",    # 20 letters, no digit
    "01234567890123456789",    # 20 digits, no letter
    "++++++++++++++++",        # long, but neither a letter nor a digit
])
def test_a_long_value_without_a_letter_and_a_digit_is_not_a_secret(value):
    assert len(value) >= secret_scan.SECRET_VALUE_MIN_LEN
    assert rules(f"api_key={value}") == []
    assert rules(f"POLY_API_KEY: '{value}'") == []


# ---------------------------------------------------------------- mnemonic case rule

def test_the_three_accepted_cases_join_a_run_and_any_other_case_breaks_it():
    assert rules(" ".join(WORDS12)) == ["bip39_mnemonic"]
    assert rules(" ".join(word.upper() for word in WORDS12)) == ["bip39_mnemonic"]
    assert rules(" ".join(word.capitalize() for word in WORDS12)) == ["bip39_mnemonic"]
    mixed = list(WORDS12)
    mixed[5] = mixed[5][0] + mixed[5][1:].upper()  # "absent" -> "aBSENT": none of the three cases
    assert mixed[5] == "aBSENT"
    assert rules(" ".join(mixed)) == []  # 5 words, then 6: neither half is a phrase
    assert rules(" ".join(WORDS12[:6]) + " zzzz " + " ".join(WORDS12[6:])) == []


# ---------------------------------------------------------------- bounded, location-only reporting

def test_halt_reason_names_the_first_three_findings_and_the_first_error_only():
    text = " ".join(f"api_key=abcdefghij12345{suffix}" for suffix in "ABCD")  # 16 characters each
    findings = secret_scan.scan_text(text, "logs/x.log", 3)
    assert [finding.rule for finding in findings] == ["key_context:api_key"] * 4
    reason = secret_scan.halt_reason(findings, ["boom one", "boom two"])
    assert reason.startswith("secret_suspected: 4 finding(s), 2 scan error(s); ")
    assert reason.count("logs/x.log:3 rule=key_context:api_key") == 3
    assert f"sha256={findings[3].digest}" not in reason  # the fourth is counted, not listed
    assert reason.endswith("; first error boom one")
    assert "boom two" not in reason


def test_a_finding_without_a_line_number_reports_its_location_alone():
    finding = secret_scan.Finding("bus", 0, secret_scan.RULE_MNEMONIC, 12, "deadbeef")
    assert finding.line_text() == "bus rule=bip39_mnemonic len=12 sha256=deadbeef"
    assert str(finding) == finding.line_text()


# ---------------------------------------------------------------- fail-closed file handling

def test_a_path_outside_the_root_keeps_its_location_and_a_missing_path_scans_nothing(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("api_key=abcdefghij12345A\n", encoding="utf-8")
    (found,) = secret_scan.scan_paths([outside], root)[0]
    assert (found.location, found.line) == (outside.as_posix(), 1)  # never a wrong relative label
    assert secret_scan.scan_paths([tmp_path / "nope"], root) == ([], [])


def test_an_unreadable_path_is_a_scan_error_not_a_clean_scan(tmp_path):
    folder = tmp_path / "adir"
    folder.mkdir()
    with pytest.raises(secret_scan.ScanError, match="adir unreadable"):
        secret_scan.scan_file(folder, "adir")
