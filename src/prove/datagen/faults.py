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
  - input_noise     INGESTION seam. `LayoutGarbler` degrades the extracted `text_layout` of a
                    scheduled fraction of documents (junk glyphs over a page band), modelling an
                    unreadable scan region. Unlike the four above, the fault is a property of the
                    document rather than of a pipeline component — no component is at fault, so
                    attribution exonerates rather than repairs.

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


class LayoutGarbler:
    """input_noise — INGESTION seam. Simulates an unreadable page region: for a scheduled fraction
    of documents, the tokens inside a horizontal band of the page have characters replaced by junk
    glyphs, exactly as an OCR engine emits on a smudge or occlusion.

    Applied to the `text_layout` AFTER extraction and BEFORE the pipeline sees the document, so the
    measured pipeline stays the real pipeline. The band defaults to the BODY region (below the
    header), which is the interesting case: the header fingerprint survives intact, so the doc
    still routes exactly at ~1.0 confidence and the resulting failure is a *high-confidence* one
    that no pre-existing peel can explain — it would be charged to a healthy skill as skill_defect
    without the input_noise account.

    Only character classes are damaged, never positions: this models illegibility, not re-layout.
    """

    _JUNK = "▯§¤|~^"

    def __init__(
        self,
        *,
        noise_rate: float = 0.3,
        token_frac: float = 0.5,
        band: tuple[float, float] = (0.35, 1.0),   # fractions of the CONTENT extent
        seed: int = 0,
    ):
        self.noise_rate = noise_rate
        self.token_frac = token_frac
        self.band = band
        self._rng = random.Random(seed)
        self.faults: list[dict[str, Any]] = []

    def apply(self, doc_id: str, text_layout: dict[str, Any]) -> dict[str, Any]:
        """Return the (possibly) degraded text_layout, logging ground truth when it fires."""
        if self._rng.random() >= self.noise_rate:
            return text_layout
        words = [dict(w) for w in text_layout.get("words", [])]
        if not words:
            return text_layout
        # the band spans the document's CONTENT, not the page: an A4 invoice leaves most of the
        # sheet blank, so a page-relative band would land on whitespace and damage nothing.
        tops = [float(w["top"]) for w in words]
        top, extent = min(tops), (max(tops) - min(tops)) or 1.0
        lo, hi = top + self.band[0] * extent, top + self.band[1] * extent
        hit = 0
        for w in words:
            if not (lo <= float(w["top"]) <= hi):
                continue
            if self._rng.random() >= self.token_frac:
                continue
            w["text"] = "".join(
                self._rng.choice(self._JUNK) if self._rng.random() < 0.5 else ch
                for ch in str(w["text"])
            )
            hit += 1
        if not hit:
            return text_layout
        degraded = dict(text_layout)
        degraded["words"] = words
        # lines/full_text are derived views — rebuild them so every consumer sees the same damage
        from ..layout import _group_lines

        degraded["lines"] = _group_lines(words)
        degraded["full_text"] = "\n".join(ln["text"] for ln in degraded["lines"])
        self.faults.append({"doc_id": doc_id, "garbled_tokens": hit,
                            "band": list(self.band)})
        return degraded

    def labels(self, doc_ids: list[str]) -> dict[str, str]:
        """Per-doc injected-fault label for the confusion matrix."""
        fired = {f["doc_id"] for f in self.faults}
        return {d: ("input_noise" if d in fired else "none") for d in doc_ids}


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

    def _corrupted(
        result: ExtractionResult, gt: Optional[GroundTruth] = None, **kwargs
    ) -> ValidationVerdict:
        verdict = validate(result, gt, **kwargs)
        if str(result.fields.get("currency", "")).upper() == bad_currency.upper():
            if rule not in verdict.rule_failures:
                verdict.rule_failures.append(rule)
            verdict.passed = False
        return verdict

    return _corrupted
