"""Resume-side fail-closed branches of src/halt_store.py the halt-consumer suite leaves open.

test_halt_consumer.py pins the veto path, record/duplicate, the resume ordering (bus line first), the
mutex-held release and the CLI exit codes. It never exercises the validation that runs *before* any
state is read: ``_require_text`` on a non-string credential (None / a number / a list / bytes, not
just a whitespace string), a ``checks`` payload that is not a dict, and a scope outside ``SCOPES``.
Nor does it resume a synthetic global id when nothing is wrong: ``halt-file`` with no logs/HALT, and
``halt-state-problem`` on a clean state — both must be ``halt_ref_not_active``, never a release.

Every refusal below happens before the ``resume`` bus line and before the state write, so each case
also proves nothing was released and nothing was logged.

Offline: temp paths only, no bus, no network, no real logs/.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import src.halt_store as halt_store

CAUSE = {"approved_by": "家伟", "root_cause": "postmortem done", "postmortem": "PR #1"}


def _record(path: Path, fallback: Path, **kw) -> dict:
    args = {"scope": "market", "market": "m1", "reason": "stale_book", "source": "Sentinel"}
    args.update(kw)
    return halt_store.record_halt(path=path, fallback=fallback, **args)


def _resume(path: Path, fallback: Path, bus_path: Path, **kw) -> dict:
    args = {"from_role": "Operator", "scope": "market", "market": "m1", "halt_id": "H0001-x",
            "approved_by": "家伟", "root_cause": "cause", "postmortem": "PR #1"}
    args.update(kw)
    return halt_store.resume(path=path, fallback=fallback, bus_path=bus_path, **args)


def _refused(tmp_path: Path, **kw) -> halt_store.ResumeRejected:
    """Run one resume that must be refused; return the rejection after proving nothing happened."""
    path, fallback, bus_path = tmp_path / "halt_state.json", tmp_path / "HALT", tmp_path / "bus.jsonl"
    record = _record(path, fallback)
    kw.setdefault("halt_id", record["id"])
    with pytest.raises(halt_store.ResumeRejected) as info:
        _resume(path, fallback, bus_path, **kw)
    assert not bus_path.exists(), "a refused resume must not log a bus line"
    assert halt_store.halt_problems("m1", path, fallback) == [f"halt_market:{record['id']}"]
    return info.value


# ---------------------------------------------------------------- arguments validated before anything else


@pytest.mark.parametrize(("field", "value"), [
    ("halt_id", None), ("halt_id", 123), ("halt_id", ["H0001-x"]), ("halt_id", b"H0001-x"),
    ("approved_by", None), ("approved_by", 7), ("approved_by", b"op"), ("approved_by", ["op"]),
    ("root_cause", 0), ("root_cause", None),
    ("postmortem", []), ("postmortem", 1.5),
])
def test_a_non_string_credential_is_missing_not_coerced(tmp_path, field, value):
    rejection = _refused(tmp_path, **{field: value})
    assert rejection.reason == f"missing_{'halt_ref' if field == 'halt_id' else field}"


@pytest.mark.parametrize("checks", [[], ["guard"], "guard", 0, 1.5, ("guard",), {"a": 1}.keys()])
def test_a_checks_payload_that_is_not_a_dict_is_refused(tmp_path, checks):
    assert _refused(tmp_path, checks=checks).reason == "bad_checks"


def test_checks_none_is_allowed_and_a_dict_is_reported_verbatim(tmp_path):
    path, fallback, bus_path = tmp_path / "halt_state.json", tmp_path / "HALT", tmp_path / "bus.jsonl"
    record = _record(path, fallback)
    out = _resume(path, fallback, bus_path, halt_id=record["id"], checks=None)
    assert out["released"] == record["id"]
    assert halt_store.read_state(path)[0][0]["resolved"]["approved_by"] == "家伟"


@pytest.mark.parametrize("scope", ["planet", "", "GLOBAL", "Market", None, 1])
def test_a_scope_outside_the_two_known_ones_is_refused(tmp_path, scope):
    assert _refused(tmp_path, scope=scope).reason == "bad_scope"


def test_a_from_role_that_is_not_exactly_the_operator_is_refused(tmp_path):
    # ``from_role`` is canonicalised, never stripped: a padded / lower-cased role is not the Operator.
    for role in ("  Operator  ", "operator", "OPERATOR", "Operator\n", None, 7):
        assert _refused(tmp_path, from_role=role).reason == "resume_not_operator"


# ---------------------------------------------------------------- the synthetic global ids with nothing wrong


def test_resuming_halt_file_without_a_fallback_file_releases_nothing(tmp_path):
    path, fallback, bus_path = tmp_path / "halt_state.json", tmp_path / "HALT", tmp_path / "bus.jsonl"
    with pytest.raises(halt_store.ResumeRejected) as info:
        _resume(path, fallback, bus_path, scope="global", halt_id=halt_store.FALLBACK_ID, market="")
    assert info.value.reason == "halt_ref_not_active"
    assert not bus_path.exists()
    assert not fallback.exists()
    assert not halt_store.state_marker(path).exists()  # nothing was written either


def test_resuming_the_state_problem_id_on_a_clean_state_releases_nothing(tmp_path):
    path, fallback, bus_path = tmp_path / "halt_state.json", tmp_path / "HALT", tmp_path / "bus.jsonl"
    for state in (None, {"version": 1, "halts": []}):  # a true first run, and an empty but valid state
        if state is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text('{"version": 1, "halts": []}', encoding="utf-8")
        with pytest.raises(halt_store.ResumeRejected) as info:
            _resume(path, fallback, bus_path, scope="global", halt_id=halt_store.STATE_PROBLEM_ID, market="")
        assert info.value.reason == "halt_ref_not_active"
    assert not bus_path.exists()
    assert not list(tmp_path.glob("halt_state.json.bad-*"))  # the good state was not moved aside


# ---------------------------------------------------------------- the same rule through the CLI


def test_the_cli_refuses_a_checks_payload_that_is_json_but_not_an_object(tmp_path, monkeypatch):
    sent: list = []
    monkeypatch.setattr(halt_store, "bus", lambda *a, **k: sent.append(a) or {"ts": "2026-10-14T00:00:00Z"})
    record = halt_store.record_halt(scope="market", market="m1", reason="stale_book", source="Sentinel")
    base = ["resume", "--halt-id", record["id"], "--market", "m1",
            "--approved-by", "家伟", "--root-cause", "cause", "--postmortem", "PR #1"]
    for checks in ("[]", '"guard"', "0", "1.5", "true"):
        assert halt_store.main(base + ["--scope", "market", "--checks", checks]) == halt_store.EXIT_REJECTED
        assert halt_store.halt_problems("m1") == [f"halt_market:{record['id']}"]
    assert sent == []
    assert halt_store.main(base + ["--scope", "market", "--checks", '{"pytest": "pass"}']) == halt_store.EXIT_OK
    assert halt_store.halt_problems("m1") == []
