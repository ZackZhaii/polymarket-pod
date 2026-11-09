"""EX author tests: pure explicit-slot facts, not independent QC."""
import importlib
import importlib.util


def api():
    assert importlib.util.find_spec("src.m2_slot_ledger") is not None, "EX API absent on actual base"
    module = importlib.import_module("src.m2_slot_ledger")
    assert callable(getattr(module, "reconcile_slots", None)), "EX API missing"
    return module.reconcile_slots


def slot(label, when="2026-11-04T05:00:00Z", proc="m2_runner"):
    return {"slot_id": label, "planned_at_utc": when, "expected_proc": proc}


def attempt(label, ident, report=None):
    return {"slot_id": label, "attempt_id": ident, "report": report}


def test_empty_plan_is_not_a_health_evaluation():
    result = api()([], [])
    assert result == {"schema": "m2_slot_ledger/1", "status": "NOT_EVALUATED",
                      "declaration_status": "UNVERIFIED", "metrics": None,
                      "denominator": None, "healthy_online_us": None,
                      "slots": [], "attempts": []}


def test_missing_middle_slot_remains_unobserved_with_unknown_reason():
    plan = [slot("a", "2026-11-04T05:00:00Z"),
            slot("b", "2026-11-04T05:00:30Z"),
            slot("c", "2026-11-04T05:01:00Z")]
    result = api()(plan, [attempt("a", "1"), attempt("c", "2")])
    middle = result["slots"][1]
    assert middle["original"] == plan[1]
    assert middle["candidate_attempt_indexes"] == []
    assert middle["state"] == "UNOBSERVED" and middle["reason"] == "UNKNOWN"
    assert [r["candidate_slot_indexes"] for r in result["attempts"]] == [[0], [2]]
    assert result["healthy_online_us"] is None and result["denominator"] is None


import base64
import copy
import hashlib
import pytest


def report(raw=b"not-json\xff"):
    return {"status": "NOT_EVALUATED", "completeness": "INVENTORIED",
            "input": {"source": "D:/tmp/never-read.json", "format": "json",
                      "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                      "records": 1, "problems": [], "duplicate_of": None,
                      "completeness": "INVENTORIED"},
            "declaration": None, "declaration_problems": [], "metrics": None,
            "denominator": None, "healthy_online_us": None,
            "capture": {"capture_id": "independent-capture-label", "expected_proc": "m2_runner",
                        "started_at_utc": "2026-11-04T05:08:00Z",
                        "finished_at_utc": "2026-11-04T05:08:01Z",
                        "raw_base64": base64.b64encode(raw).decode("ascii")}}


def one(rep):
    return api()([slot("s")], [attempt("s", "a", rep)])["attempts"][0]


def test_three_time_layers_and_late_attempt_are_never_reassigned():
    rep = report()
    rep["declaration"] = {"started_at": "2026-11-01T00:00:00Z", "ts": "2026-11-02T00:00:00Z",
                          "state": "running", "seq": 999999, "proc": "unverified-imposter"}
    plans = [slot("first", "2026-11-04T05:00:00Z"), slot("gap", "2026-11-04T05:00:30Z"),
             slot("last", "2026-11-04T05:01:00Z")]
    rows = [attempt("first", "a", rep), attempt("last", "b", copy.deepcopy(rep))]
    before = copy.deepcopy((plans, rows))
    result = api()(plans, rows)
    assert (plans, rows) == before
    assert [v["original"] for v in result["slots"]] == plans
    assert [v["original"] for v in result["attempts"]] == rows
    assert result["slots"][1]["state"] == "UNOBSERVED"
    assert result["slots"][1]["reason"] == "UNKNOWN"
    assert [v["candidate_slot_indexes"] for v in result["attempts"]] == [[0], [2]]
    assert [v["raw_state"] for v in result["attempts"]] == ["VERIFIED", "VERIFIED"]
    assert set(result) == {"schema", "status", "declaration_status", "metrics", "denominator",
                           "healthy_online_us", "slots", "attempts"}
    assert result["status"] == "NOT_EVALUATED" and result["declaration_status"] == "UNVERIFIED"
    assert all(result[k] is None for k in ("metrics", "denominator", "healthy_online_us"))


