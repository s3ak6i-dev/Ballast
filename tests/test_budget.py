"""Budget tracker contract (PRD P1, TechSpec §2.4 budget trigger)."""

import pytest

from ballast import EventType
from ballast._clock import FakeClock
from ballast.budget import BudgetTracker


def make_tracker(budget=10.0, **kwargs):
    clock = FakeClock()
    events: list = []
    tracker = BudgetTracker(budget, clock=clock, emit=events.append, **kwargs)
    return tracker, events, clock


class TestDisabled:
    def test_no_budget_never_exceeds(self):
        tracker, events, _ = make_tracker(budget=None)
        tracker.record(1_000_000.0)
        assert tracker.soft_exceeded is False
        assert tracker.hard_exceeded is False
        assert events == []
        assert tracker.status()["enabled"] is False


class TestWindow:
    def test_spend_accumulates_and_decays(self):
        tracker, _, clock = make_tracker()
        tracker.record(2.0)
        clock.advance(1800)
        tracker.record(3.0)
        assert tracker.spent() == pytest.approx(5.0)
        clock.advance(1801)  # first entry is now > 3600s old
        assert tracker.spent() == pytest.approx(3.0)
        clock.advance(3600)
        assert tracker.spent() == pytest.approx(0.0)

    def test_negative_spend_rejected(self):
        tracker, _, _ = make_tracker()
        with pytest.raises(ValueError):
            tracker.record(-1.0)


class TestCeilings:
    def test_soft_then_hard_edges_fire_once(self):
        tracker, events, _ = make_tracker(budget=10.0)  # soft at 8.0
        tracker.record(7.0)
        assert tracker.soft_exceeded is False
        tracker.record(1.5)  # 8.5: soft crossed
        assert tracker.soft_exceeded is True and tracker.hard_exceeded is False
        tracker.record(0.5)  # still above soft — no second event
        tracker.record(2.0)  # 11.0: hard crossed
        assert tracker.hard_exceeded is True
        types = [e.event_type for e in events]
        assert types.count(EventType.BUDGET_SOFT_CEILING) == 1
        assert types.count(EventType.BUDGET_HARD_CEILING) == 1

    def test_ceiling_rearms_after_decay(self):
        tracker, events, clock = make_tracker(budget=10.0)
        tracker.record(9.0)  # soft crossed
        clock.advance(3601)  # decays out
        assert tracker.soft_exceeded is False
        tracker.record(9.0)  # soft crossed again → second event
        types = [e.event_type for e in events]
        assert types.count(EventType.BUDGET_SOFT_CEILING) == 2

    def test_status_fields(self):
        tracker, _, _ = make_tracker(budget=10.0, hard_behavior="refuse")
        tracker.record(4.0)
        status = tracker.status()
        assert status == {
            "enabled": True,
            "budget_usd_per_hour": 10.0,
            "soft_ceiling_usd": 8.0,
            "spent_window_usd": 4.0,
            "soft_exceeded": False,
            "hard_exceeded": False,
            "hard_behavior": "refuse",
        }


class TestValidation:
    def test_invalid_config_rejected(self):
        with pytest.raises(ValueError):
            BudgetTracker(-1.0)
        with pytest.raises(ValueError):
            BudgetTracker(10.0, soft_margin=1.5)
        with pytest.raises(ValueError):
            BudgetTracker(10.0, hard_behavior="explode")
