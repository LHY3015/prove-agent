"""Admission harness: the held-out regression gate that decides candidate -> trial.

This is the skill lifecycle's first review. It is a *controlled* environment — routing is
bypassed, the distribution is the format's own verified pool, and the validator rules are
frozen — so a failure here indicts the skill directly and needs no attribution (attribution is
pre-completed by the experimental design; see PROJECT_RECORD §3.4).

Split discipline (guards against a trainable gate): the holdout (30%, min 3) is chosen once per
fingerprint, deterministically, and its doc_ids are PERSISTED for the whole admission episode —
synthesis retries after a rejection re-use the same holdout, and any later pool arrivals go to
training only. Synthesis never sees holdout samples.

Oracle: the candidate's sandbox output is compared to each holdout sample's OWN validated pool
`fields` (self-supervised — the pool is the system's definition of truth), by semantic
field match (parsed money/date, exact otherwise).
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import asdict, dataclass, field

from .sample_pool import PoolSample
from .sandbox import run_skill
from .schemas import TARGET_FIELDS
from .validator import compare_fields


@dataclass
class AdmissionReport:
    passed: bool
    holdout_f1: float
    holdout_n: int
    mismatches: int              # wrong (field, doc) instances over the holdout
    threshold: float
    errors: int = 0              # holdout docs where the skill raised / was blocked
    per_doc: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class Admission:
    def __init__(
        self,
        *,
        holdout_frac: float = 0.3,
        min_holdout: int = 3,
        f1_threshold: float = 0.95,
        seed: int = 0,
        cpu_seconds: int = 5,
        mem_mb: int = 512,
    ):
        self.holdout_frac = holdout_frac
        self.min_holdout = min_holdout
        self.f1_threshold = f1_threshold
        self.seed = seed
        self.cpu_seconds = cpu_seconds
        self.mem_mb = mem_mb
        self._holdout_ids: dict[str, set[str]] = {}  # fingerprint -> frozen holdout doc_ids

    # ---- split -----------------------------------------------------------

    def split(
        self, fingerprint: str, samples: list[PoolSample]
    ) -> tuple[list[PoolSample], list[PoolSample]]:
        """Return (train, holdout). The holdout is fixed per fingerprint across retries; new
        samples always land in train."""
        if fingerprint not in self._holdout_ids:
            self._holdout_ids[fingerprint] = self._choose_holdout(fingerprint, samples)
        held = self._holdout_ids[fingerprint]
        holdout = [s for s in samples if s.doc_id in held]
        train = [s for s in samples if s.doc_id not in held]
        return train, holdout

    def reset_holdout(self, fingerprint: str) -> None:
        """Drop the frozen holdout for a fingerprint so the next split re-chooses it. Called on
        deprecation self-heal: the old holdout doc_ids belong to now-tombstoned pre-drift pool
        samples, so the post-drift campaign must pick a fresh holdout from the new arrivals."""
        self._holdout_ids.pop(fingerprint, None)

    def _choose_holdout(self, fingerprint: str, samples: list[PoolSample]) -> set[str]:
        n = len(samples)
        k = max(self.min_holdout, round(self.holdout_frac * n))
        k = min(k, n - 1) if n > 1 else 0
        ordered = sorted(samples, key=lambda s: s.doc_id)
        stable = int(hashlib.sha1(fingerprint.encode()).hexdigest()[:8], 16)
        rng = random.Random(self.seed ^ stable)
        rng.shuffle(ordered)
        return {s.doc_id for s in ordered[:k]}

    # ---- evaluation ------------------------------------------------------

    def evaluate(self, code: str, holdout: list[PoolSample]) -> AdmissionReport:
        """Run the candidate on the holdout and score field-level F1 against pool fields.
        Pass = F1 over threshold AND no crash on any holdout doc. At the min holdout (3 docs x
        8 fields = 24 comparisons) the 0.95 threshold already tolerates at most one wrong
        field-instance — strict enough to reject the memorization overfit (F1 ~0.75) yet robust
        to a single validated-but-wrong pool sample (the persistent holdout makes a stricter
        rule reject a format forever once one holdout field is pool-noisy)."""
        total = correct = mismatches = errors = 0
        per_doc = []
        for s in holdout:
            res = run_skill(code, s.text_layout, cpu_seconds=self.cpu_seconds, mem_mb=self.mem_mb)
            if not res.ok:
                errors += 1
                total += len(TARGET_FIELDS)
                mismatches += len(TARGET_FIELDS)
                per_doc.append({"doc_id": s.doc_id, "error": res.error})
                continue
            diffs = compare_fields(res.value or {}, s.fields)
            c = sum(1 for v in diffs.values() if v)
            correct += c
            total += len(diffs)
            mismatches += len(diffs) - c
            # record which fields missed so a pool-noise rejection is diagnosable from the event
            per_doc.append({"doc_id": s.doc_id, "correct": c, "of": len(diffs),
                            "wrong": [f for f, ok in diffs.items() if not ok]})

        f1 = correct / total if total else 0.0
        passed = f1 >= self.f1_threshold and errors == 0
        return AdmissionReport(
            passed=passed,
            holdout_f1=round(f1, 4),
            holdout_n=len(holdout),
            mismatches=mismatches,
            threshold=self.f1_threshold,
            errors=errors,
            per_doc=per_doc,
        )