def test_duplicate_slot_attempt_ids_and_multiple_rows_preserve_all_candidates():
    plans = [slot("dup"), slot("other"), slot("dup")]
    rows = [attempt("dup", "repeat"), attempt("other", "repeat"), attempt("dup", "unique"),
            attempt("unknown", "orphan")]
    result = api()(plans, rows)
    assert [v["index"] for v in result["slots"]] == [0, 1, 2]
    assert [v["index"] for v in result["attempts"]] == [0, 1, 2, 3]
    assert [v["candidate_attempt_indexes"] for v in result["slots"]] == [[0, 2], [1], [0, 2]]
    assert [v["candidate_slot_indexes"] for v in result["attempts"]] == [[0, 2], [1], [0, 2], []]
    assert [v["association"] for v in result["attempts"]] == ["AMBIGUOUS", "UNIQUE", "AMBIGUOUS", "UNMATCHED"]
    for i in (0, 2):
        assert "DUPLICATE_SLOT_ID" in result["slots"][i]["problems"]
        assert "MULTIPLE_ATTEMPTS" in result["slots"][i]["problems"]
        assert "DUPLICATE_SLOT_ID" in result["attempts"][i]["problems"]
        assert "MULTIPLE_ATTEMPTS" in result["attempts"][i]["problems"]
    assert "DUPLICATE_ATTEMPT_ID" in result["attempts"][0]["problems"]
    assert "DUPLICATE_ATTEMPT_ID" in result["attempts"][1]["problems"]
    assert "DUPLICATE_ATTEMPT_ID" not in result["attempts"][2]["problems"]
    assert "UNMATCHED_SLOT" in result["attempts"][3]["problems"]


def test_different_slots_same_sha_are_not_one_sample_or_duplicate_attempt():
    result = api()([slot("a"), slot("b")], [attempt("a", "one", report()), attempt("b", "two", report())])
    assert len(result["attempts"]) == 2
    assert [r["raw_state"] for r in result["attempts"]] == ["VERIFIED", "VERIFIED"]
    assert all(not r["problems"] for r in result["attempts"])
    assert [r["candidate_attempt_indexes"] for r in result["slots"]] == [[0], [1]]


def test_exact_labels_whitespace_case_and_capture_id_are_not_normalized():
    rep = report(); rep["capture"]["capture_id"] = "not-attempt-id"
    result = api()([slot(" x "), slot("x"), slot(" ")],
                   [attempt(" x ", "id", rep), attempt("X", "other", rep), attempt(" ", " ", rep)])
    assert [r["candidate_slot_indexes"] for r in result["attempts"]] == [[0], [], [2]]
    assert result["attempts"][0]["original"]["report"]["capture"]["capture_id"] == "not-attempt-id"
    assert result["attempts"][2]["attempt_id"] == " "


@pytest.mark.parametrize("bad", [None, 7, [], {}, {"slot_id": "s"}, {"slot_id": False},
                                  {"slot_id": "s", "planned_at_utc": "bad", "expected_proc": 8, "extra": 1}])
def test_finite_bad_plan_rows_retained_and_usable_slot_still_associates(bad):
    result = api()([bad], [attempt("s", "a")])
    row = result["slots"][0]
    assert row["index"] == 0 and row["original"] == bad and row["problems"]
    usable = type(bad) is dict and bad.get("slot_id") == "s"
    assert result["attempts"][0]["candidate_slot_indexes"] == ([0] if usable else [])
    assert row["state"] == ("ATTEMPTS_SUPPLIED" if usable else "INVALID_SLOT")


@pytest.mark.parametrize("bad", [None, 7, [], {}, {"slot_id": "s"}, {"slot_id": False},
                                  {"slot_id": "s", "attempt_id": "a", "report": None, "archive_receipt": "unverified"}])
def test_bad_attempt_rows_stay_in_order_even_with_invalid_report_or_extra_fields(bad):
    result = api()([slot("s")], [bad])
    row = result["attempts"][0]
    assert row["index"] == 0 and row["original"] == bad and row["problems"]
    assert row["report_problems"] == ["report_shape"]
    assert row["raw_state"] == "UNKNOWN"
    usable = type(bad) is dict and bad.get("slot_id") == "s"
    assert row["association"] == ("UNIQUE" if usable else "INVALID")
    if usable:
        assert result["slots"][0]["state"] == "ATTEMPTS_SUPPLIED"


def test_returned_rows_are_deep_snapshots_and_input_aliases_are_not_cycles():
    shared = slot("s"); rep = report(); plans = [shared, shared]; rows = [attempt("s", "a", rep)]
    result = api()(plans, rows)
    shared["expected_proc"] = "changed"; rep["capture"]["raw_base64"] = "changed"
    assert result["slots"][0]["original"]["expected_proc"] == "m2_runner"
    assert result["slots"][1]["original"]["expected_proc"] == "m2_runner"
    assert result["attempts"][0]["original"]["report"]["capture"]["raw_base64"] != "changed"
    result["slots"][0]["original"]["expected_proc"] = "output-only"
    assert result["slots"][1]["original"]["expected_proc"] == "m2_runner"
    assert shared["expected_proc"] == "changed"


