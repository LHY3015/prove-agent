"""Root-cause attribution: charge a skill's failure batch to the correct account.

This is the *accountant*, not the judge (Hard Design Rule 1). The verdict of quality already
exists — the validator produced it. Attribution only reads objective trace signals and decides
*whose account* a batch of failures belongs to, so the confidence ledger charges the right party:

  routing_error  failures land on low-confidence (mis-routed) docs while the skill's own
                 high-confidence traffic passes → the router misdelivered; the skill is innocent.
  rule_defect    failures are entirely explained by a frozen/suspect validator rule → the rule
                 is wrong, not the extraction.
  data_drift     the skill passed cleanly for a while, then failures begin abruptly at high
                 confidence (same format, header-stable fingerprint) → the world changed.
  input_noise    the document's own extracted text was measurably degraded at ingestion → the
                 input failed, not any component. Unique among the accounts in having no party to
                 charge and nothing to repair: the remedy is to quarantine the document. Without
                 this account, a degraded doc routes exactly (its header may be intact), fails at
                 high confidence, survives every other peel, and is charged to a healthy skill as
                 skill_defect — the precise miscarriage this module exists to prevent.
  skill_defect   high-confidence failures spread across the batch since the skill's start → the
                 skill itself is broken.
  ambiguous      no deterministic rule fits → honest fallback (logged, no ledger charge, no
                 remedy). An optional single constrained LLM call may resolve it (Phase 4d); the
                 LLM is forbidden from judging extraction quality — it only classifies.

Design: *per-doc exoneration* ("peel"), never batch-fiat. Failures a deterministic per-doc test
explains (input_integrity < tau; route_confidence < tau; rule_failures ⊆ frozen rules) are removed from the batch and
charged to their own account; only the *residual* high-confidence, non-rule failures are eligible
to be charged to the skill. This makes mixed-cause batches (routing noise concurrent with a real
defect) resolve correctly and lets the same code back both the routing and the rule remedies.

The classifier is a pure function of the batch — it holds no state and issues no side effects; the
pipeline owns the remedies (charge/deprecate/quarantine/freeze).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .schemas import AttributionVerdict, Trace

# route_confidence at/above this is a clean delivery (base router exact-match ~1.0); below it the
# doc was mis-delivered (a forced/fuzzy misroute records its genuine low Jaccard). The gap between
# exact hits (~1.0) and cross-format Jaccard (~0.4) is wide, so tau is not knife-edge — but it is
# calibrated empirically in the eval before the headline run.
DEFAULT_CONF_TAU = 0.9
# fraction of the ordered batch that must pass cleanly *before* the first residual failure for the
# onset to read as abrupt (data_drift) rather than present-from-the-start (skill_defect).
DEFAULT_DRIFT_PREFIX = 0.4
# input_integrity at/below this marks the document itself as unreadable. Born-digital extraction
# scores 1.0, so like conf_tau this sits on a wide margin rather than a knife edge.
DEFAULT_INTEGRITY_TAU = 0.95

RootCause = str  # skill_defect | routing_error | rule_defect | data_drift | input_noise | ambiguous


@dataclass
class AttributionResult:
    root_cause: RootCause
    charged_doc_ids: list[str]      # failures charged to the skill's beta (skill_defect/data_drift)
    exonerated_doc_ids: list[str]   # failures NOT charged to the skill (routing/rule/ambiguous)
    evidence: dict = field(default_factory=dict)

    def to_verdict(self, batch_id: str, action: str = "") -> AttributionVerdict:
        return AttributionVerdict(
            failure_batch_id=batch_id,
            root_cause=self.root_cause,
            evidence={**self.evidence,
                      "charged_doc_ids": self.charged_doc_ids,
                      "exonerated_doc_ids": self.exonerated_doc_ids},
            action_taken=action,
        )


class Attributor:
    def __init__(
        self,
        *,
        conf_tau: float = DEFAULT_CONF_TAU,
        drift_prefix_frac: float = DEFAULT_DRIFT_PREFIX,
        integrity_tau: float = DEFAULT_INTEGRITY_TAU,
    ):
        self.conf_tau = conf_tau
        self.drift_prefix_frac = drift_prefix_frac
        self.integrity_tau = integrity_tau

    def classify(
        self,
        traces: list[Trace],
        *,
        frozen_rules: Optional[set[str]] = None,
    ) -> AttributionResult:
        """Classify a skill's recent batch (its monitor window, time-ordered). Only skill-served
        traces are considered — LLM-fallback docs are not the skill's account."""
        frozen_rules = frozen_rules or set()
        batch = [t for t in traces if t.extraction_source == "skill"]
        passes = [t for t in batch if t.validation.passed]
        fails = [t for t in batch if not t.validation.passed]
        if not fails:
            return AttributionResult("ambiguous", [], [],
                                     {"reason": "no_failures_in_batch"})

        has_clean_high_conf = any(t.route_confidence >= self.conf_tau for t in passes)

        # --- peel 0: input_noise (per-doc, deterministic) -------------------
        # a failure on a document whose extracted text was measurably degraded at ingestion is
        # nobody's fault — not the skill's, not the router's. Runs FIRST because degraded header
        # tokens also depress route_confidence, so a doc eligible for both peels must land in the
        # account naming the root cause (the unreadable input) rather than its downstream symptom
        # (the weak route). Only failures are peeled: a low-integrity doc that still validates
        # harmed nobody and needs no action.
        noise_exon = [t for t in fails if t.input_integrity < self.integrity_tau]
        residual = [t for t in fails if t not in noise_exon]

        # --- peel 1: routing_error (per-doc, deterministic) -----------------
        # a failure below tau is a mis-delivery; exonerate it ONLY when the skill demonstrably
        # works on its own high-confidence traffic (else "low confidence everywhere" is the
        # skill's own format being unfamiliar, not a routing fault).
        routing_exon = [t for t in residual
                        if t.route_confidence < self.conf_tau and has_clean_high_conf]
        residual = [t for t in residual if t not in routing_exon]

        # --- peel 2: rule_defect (per-doc, deterministic) -------------------
        # a failure whose every rule_failure is a frozen (known-bad) rule is the rule's account,
        # not the skill's. Empty rule_failures can't be rule-explained.
        rule_exon = [t for t in residual
                     if t.validation.rule_failures
                     and set(t.validation.rule_failures) <= frozen_rules]
        residual = [t for t in residual if t not in rule_exon]

        # --- classify the residual (high-confidence, non-rule failures) -----
        if not residual:
            # the whole batch was explained by exoneration; the dominant account wins.
            all_exon = [t.doc_id for t in fails]
            counts = {"input_noise": len(noise_exon), "routing_error": len(routing_exon),
                      "rule_defect": len(rule_exon)}
            # ties break toward the earlier peel (input_noise > routing_error > rule_defect),
            # matching the peel order's precedence.
            dominant = max(counts, key=lambda k: counts[k])
            if dominant == "input_noise":
                return AttributionResult(
                    "input_noise", [], all_exon,
                    {"degraded_failures": len(noise_exon), "integrity_tau": self.integrity_tau,
                     "mean_integrity": round(
                         sum(t.input_integrity for t in noise_exon) / len(noise_exon), 3)})
            if dominant == "routing_error":
                return AttributionResult(
                    "routing_error", [], all_exon,
                    {"low_conf_failures": len(routing_exon), "tau": self.conf_tau,
                     "clean_high_conf_passes": len(passes)})
            return AttributionResult(
                "rule_defect", [], all_exon,
                {"rule_explained_failures": len(rule_exon),
                 "frozen_rules": sorted(frozen_rules)})

        exon_ids = [t.doc_id for t in noise_exon + routing_exon + rule_exon]
        charged_ids = [t.doc_id for t in residual]
        cause = self._drift_vs_defect(batch, residual)
        evidence = {
            "residual_failures": len(residual),
            "exonerated": len(exon_ids),
            "mean_fail_conf": round(sum(t.route_confidence for t in residual) / len(residual), 3),
        }
        return AttributionResult(cause, charged_ids, exon_ids, evidence)

    def _drift_vs_defect(self, batch: list[Trace], residual: list[Trace]) -> RootCause:
        """Onset timing over the time-ordered batch: a clean prefix followed by failures reads as
        an abrupt onset → data_drift; failures reaching back to the batch start → skill_defect."""
        residual_ids = {id(t) for t in residual}
        first_fail_idx = next(
            (i for i, t in enumerate(batch) if id(t) in residual_ids), len(batch)
        )
        clean_prefix = sum(1 for t in batch[:first_fail_idx] if t.validation.passed)
        if clean_prefix >= self.drift_prefix_frac * len(batch):
            return "data_drift"
        return "skill_defect"
