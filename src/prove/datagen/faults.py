"""Fault injectors with logged ground-truth labels — the raw material for the attribution eval.

Four faults, injected at two distinct seams so the *measured* pipeline stays the real pipeline
(no test machinery in the production path):

  - routing_noise   COMPONENT seam. `NoisyRouter` wraps the real `Router`: for a scheduled
                    fraction of docs that WOULD have routed correctly, it forces delivery to a
                    different format that currently has a serving skill, and records that format's
                    *genuine* Jaccard similarity as the confidence (never a fabricated number — the
                    low value must arise naturally, or the eval would be injecting the very signal
                    attribution reads). Reported as method="fuzzy" (a real misroute comes from a
                    fuzzy match at mediocre similarity, not an exact match).
  - template_drift  DATA seam. Already produced by `generator.generate_stream(drift=DriftSpec)`
                    (a fingerprint-stable date-style change); `drift_labels` turns its manifest
                    into per-doc fault labels for the confusion matrix.
  - rule_corruption COMPONENT seam (Stage 4b): a validator wrapper that flips/loosens one rule.
  - pool_poisoning  DATA seam (Stage 4c): mislabeled samples inserted into a pool pre-synthesis.

Each injector is the single writer of its own ground-truth label log, so the confusion matrix
compares injected cause vs attributed cause with no guesswork.
"""

from __future__ import annotations

import random
from typing import Any, Callable, Optional

from ..router import Router, _jaccard
from ..schemas import ExtractionResult, GroundTruth, ValidationVerdict
from ..validator import validate


class NoisyRouter(Router):
    """A `Router` that mis-delivers a scheduled fraction of otherwise-correct traffic.

    `serving_formats()` returns the format_ids that currently have a serving skill (active/trial);
    a misroute target must be one of them (else the noise lands on the LLM fallback and silently
    dilutes the effective rate). Injection only fires when the doc WOULD have matched exactly and a
    different served format exists — so warm-up is automatic (no served target ⇒ no injection).

    Ground truth is authoritative here: every injected misroute is logged in `.faults`.
    """

    def __init__(
        self,
        serving_formats: Callable[[], set[str]],
        *,
        noise_rate: float = 0.15,
        seed: int = 0,
    ):
        super().__init__()
        self._serving_formats = serving_formats
        self.noise_rate = noise_rate
        self._rng = random.Random(seed)
        self._ctx: tuple[Optional[str], Optional[str]] = (None, None)  # (doc_id, true_format)
        self.faults: list[dict[str, Any]] = []

    def begin(self, doc_id: str, true_format: Optional[str]) -> None:
        """Announce the doc about to be routed, so an injected misroute can be labelled."""
        self._ctx = (doc_id, true_format)

    def match(self, fp):  # type: ignore[override]
        true_id, conf, method = super().match(fp)
        if method != "exact":
            return true_id, conf, method  # a doc that wouldn't route can't be *mis*-routed
        if self._rng.random() >= self.noise_rate:
            return true_id, conf, method
        targets = [f for f in self.known_formats
                   if f != true_id and f in self._serving_formats()]
        if not targets:
            return true_id, conf, method
        forced = self._rng.choice(targets)
        genuine = _jaccard(fp, self._known[forced])  # honest low similarity vs the wrong format
        doc_id, _ = self._ctx
        self.faults.append({"doc_id": doc_id, "true_format": true_id,
                            "forced_format": forced, "confidence": round(genuine, 4)})
        return forced, genuine, "fuzzy"


def drift_labels(manifest: list[dict[str, Any]]) -> dict[str, str]:
    """Per-doc injected-fault label for a drift stream: 'data_drift' for drifted docs, 'none'
    otherwise. `generator.generate_stream` marks each entry with `drifted`."""
    return {e["doc_id"]: ("data_drift" if e.get("drifted") else "none") for e in manifest}


def corrupt_validator(
    bad_currency: str = "USD", rule: str = "currency_invalid",
) -> Callable[[ExtractionResult, Optional[GroundTruth]], ValidationVerdict]:
    """rule_corruption (false-fail direction): a validator that spuriously rejects a *valid*
    currency. Correct extractions of `bad_currency` docs now fail with `rule`, so a healthy skill's
    production failures pile up through no fault of its own — the failure the audit cross-check
    unmasks as the rule's account, not the skill's. Focused on the false-fail direction (the novel
    attribution story); false-pass contamination of new pool entries is the audit's separate story.

    Wraps the real validator (it stays the judge) and only *adds* the spurious verdict — so the
    corruption is a strict over-tightening of one rule, not a rewrite."""

    def _corrupted(result: ExtractionResult, gt: Optional[GroundTruth] = None) -> ValidationVerdict:
        verdict = validate(result, gt)
        if str(result.fields.get("currency", "")).upper() == bad_currency.upper():
            if rule not in verdict.rule_failures:
                verdict.rule_failures.append(rule)
            verdict.passed = False
        return verdict

    return _corrupted