@pytest.mark.parametrize("field,value,problem", [
    ("status", "HEALTHY", "report_contract"), ("metrics", {}, "report_contract"),
    ("denominator", 1, "report_contract"), ("healthy_online_us", 1, "report_contract"),
    ("completeness", [], "report_contract"), ("declaration", True, "declaration_type"),
    ("declaration_problems", None, "declaration_problems_type"),
    ("declaration_problems", [False], "declaration_problems_type")])
def test_report_problems_do_not_hide_verified_byte_binding(field, value, problem):
    rep = report(); rep[field] = value
    row = one(rep)
    assert row["raw_state"] == "VERIFIED" and problem in row["report_problems"]
    assert row["original"]["report"][field] == value


@pytest.mark.parametrize("target,key", [("root", "metrics"), ("input", "source"), ("capture", "capture_id")])
def test_wrong_closed_keysets_remain_visible_without_erasing_extractable_raw(target, key):
    rep = report(); dest = rep if target == "root" else rep[target]; del dest[key]
    row = one(rep)
    assert row["raw_state"] == "VERIFIED"
    assert {"root": "report_shape", "input": "input_shape", "capture": "capture_shape"}[target] in row["report_problems"]


def test_inconsistent_input_completeness_is_diagnosed_without_repair():
    rep = report(); rep["input"]["completeness"] = "INVALID"
    row = one(rep)
    assert "report_contract" in row["report_problems"] and row["raw_state"] == "VERIFIED"
    assert row["original"]["report"]["input"]["completeness"] == "INVALID"


@pytest.mark.parametrize("rep", [None, [], 1, {}, {"input": None, "capture": {"raw_base64": None}},
                                 {"input": {}, "capture": {}}, {"input": {}, "capture": []}])
def test_unextractable_raw_is_unknown(rep):
    assert one(rep)["raw_state"] == "UNKNOWN"


def test_missing_raw_and_captured_empty_bytes_are_distinct():
    missing = report(); missing["capture"]["raw_base64"] = None
    assert one(missing)["raw_state"] == "MISSING"
    empty = one(report(b""))
    assert empty["raw_state"] == "VERIFIED" and empty["original"]["report"]["input"]["bytes"] == 0


@pytest.mark.parametrize("encoded", ["Zg", "Zh==", "Zg==\n", "Zg===", "_w==", "heart♥", 1, True, [], {}])
def test_noncanonical_or_nonstring_base64_is_invalid(encoded):
    rep = report(b"f"); rep["capture"]["raw_base64"] = encoded
    assert one(rep)["raw_state"] == "INVALID"


@pytest.mark.parametrize("sha", [None, 7, "F" * 64, "a" * 63, "b" * 64])
def test_digest_must_be_exact_lowercase_matching_sha(sha):
    rep = report(); rep["input"]["sha256"] = sha
    assert one(rep)["raw_state"] == "INVALID"


@pytest.mark.parametrize("size", [None, True, False, -1, 1.0, 999])
def test_byte_count_is_exact_nonbool_nonnegative_integer(size):
    rep = report(b"f"); rep["input"]["bytes"] = size
    assert one(rep)["raw_state"] == "INVALID"


def test_raw_binding_neither_parses_json_nor_authenticates_declared_producer():
    rep = report(b"\xffbad JSON")
    rep["declaration"] = {"proc": "imposter", "started_at": "2026-11-04T05:01:00Z", "ts": "2026-11-04T05:00:00Z"}
    row = one(rep)
    assert row["raw_state"] == "VERIFIED" and "reversed_producer_times" in row["time_problems"]
    assert row["original"]["report"]["declaration"]["proc"] == "imposter"


@pytest.mark.parametrize("bad", [None, False, 1, "bad", "2026-11-04", "2026-11-04T05:00:00+00:00"])
def test_bad_plan_time_diagnoses_but_never_controls_slot_association(bad):
    result = api()([slot("s", bad)], [attempt("s", "a", report())])
    assert "invalid_planned_at_utc" in result["slots"][0]["problems"]
    assert result["slots"][0]["original"]["planned_at_utc"] == bad
    assert result["attempts"][0]["association"] == "UNIQUE"


def test_bad_and_reversed_capture_and_producer_times_stay_separate():
    rep = report(); rep["capture"]["started_at_utc"] = "2026-11-04T05:10:00Z"
    rep["declaration"] = {"started_at": "bad", "ts": None}
    row = one(rep)
    assert row["time_problems"] == ["reversed_capture_times", "invalid_producer_started_at", "invalid_producer_ts"]
    assert row["raw_state"] == "VERIFIED" and row["original"]["report"] == rep
    rep["capture"]["started_at_utc"] = None; rep["capture"]["finished_at_utc"] = False
    row = one(rep)
    assert row["time_problems"][:2] == ["invalid_capture_started_at_utc", "invalid_capture_finished_at_utc"]


