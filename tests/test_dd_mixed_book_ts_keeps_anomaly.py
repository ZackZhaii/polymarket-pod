"""Task DD (Poly风控, #275 follow-up): one tick with one valid book and one book whose book_ts is invalid must
not clear clock_anomaly. Offline rig only: no network, no credentials, no orders."""
from __future__ import annotations

import pytest

from src import m2_runner
from tests.test_m2_runner_recovery import _skew_rig, _tick, env  # noqa: F401


@pytest.mark.parametrize("bad_ts", [None, "not-a-time", "nan", "inf"])
def test_one_valid_and_one_invalid_book_ts_keeps_the_clock_anomaly(env, bad_ts):  # noqa: F811
    clock, fake, state = _skew_rig(env)
    _tick(state, clock, fake)
    assert state.clock_anomaly == "clock_skew"
    fake.skew_s = None
    calls = {"n": 0}
    sane = fake.book

    def mixed(url: str):
        body = sane(url)
        if url == "https://clob.polymarket.com/time":  # #253: the independent clock source, left clean
            return body
        body = dict(body)
        calls["n"] += 1
        if calls["n"] % 2 == 0:  # every second book of the tick has an invalid timestamp
            if bad_ts is None:
                body.pop("timestamp", None)
            else:
                body["timestamp"] = bad_ts
        return body

    fake.book = mixed
    _tick(state, clock, fake, 60.0)
    assert calls["n"] >= 2, "the tick must fetch a valid and an invalid book"
    assert state.clock_anomaly is not None
    assert m2_runner.runner_clock_ok(state) is False
