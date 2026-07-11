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
from typing import Any, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from .admission import Admission
from .extraction_agent import ExtractionAgent
from .llm_client import LLMClient
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

        # lifecycle bookkeeping
        self._synth_state: dict[str, dict] = {}   # fp -> {rejections}
        self._trial_passes: dict[str, int] = {}   # skill_id -> clean trial docs served
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
        if not self.skills_enabled or state["route_method"] != "exact":
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
        return {"verdict": validate(state["extraction"], state.get("ground_truth"))}

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

        if ext.source == "skill" and ext.skill_id:
            self._record_skill_outcome(ext.skill_id, verdict.passed)

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
            cost_usd=ext.cost_usd, tokens_in=ext.tokens_in, tokens_out=ext.tokens_out,
        )
        self.traces.write(trace)
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

    def _record_skill_outcome(self, skill_id: str, passed: bool) -> None:
        # A2+ charges every production outcome to the ledger (raw — attribution is Phase 4).
        self.registry.record_outcome(skill_id, passed, attributed=True)
        skill = self.registry.get_skill(skill_id)
        if skill.state == "trial" and passed:
            self._trial_passes[skill_id] = self._trial_passes.get(skill_id, 0) + 1
            if self._trial_passes[skill_id] >= self.trial_docs:
                self.registry.activate(skill_id)

    # ---- lifecycle (synthesis trigger) ----------------------------------

    def _lifecycle(self, fingerprint: str) -> None:
        if not self.skills_enabled:
            return
        if self.registry.serving_skill(fingerprint) is not None:
            return
        state = self._synth_state.setdefault(fingerprint, {"rejections": 0})
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
                self.registry.log_event(
                    skill.skill_id, "flagged_meta_review",
                    {"rejections": state["rejections"]},  # Phase 4 handles; for now log + stop
                )

    # ---- public API ------------------------------------------------------

    def process(self, document: Document, ground_truth: Optional[GroundTruth] = None) -> Trace:
        final = self._app.invoke({"document": document, "ground_truth": ground_truth})
        self._lifecycle(final["fingerprint_hash"])
        return final["trace"]