def test_proc_mismatch_only_for_a_unique_literal_association():
    rep = report(); rep["capture"]["expected_proc"] = "different"
    unique = one(rep)
    assert "expected_proc_mismatch" in unique["problems"]
    ambiguous = api()([slot("s"), slot("s")], [attempt("s", "a", rep)])
    assert "expected_proc_mismatch" not in ambiguous["attempts"][0]["problems"]


@pytest.mark.parametrize("left,right", [(None, []), ({}, []), ((), []), ([], "rows"), ([], {})])
def test_top_level_nonlists_reject_instead_of_iterating(left, right):
    with pytest.raises(ValueError):
        api()(left, right)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), {1: "key"},
                                 "\ud800", {"\ud800": 1}, ("tuple",)])
def test_nonfinite_nonjson_or_unencodable_nested_values_reject_whole_input(bad):
    with pytest.raises(ValueError):
        api()([slot("s")], [attempt("s", "a", report()), bad])


def test_cycles_reject_and_custom_conversion_hooks_are_never_called():
    cycle = []; cycle.append(cycle)
    with pytest.raises(ValueError):
        api()([], cycle)
    mapping = {}; mapping["cycle"] = mapping
    with pytest.raises(ValueError):
        api()([mapping], [])
    hits = []
    class Custom:
        def __str__(self):
            hits.append("str"); raise AssertionError("no conversion")
        def __fspath__(self):
            hits.append("fspath"); raise AssertionError("no conversion")
        def __iter__(self):
            hits.append("iter"); raise AssertionError("no conversion")
    class DictSubclass(dict):
        def items(self):
            hits.append("items"); raise AssertionError("no subclass")
    class StringSubclass(str):
        def encode(self, *args, **kwargs):
            hits.append("encode"); raise AssertionError("no subclass")
    for bad in (Custom(), DictSubclass(), StringSubclass("x")):
        with pytest.raises(ValueError):
            api()([bad], [])
    assert hits == []


def test_exact_output_row_schemas_and_no_input_derived_health_fields():
    result = api()([slot("s")], [attempt("s", "a", report())])
    assert set(result["slots"][0]) == {"index", "original", "slot_id", "candidate_attempt_indexes", "state", "reason", "problems"}
    assert set(result["attempts"][0]) == {"index", "original", "slot_id", "attempt_id", "candidate_slot_indexes", "association", "raw_state", "problems", "report_problems", "time_problems"}
    assert all(result[k] is None for k in ("metrics", "denominator", "healthy_online_us"))


def test_cold_app_import_and_call_have_zero_application_io_clock_or_runtime_calls(monkeypatch):
    import builtins
    import datetime as date_module
    import io
    import os
    from pathlib import Path
    import socket
    import subprocess
    import sys
    import time
    import binascii
    import math
    import re
    from datetime import datetime, timedelta
    # All stdlib dependencies are warm; Python module-code loading is not app IO.
    rep = report(); plans = [slot("s")]; rows = [attempt("s", "a", rep)]
    saved = sys.modules.pop("src.m2_slot_ledger", None)
    before = set(sys.modules); hits = []
    def forbidden(*args, **kwargs):
        hits.append("applicationIO"); raise AssertionError("application IO forbidden")
    class NoClock(datetime):
        @classmethod
        def now(cls, *args, **kwargs):
            return forbidden()
        @classmethod
        def utcnow(cls):
            return forbidden()
    try:
        with monkeypatch.context() as m:
            for obj, names in ((builtins, ["open"]), (io, ["open"]),
                               (Path, ["open", "stat", "lstat", "resolve", "exists"]),
                               (socket, ["socket", "create_connection"]), (subprocess, ["Popen"]),
                               (time, ["time", "time_ns", "monotonic", "monotonic_ns", "sleep"])):
                for name in names:
                    m.setattr(obj, name, forbidden)
            m.setattr(date_module, "datetime", NoClock)
            module = importlib.import_module("src.m2_slot_ledger")
            # Importlib code-loading metadata is permitted; all app call metadata is blocked.
            for name in ("stat", "lstat", "listdir", "scandir"):
                m.setattr(os, name, forbidden)
            result = module.reconcile_slots(plans, rows)
        assert hits == [] and result["attempts"][0]["raw_state"] == "VERIFIED"
        added = set(sys.modules) - before
        assert not any(name.startswith("src.") and name != "src.m2_slot_ledger" for name in added)
    finally:
        if saved is not None:
            sys.modules["src.m2_slot_ledger"] = saved
            setattr(sys.modules["src"], "m2_slot_ledger", saved)


def test_recursion_resource_failure_propagates_instead_of_partial_business_result():
    deep = []
    for _ in range(3000):
        deep = [deep]
    with pytest.raises(RecursionError):
        api()([], deep)
