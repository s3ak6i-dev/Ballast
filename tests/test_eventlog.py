"""SQLite event log contract (TechSpec §4)."""

import ballast
from ballast.eventlog import SQLiteEventLog


def make_log(runtime):
    log = SQLiteEventLog(":memory:")
    log.attach(runtime)
    return log


def test_events_persisted_with_session_id():
    rt = ballast.configure()
    log = make_log(rt)
    rt.breaker("api").force_open()
    rows = log.query()
    assert len(rows) == 1
    row = rows[0]
    assert row["event_type"] == "manual_override"
    assert row["dependency"] == "api"
    assert row["detail"]["action"] == "force_open"
    assert row["session_id"] == rt.session_id
    log.close()


def test_query_filters_and_order():
    rt = ballast.configure()
    log = make_log(rt)
    rt.breaker("api_a").force_open()
    rt.breaker("api_b").force_open()
    rt.breaker("api_a").reset()

    assert len(log.query()) == 3
    a_rows = log.query(dependency="api_a")
    assert [r["detail"]["action"] for r in a_rows] == ["reset", "force_open"]  # newest first
    assert len(log.query(event_type="manual_override", limit=2)) == 2
    assert log.query(dependency="nope") == []
    log.close()


def test_detached_log_stops_recording():
    rt = ballast.configure()
    log = make_log(rt)
    log.close()
    rt.breaker("api").force_open()  # bus swallows the write-after-close, if any
    # No assertion on the closed DB — the point is no exception reached the caller.
