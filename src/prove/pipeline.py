"""LangGraph wiring of the per-document dataflow + the skill-lifecycle orchestration.

Per-doc graph:  route -> (skill | llm) extract -> validate -> finalize (pool + outcome + trace).
Lifecycle (imperative, run after the graph in `process()`, NOT a graph node — it has batch /
retry semantics that don't belong in per-doc state): when a fingerprint's verified pool reaches
`synthesis_trigger`, synthesize a skill from the training split, gate it through admission (held
out split), and on pass register it so the format's future traffic routes to the skill and runs
in the sandbox instead of the LLM.

Ablation modes (from `ablation.mode`):
  A0  skills disabled — every doc goes to the LLM (the Phase-1 baseline).
  A1  synthesis on, admission gate OFF — candidate goes straight to active (silent-failure demo).
  A2/A3  synthesis on, admission gate ON.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Callable, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from .admission import Admission
from .attribution import Attributor
from .audit import PoolAuditor
from .extraction_agent import ExtractionAgent
from .layout import input_integrity
from .llm_client import LLMClient
from .monitor import Monitor
from .registry import Registry
from .router import Router, compute_fingerprint, fingerprint_hash
from .sample_pool import PoolSample, SamplePool
from .sandbox import run_skill
from .schemas import Document, ExtractionResult, GroundTruth, Trace, ValidationVerdict
from .synthesis_agent import SynthesisAgent
from .traces import TraceStore
from .validator import validate


class PipelineState(TypedDict, total=False):
    document: Document
    ground_truth: Optional[GroundTruth]
    fingerprint_hash: str
    route_format_id: Optional[str]
    route_confidence: float
    route_method: str
    skill_id: Optional[str]
    skill_version: Optional[int]
    extraction: ExtractionResult
    verdict: ValidationVerdict
    trace: Trace


class Pipeline:
    def __init__(
        self,
        config: dict[str, Any],
        client: LLMClient,
        trace_store: Optional[TraceStore] = None,
        router: Optional[Router] = None,
        sample_pool: Optional[SamplePool] = None,
        registry: Optional[Registry] = None,
        validator: Optional[Callable] = None,
    ):
        self.config = config
        self.mode = config.get("ablation", {}).get("mode", "A0")
        self.skills_enabled = self.mode != "A0"
        self.gate_enabled = self.mode not in ("A0", "A1")

        self.router = router or Router()
        self.pool = sample_pool or SamplePool()
        self.traces = trace_store or TraceStore(":memory:")
        self.registry = registry or Registry(":memory:")
        self.extraction_agent = ExtractionAgent(client, config["model"]["extraction"])
        # injectable validator seam (rule_corruption fault, Phase 4b); defaults to the real engine.
        self._validate = validator or validate

        sb = config.get("sandbox", {})
        self.cpu_seconds = sb.get("cpu_seconds", 5)
        self.mem_mb = sb.get("mem_mb", 512)
        self.trigger = config.get("synthesis_trigger", 10)
        adm = config.get("admission", {})
        self.max_rejections = adm.get("max_rejections", 3)
        self.trial_docs = config.get("trial_docs", 10)

        self.synthesis_agent = SynthesisAgent(
            client, config["model"]["synthesis"],
            max_attempts=adm.get("max_synthesis_attempts", 3),
            cpu_seconds=self.cpu_seconds, mem_mb=self.mem_mb,
        )
        self.admission = Admission(
            holdout_frac=adm.get("holdout_frac", 0.3),
            min_holdout=adm.get("min_holdout", 3),
            f1_threshold=adm.get("f1_threshold", 0.95),
            cpu_seconds=self.cpu_seconds, mem_mb=self.mem_mb,
        )
        # monitoring + self-healing (Phase 3). A0/A1 keep skills but never deprecate them:
        # A0 has no skills; A1 exists to SHOW un-gated skills failing silently, so healing it
        # would erase the demonstration. Deprecation runs from A2 on.
        self.monitor_enabled = self.mode not in ("A0", "A1")
        mon = config.get("monitor", {})
        self.window = mon.get("window", 20)
        self.monitor = Monitor(
            window=self.window,
            failure_rate_threshold=mon.get("failure_rate_threshold", 0.2),
            confidence_floor=mon.get("confidence_floor", 0.3),
            min_window=mon.get("min_window", 10),
            min_failures=mon.get("min_failures", 3),
        )

        # attribution (Phase 4). A3 only: a fired monitor batch is classified to a root cause and
        # only skill-fault failures charge the ledger; A2 charges every failure raw (no attribution).
        self.attribution_enabled = self.mode == "A3"
        att = config.get("attribution", {})
        self.attributor = Attributor(
            conf_tau=att.get("conf_tau", 0.9),
            drift_prefix_frac=att.get("drift_prefix_frac", 0.4),
            integrity_tau=att.get("integrity_tau", 0.95),
        )
        self.ambiguous_meta_review = att.get("ambiguous_meta_review", 2)
        # the auditor must judge with the SAME (possibly corrupted) validator the pipeline uses, so
        # a corrupted rule firing on the immutable pool is what it detects.
        self.auditor = PoolAuditor(validator=self._validate,
                                   anomaly_frac=att.get("audit_anomaly_frac", 0.2))

        # lifecycle bookkeeping
        self._synth_state: dict[str, dict] = {}   # fp -> {rejections, campaign}
        self._trial_passes: dict[str, int] = {}   # skill_id -> clean trial docs served
        self._trace_windows: dict[str, deque] = {}  # skill_id -> recent Traces (attribution batch)
        self._ambiguous_counts: dict[str, int] = {}  # skill_id -> consecutive ambiguous verdicts
        self._frozen_rules: set[str] = set()      # rule_defect remedy: rules excused from the ledger
        self.attributions: list[dict] = []        # verdict log (injected-vs-attributed eval reads this)
        self.lifecycle_cost_usd = 0.0
        self._app = self._build_graph()

    # ---- graph nodes -----------------------------------------------------

    def _route_node(self, state: PipelineState) -> dict:
        fp = compute_fingerprint(state["document"].text_layout)
        fmt_id, conf, method = self.router.match(fp)
        return {
            "fingerprint_hash": fingerprint_hash(fp),
            "route_format_id": fmt_id,
            "route_confidence": conf,
            "route_method": method,
        }

    def _decide(self, state: PipelineState) -> str:
        # A serving skill executes on an exact match, or on a fuzzy match (the fuzzy-routing path;
        # the base Router never emits fuzzy, so this only activates under injected routing_noise —
        # a misroute delivered to another format's skill at genuine low confidence).
        if not self.skills_enabled or state["route_method"] not in ("exact", "fuzzy"):
            return "llm"
        skill = self.registry.serving_skill(state["route_format_id"])
        return "skill" if skill is not None else "llm"

    def _llm_extract_node(self, state: PipelineState) -> dict:
        return {"extraction": self.extraction_agent.extract(state["document"])}

    def _skill_execute_node(self, state: PipelineState) -> dict:
        doc = state["document"]
        skill = self.registry.serving_skill(state["route_format_id"])
        res = run_skill(
            self.registry.get_code(skill.skill_id), doc.text_layout,
            cpu_seconds=self.cpu_seconds, mem_mb=self.mem_mb,
        )
        fields = res.value if res.ok and res.value else {}
        ext = ExtractionResult(
            doc_id=doc.doc_id, fields=fields, source="skill", skill_id=skill.skill_id,
        )
        return {"extraction": ext, "skill_id": skill.skill_id, "skill_version": skill.version}

    def _validate_node(self, state: PipelineState) -> dict:
        return {"verdict": self._validate(state["extraction"], state.get("ground_truth"))}

    def _finalize_node(self, state: PipelineState) -> dict:
        ext = state["extraction"]
        verdict = state["verdict"]
        fp_hash = state["fingerprint_hash"]

        # only LLM-verified samples feed the pool (the pre-synthesis accumulator); a skill's
        # own outputs never re-enter it (avoids self-poisoning; re-accumulation on deprecation
        # happens via the LLM fallback in Phase 3).
        if verdict.passed and ext.source == "llm":
            self.pool.add(PoolSample(
                doc_id=ext.doc_id, fingerprint=fp_hash,
                text_layout=state["document"].text_layout, fields=ext.fields,
            ))

        trace = Trace(
            doc_id=ext.doc_id, ts=time.time(),
            route_format_id=state["route_format_id"],
            route_confidence=state["route_confidence"],
            route_method=state["route_method"],
            route_fingerprint=fp_hash,
            skill_id=ext.skill_id, skill_version=state.get("skill_version"),
            extraction_source=ext.source,
            field_results=verdict.field_diffs or {},
            validation=verdict,
            input_integrity=input_integrity(state["document"].text_layout),
            cost_usd=ext.cost_usd, tokens_in=ext.tokens_in, tokens_out=ext.tokens_out,
        )
        self.traces.write(trace)

        # the trace is the attribution batch's unit, so the skill-outcome bookkeeping runs on it
        # (after it exists) rather than on a bare (skill_id, passed) pair.
        if ext.source == "skill" and ext.skill_id:
            self._record_skill_outcome(trace)
        return {"trace": trace}

    def _build_graph(self):
        g = StateGraph(PipelineState)
        g.add_node("route", self._route_node)
        g.add_node("llm_extract", self._llm_extract_node)
        g.add_node("skill_execute", self._skill_execute_node)
        g.add_node("validate", self._validate_node)
        g.add_node("finalize", self._finalize_node)

        g.add_edge(START, "route")
        g.add_conditional_edges(
            "route", self._decide, {"llm": "llm_extract", "skill": "skill_execute"}
        )
        g.add_edge("llm_extract", "validate")
        g.add_edge("skill_execute", "validate")
        g.add_edge("validate", "finalize")
        g.add_edge("finalize", END)
        return g.compile()

    # ---- skill outcome + trial promotion --------------------------------

    def _record_skill_outcome(self, trace: Trace) -> None:
        skill_id = trace.skill_id
        passed = trace.validation.passed
        self._trace_windows.setdefault(skill_id, deque(maxlen=self.window)).append(trace)

        # Ledger charging. A2 charges every outcome raw (no attribution). A3 defers: a pass is an
        # unambiguous positive (charge alpha now); a failure's blame is unknown until the batch is
        # attributed, so it is only logged (attributed=False leaves beta untouched) and charged
        # later iff attribution finds the skill at fault.
        if not self.attribution_enabled or passed:
            skill = self.registry.record_outcome(skill_id, passed, attributed=True)
        else:
            skill = self.registry.record_outcome(skill_id, False, attributed=False)
        self.monitor.record(skill_id, passed)

        # self-healing: the monitor may fire on a failing trial OR active skill. Check this BEFORE
        # trial promotion so a skill can't promote on the same doc that trips it.
        if self.monitor_enabled:
            confidence = skill.alpha / (skill.alpha + skill.beta)
            reason = self.monitor.should_deprecate(skill_id, confidence)
            if reason is not None:
                if self.attribution_enabled:
                    self._attribute_and_remedy(skill.skill_id, skill.format_id, reason)
                else:
                    self._deprecate_and_reset(skill.skill_id, skill.format_id, reason)
                return

        if skill.state == "trial" and passed:
            self._trial_passes[skill_id] = self._trial_passes.get(skill_id, 0) + 1
            if self._trial_passes[skill_id] >= self.trial_docs:
                self.registry.activate(skill_id)

    def _attribute_and_remedy(self, skill_id: str, fingerprint: str, reason: str) -> None:
        """A3: a monitor batch fired. Classify its root cause and apply the targeted remedy — only
        skill-fault failures (skill_defect / data_drift) charge the ledger and deprecate; a routing
        or rule fault leaves the skill's confidence untouched (the failure belongs to another
        account); an ambiguous batch is logged with no charge and escalated to meta-review on
        repetition. After any non-deprecating verdict the skill's window is reset so the same batch
        doesn't re-trip every subsequent doc (a persisting fault re-accumulates and re-fires)."""
        batch = list(self._trace_windows.get(skill_id, []))
        # audit cross-check (rule_defect detection): re-validate this format's immutable pool; any
        # rule now firing on it is corrupted, so freeze it before classifying — the peel then
        # exonerates skill failures that carry only corrupted rules.
        audit = self.auditor.audit(self.pool, fingerprint)
        if audit.corrupted_rules:
            self._frozen_rules |= set(audit.corrupted_rules)
        result = self.attributor.classify(batch, frozen_rules=self._frozen_rules)
        batch_id = f"{skill_id}:{reason}"
        verdict = result.to_verdict(batch_id)

        if result.root_cause in ("skill_defect", "data_drift"):
            for _ in result.charged_doc_ids:  # attribution-corrected ledger write (the skill's fault)
                self.registry.record_outcome(skill_id, False, attributed=True)
            verdict.action_taken = "deprecate_resynthesize"
            self._log_attribution(skill_id, verdict, reason)
            self._deprecate_and_reset(skill_id, fingerprint, f"{result.root_cause}:{reason}")
            return

        if result.root_cause == "input_noise":
            # no component is at fault, so no ledger charge and no repair — the documents
            # themselves are quarantined for re-ingestion and the skill keeps serving.
            verdict.action_taken = "quarantine_documents"
            self.registry.log_event(skill_id, "input_noise_quarantine",
                                    {"exonerated": len(result.exonerated_doc_ids),
                                     "reason": reason})
            self._ambiguous_counts.pop(skill_id, None)
        elif result.root_cause == "routing_error":
            verdict.action_taken = "quarantine_route"
            self.registry.log_event(skill_id, "routing_quarantine",
                                    {"exonerated": len(result.exonerated_doc_ids), "reason": reason})
            self._ambiguous_counts.pop(skill_id, None)
        elif result.root_cause == "rule_defect":
            # the audit (run above) is the SOLE freezing authority — it only flags rules that fire
            # on the immutable, previously-passing pool, so it can never freeze a legitimately-fired
            # rule. Deriving the freeze from the batch instead would also freeze the LEGITIMATE
            # rules that routing-exonerated (misrouted) docs fired (date / required-field checks),
            # and _frozen_rules is global + monotone → permanent silent masking of future genuine
            # skill failures. So we report what the audit froze and add nothing here.
            verdict.action_taken = f"freeze_rules:{sorted(audit.corrupted_rules)}"
            self._ambiguous_counts.pop(skill_id, None)
        else:  # ambiguous — honest fallback: no charge, no remedy, escalate on repetition
            self._ambiguous_counts[skill_id] = self._ambiguous_counts.get(skill_id, 0) + 1
            verdict.action_taken = "logged_no_remedy"
            if self._ambiguous_counts[skill_id] >= self.ambiguous_meta_review:
                self.registry.log_event(skill_id, "meta_review_flag",
                                        {"consecutive_ambiguous": self._ambiguous_counts[skill_id]})

        self._log_attribution(skill_id, verdict, reason)
        self.monitor.drop(skill_id)           # reset detection window (paired with the remedy above)
        self._trace_windows.pop(skill_id, None)

    def _log_attribution(self, skill_id: str, verdict, reason: str) -> None:
        self.attributions.append({"skill_id": skill_id, "reason": reason,
                                  **verdict.model_dump()})
        self.registry.log_event(skill_id, "attribution", verdict.model_dump())

    def _deprecate_and_reset(self, skill_id: str, fingerprint: str, reason: str) -> None:
        """Deprecate a skill and open a fresh synthesis campaign for its format: tombstone the
        stale pool, drop the frozen holdout, and reset the rejection counter under a new campaign
        id. Traffic falls back to the LLM (serving_skill is now None), the pool re-accumulates
        from post-deprecation LLM-verified docs, and `_lifecycle` re-fires once it re-reaches the
        trigger with those FRESH samples."""
        self.registry.deprecate(skill_id, reason)
        self.monitor.drop(skill_id)
        self._trial_passes.pop(skill_id, None)
        self._trace_windows.pop(skill_id, None)
        self._ambiguous_counts.pop(skill_id, None)
        self.pool.invalidate(fingerprint)
        self.admission.reset_holdout(fingerprint)
        prev = self._synth_state.get(fingerprint, {"campaign": 0})
        campaign = prev.get("campaign", 0) + 1
        self._synth_state[fingerprint] = {"rejections": 0, "campaign": campaign}
        self.registry.log_event(skill_id, "deprecated_reset",
                                {"reason": reason, "campaign": campaign})

    # ---- lifecycle (synthesis trigger) ----------------------------------

    def _lifecycle(self, fingerprint: str) -> None:
        if not self.skills_enabled:
            return
        if self.registry.serving_skill(fingerprint) is not None:
            return
        state = self._synth_state.setdefault(fingerprint, {"rejections": 0, "campaign": 0})
        if state["rejections"] >= self.max_rejections:
            return
        if self.pool.count(fingerprint) < self.trigger:
            return

        samples = self.pool.samples_for(fingerprint)
        train, holdout = self.admission.split(fingerprint, samples)
        synth = self.synthesis_agent.synthesize(fingerprint, train)
        schema_version = samples[0].text_layout.get("schema_version", 1)
        skill = self.registry.create_candidate(fingerprint, synth.code, schema_version)
        self.lifecycle_cost_usd += synth.cost_usd
        self.registry.log_event(
            skill.skill_id, "synthesized",
            {"attempts": synth.attempts, "passed_training": synth.passed_training,
             "campaign": state["campaign"],
             "tokens_in": synth.tokens_in, "tokens_out": synth.tokens_out},
            cost_usd=synth.cost_usd,
        )

        if not self.gate_enabled:  # A1: no held-out gate
            self.registry.admit_direct_active(skill.skill_id)
            self.router.register(fingerprint, samples[0].text_layout)
            return

        report = self.admission.evaluate(synth.code, holdout)
        if report.passed:
            self.registry.admit_to_trial(skill.skill_id, report.to_dict())
            self.router.register(fingerprint, samples[0].text_layout)
        else:
            self.registry.reject(skill.skill_id, report.to_dict())
            state["rejections"] += 1
            if state["rejections"] >= self.max_rejections:
                # campaign exhausted: this format stays on the LLM fallback until a future
                # deprecation opens a new campaign (or Phase-4 meta-review intervenes). Safe but
                # expensive — logged loudly, never silently stranded.
                self.registry.log_event(
                    skill.skill_id, "campaign_exhausted",
                    {"rejections": state["rejections"], "campaign": state["campaign"]},
                )

    # ---- public API ------------------------------------------------------

    def process_verbose(
        self, document: Document, ground_truth: Optional[GroundTruth] = None
    ) -> tuple[ExtractionResult, Trace]:
        """Run one document and return (extraction, trace). The service needs the extracted fields
        (the trace carries only routing/validation/cost); everything else uses `process`."""
        final = self._app.invoke({"document": document, "ground_truth": ground_truth})
        self._lifecycle(final["fingerprint_hash"])
        return final["extraction"], final["trace"]

    def process(self, document: Document, ground_truth: Optional[GroundTruth] = None) -> Trace:
        return self.process_verbose(document, ground_truth)[1]
