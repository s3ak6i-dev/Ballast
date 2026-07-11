"""Fallback machinery (TechSpec §2.4–2.5): response cache + difficulty classifier.

The chain itself lives in the interceptor (it owns the call context); this
module provides the two smart rungs:

    1. ResponseCache — TTL cache keyed on a hash of (dependency, args, kwargs);
       populated by healthy calls, served only when falling back.
    2. RulesClassifier — decides whether a request is simple enough for a
       cheaper model. Rules-based for v1 (explainable); any object with
       ``eligible_for_cheaper(args, kwargs) -> bool`` can replace it.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Any

from ._clock import Clock, monotonic

#: Signals of structured output or multi-step reasoning: keep on the big model.
DEFAULT_REASONING_KEYWORDS: tuple[str, ...] = (
    "json", "schema", "xml", "yaml", "sql",
    "step by step", "step-by-step", "chain of thought",
    "reason", "prove", "derive", "analyze", "compare",
)


class RulesClassifier:
    """TechSpec §2.5: short prompt + no reasoning/structured-output keywords
    → eligible for a cheaper model. The prompt is the first string argument."""

    def __init__(
        self,
        max_prompt_chars: int = 2000,
        reasoning_keywords: tuple[str, ...] = DEFAULT_REASONING_KEYWORDS,
    ) -> None:
        self.max_prompt_chars = max_prompt_chars
        self.reasoning_keywords = reasoning_keywords

    def eligible_for_cheaper(self, args: tuple, kwargs: dict) -> bool:
        prompt = next(
            (a for a in (*args, *kwargs.values()) if isinstance(a, str)), None
        )
        if prompt is None:
            return False  # nothing to inspect — stay on the original model
        if len(prompt) > self.max_prompt_chars:
            return False
        lowered = prompt.lower()
        return not any(keyword in lowered for keyword in self.reasoning_keywords)


class ResponseCache:
    """Thread-safe TTL cache. One per runtime, shared across dependencies."""

    def __init__(self, *, clock: Clock = monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[float, Any]] = {}  # key -> (expires_at, value)
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _key(dependency: str, args: tuple, kwargs: dict) -> str:
        raw = repr((dependency, args, sorted(kwargs.items())))
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, dependency: str, args: tuple, kwargs: dict) -> tuple[bool, Any]:
        """Returns (hit, value); expired entries count as misses and are dropped."""
        key = self._key(dependency, args, kwargs)
        now = self._clock()
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and now < entry[0]:
                self._hits += 1
                return True, entry[1]
            if entry is not None:
                del self._entries[key]
            self._misses += 1
            return False, None

    def put(self, dependency: str, args: tuple, kwargs: dict, value: Any, ttl_s: float) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be > 0")
        key = self._key(dependency, args, kwargs)
        with self._lock:
            self._entries[key] = (self._clock() + ttl_s, value)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._entries), "hits": self._hits, "misses": self._misses}
