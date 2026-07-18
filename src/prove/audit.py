"""Sample-pool purity audit — the mechanism that keeps admission's "controlled environment"
assumption honest and supplies attribution's rule-defect cross-check.

Every sample in the verified pool passed the validator when it joined (that pass is why it is in
the pool). So re-validating the *immutable* pool with the current validator is a controlled probe:
the samples did not change, therefore any rule that now fires on them is a rule that changed —
i.e. a corrupted/over-tightened validator rule producing false failures. That is exactly the
signal attribution needs to tell a rule_defect (blame the rule) from a skill_defect (blame the
skill): a healthy skill's production failures carrying only corrupted rules are the rule's account.

Two uses, one mechanism: run periodically as a purity guard, or on-demand when a failure batch
fires so attribution can cross-check the batch's rules against the pool before charging anyone.
"""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass
from typing import Callable

from .sample_pool import SamplePool
from .schemas import ExtractionResult
from .validator import validate


@dataclass
class AuditReport:
    fingerprint: str
    n_sampled: int
    pass_rate: float                 # fraction of the re-validated pool that still passes
    corrupted_rules: list[str]       # rules firing on the previously-passing pool → suspect

    def anomalous(self) -> bool:
        return bool(self.corrupted_rules)


class PoolAuditor:
    def __init__(
        self,
        validator: Callable = validate,
        *,
        anomaly_frac: float = 0.2,
        sample_n: int = 20,
        seed: int = 0,
    ):
        self._validate = validator
        self.anomaly_frac = anomaly_frac
        self.sample_n = sample_n
        self._rng = random.Random(seed)

    def audit(self, pool: SamplePool, fingerprint: str) -> AuditReport:
        """Re-validate a random slice of one fingerprint's live pool. A rule is flagged corrupted
        when it fires on at least `anomaly_frac` of the slice (systematic false failures), never on
        a single fluke."""
        samples = pool.samples_for(fingerprint)
        if not samples:
            return AuditReport(fingerprint, 0, 1.0, [])
        chosen = (samples if len(samples) <= self.sample_n
                  else self._rng.sample(samples, self.sample_n))
        hits: Counter = Counter()
        passed = 0
        for s in chosen:
            verdict = self._validate(
                ExtractionResult(doc_id=s.doc_id, fields=s.fields, source="llm"), None)
            passed += int(verdict.passed)
            hits.update(verdict.rule_failures)
        floor = max(2, math.ceil(self.anomaly_frac * len(chosen)))
        corrupted = sorted(r for r, c in hits.items() if c >= floor)
        return AuditReport(fingerprint, len(chosen), round(passed / len(chosen), 3), corrupted)

    def audit_all(self, pool: SamplePool) -> list[AuditReport]:
        return [self.audit(pool, fp) for fp in pool.fingerprints()]
