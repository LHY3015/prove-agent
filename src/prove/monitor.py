"""Production failure accumulation and self-healing triggers.

A per-skill sliding window of validation outcomes, plus the deprecation decision. Two paths:

  - FAST (window): once a skill has served at least `min_window` docs, a window failure rate
    above `failure_rate_threshold` AND at least `min_failures` failures in the window trips a
    deprecation. The absolute floor keeps a *young* skill from being killed by a couple of
    noise-induced failures early in its life (routing noise in A2).
  - SLOW (ledger floor): the discounted-Beta confidence dropping below `confidence_floor` is
    the backstop — it kills only sustained low-success skills that the fast path missed.

Windows are keyed by `skill_id` (a resynthesized skill is a new id, so it starts with a clean
window and cannot inherit the deprecated skill's failure history). Covers trial AND active
skills — a trial skill that starts failing is deprecable before it ever promotes.

Phase 3 has no attribution: a fired trigger deprecates the skill directly. Attribution (Phase 4)
will interpose on the fast path to decide whether the batch is actually the skill's fault.
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class Monitor:
    def __init__(
        self,
        *,
        window: int = 20,
        failure_rate_threshold: float = 0.2,
        confidence_floor: float = 0.3,
        min_window: int = 10,
        min_failures: int = 3,
    ):
        self.window = window
        self.failure_rate_threshold = failure_rate_threshold
        self.confidence_floor = confidence_floor
        self.min_window = min_window
        self.min_failures = min_failures
        self._windows: dict[str, deque[bool]] = {}

    def record(self, skill_id: str, passed: bool) -> None:
        w = self._windows.setdefault(skill_id, deque(maxlen=self.window))
        w.append(passed)

    def should_deprecate(self, skill_id: str, confidence: float) -> Optional[str]:
        """Return a deprecation reason string, or None. Call after `record` for the same doc."""
        w = self._windows.get(skill_id)
        if w and len(w) >= self.min_window:
            failures = sum(1 for p in w if not p)
            if failures >= self.min_failures and failures / len(w) > self.failure_rate_threshold:
                return f"failure_batch:window({failures}/{len(w)})"
        if confidence < self.confidence_floor:
            return f"confidence_floor({confidence:.3f})"
        return None

    def drop(self, skill_id: str) -> None:
        """Discard a deprecated skill's window (its id is retired; a new skill gets a fresh one)."""
        self._windows.pop(skill_id, None)
