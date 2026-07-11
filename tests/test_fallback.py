"""Fallback router contract (TechSpec §2.4–2.5): cache, classifier, and the
chain order inside @guarded."""

import pytest

import ballast
from ballast import BudgetExceededError, EventType, RulesClassifier, guarded
from ballast._clock import FakeClock
from ballast.fallback import ResponseCache


def fallback_events(seen):
    return [e for e in seen if e.event_type is EventType.FALLBACK_USED]


class TestRulesClassifier:
    def test_short_plain_prompt_is_eligible(self):
        clf = RulesClassifier()
        assert clf.eligible_for_cheaper(("What color is the sky?",), {}) is True

    def test_long_prompt_not_eligible(self):
        clf = RulesClassifier(max_prompt_chars=50)
        assert clf.eligible_for_cheaper(("x" * 51,), {}) is False

    def test_reasoning_keywords_not_eligible(self):
        clf = RulesClassifier()
        assert clf.eligible_for_cheaper(("Return the answer as JSON",), {}) is False
        assert clf.eligible_for_cheaper((), {"prompt": "think step by step"}) is False

    def test_no_string_args_not_eligible(self):
        clf = RulesClassifier()
        assert clf.eligible_for_cheaper((42,), {"n": 3}) is False


class TestResponseCache:
    def test_put_get_hit(self):
        clock = FakeClock()
        cache = ResponseCache(clock=clock)
        cache.put("api", ("q",), {}, "answer", ttl_s=60)
        hit, value = cache.get("api", ("q",), {})
        assert hit is True and value == "answer"

    def test_expiry(self):
        clock = FakeClock()
        cache = ResponseCache(clock=clock)
        cache.put("api", ("q",), {}, "answer", ttl_s=60)
        clock.advance(61)
        hit, _ = cache.get("api", ("q",), {})
        assert hit is False
        assert cache.stats()["entries"] == 0  # expired entry dropped

    def test_key_includes_dependency_and_args(self):
        cache = ResponseCache()
        cache.put("api_a", ("q",), {}, "a", ttl_s=60)
        assert cache.get("api_b", ("q",), {})[0] is False
        assert cache.get("api_a", ("other",), {})[0] is False

    def test_stats(self):
        cache = ResponseCache()
        cache.put("api", ("q",), {}, "a", ttl_s=60)
        cache.get("api", ("q",), {})
        cache.get("api", ("miss",), {})
        assert cache.stats() == {"entries": 1, "hits": 1, "misses": 1}


class TestChainOrder:
    def test_cache_served_first_when_breaker_open(self):
        rt = ballast.configure()
        calls = []

        @guarded(dependency="api", cache_ttl_s=300, fallback="static")
        def call(prompt):
            calls.append(prompt)
            return f"live:{prompt}"

        assert call("q") == "live:q"  # healthy call populates the cache
        rt.breaker("api").force_open()
        seen: list = []
        ballast.subscribe(seen.append)
        assert call("q") == "live:q"  # served from cache, not "static"
        assert len(calls) == 1  # real callable not touched again
        assert fallback_events(seen)[0].detail == {"reason": "breaker_open", "rung": "cache"}

    def test_cheaper_used_when_no_cache_entry(self):
        rt = ballast.configure()
        rt.breaker("api").force_open()
        seen: list = []
        ballast.subscribe(seen.append)

        @guarded(dependency="api", cache_ttl_s=300,
                 cheaper=lambda prompt: f"cheap:{prompt}", fallback="static")
        def call(prompt):
            raise AssertionError("must not run")

        assert call("hi") == "cheap:hi"
        assert fallback_events(seen)[0].detail["rung"] == "cheaper_model"

    def test_static_when_classifier_rejects_cheaper(self):
        rt = ballast.configure()
        rt.breaker("api").force_open()

        @guarded(dependency="api", cheaper=lambda p: "cheap", fallback="static")
        def call(prompt):
            raise AssertionError("must not run")

        # "json" keyword → not eligible for the cheaper model → static rung.
        assert call("give me json") == "static"

    def test_raises_original_when_chain_empty(self):
        rt = ballast.configure()
        rt.breaker("api").force_open()

        @guarded(dependency="api")
        def call(prompt):
            raise AssertionError("must not run")

        with pytest.raises(ballast.CircuitOpenError):
            call("q")


class TestBudgetIntegration:
    def test_hard_ceiling_refuse_raises(self):
        rt = ballast.configure(budget_usd_per_hour=1.0, budget_hard_behavior="refuse")
        rt.budget.record(1.5)

        @guarded(dependency="api", fallback="static")
        def call():
            return "live"

        with pytest.raises(BudgetExceededError):
            call()

    def test_hard_ceiling_downgrade_uses_chain(self):
        rt = ballast.configure(budget_usd_per_hour=1.0)  # downgrade (default)
        rt.budget.record(1.5)
        seen: list = []
        ballast.subscribe(seen.append)

        @guarded(dependency="api", fallback="static")
        def call():
            raise AssertionError("must not run")

        assert call() == "static"
        assert fallback_events(seen)[0].detail["reason"] == "budget_hard"

    def test_soft_ceiling_downgrades_eligible_calls(self):
        rt = ballast.configure(budget_usd_per_hour=10.0)
        rt.budget.record(9.0)  # above soft (8.0), below hard
        seen: list = []
        ballast.subscribe(seen.append)

        @guarded(dependency="api", cheaper=lambda p: f"cheap:{p}")
        def call(prompt):
            raise AssertionError("must not run — soft ceiling should downgrade")

        assert call("simple question") == "cheap:simple question"
        assert fallback_events(seen)[0].detail == {"reason": "budget_soft", "rung": "cheaper_model"}

    def test_soft_ceiling_keeps_hard_prompts_on_primary(self):
        rt = ballast.configure(budget_usd_per_hour=10.0)
        rt.budget.record(9.0)

        @guarded(dependency="api", cheaper=lambda p: "cheap")
        def call(prompt):
            return "live"

        assert call("analyze this schema as json") == "live"  # not eligible → primary

    def test_cost_fn_records_spend(self):
        rt = ballast.configure(budget_usd_per_hour=10.0)

        @guarded(dependency="api", cost_fn=lambda result: 0.25)
        def call():
            return "live"

        call()
        call()
        assert rt.budget.spent() == pytest.approx(0.5)
