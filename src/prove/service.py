"""FastAPI shell: POST /extract (+ evidence card), GET /skills, GET /traces.

Auditability is a first-class output, not a bolt-on log: every /extract response carries an
*evidence card* — routing evidence (fingerprint/format/confidence/method), executor identity
(skill id/version/confidence, or the LLM fallback), the per-rule validation verdict, and cost —
all lifted from the structured Trace that is written for the document anyway. /skills exposes the
registry state machine + confidence ledger; /traces the recent per-document trace stream.

The observability SSE dashboard is intentionally out of scope (the ablation plots carry the
narrative); this is the minimal auditable API surface.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import FastAPI
from pydantic import BaseModel

from .config import load_config
from .llm_client import FakeClient, LLMClient
from .pipeline import Pipeline
from .schemas import Document, Trace


class ExtractRequest(BaseModel):
    doc_id: str
    text_layout: Optional[dict] = None   # the skill ABI (pre-extracted pdfplumber view)
    pdf_path: Optional[str] = None       # server-side layout extraction if text_layout absent


def _evidence_card(pipe: Pipeline, trace: Trace) -> dict[str, Any]:
    """Everything a reviewer needs to trust (or contest) one extraction, all from the Trace."""
    skill_confidence = None
    if trace.skill_id:
        try:
            skill_confidence = round(pipe.registry.get_skill(trace.skill_id).confidence, 4)
        except KeyError:
            pass
    return {
        "routing": {"format_id": trace.route_format_id,
                    "confidence": round(trace.route_confidence, 4),
                    "method": trace.route_method, "fingerprint": trace.route_fingerprint},
        "executor": {"source": trace.extraction_source, "skill_id": trace.skill_id,
                     "skill_version": trace.skill_version, "skill_confidence": skill_confidence},
        "validation": {"passed": trace.validation.passed,
                       "rule_failures": trace.validation.rule_failures},
        "cost": {"cost_usd": trace.cost_usd,
                 "tokens_in": trace.tokens_in, "tokens_out": trace.tokens_out},
    }


def create_app(pipeline: Pipeline) -> FastAPI:
    app = FastAPI(title="PROVE", summary="Outcome-verified skill lifecycle governance")

    @app.post("/extract")
    def extract(req: ExtractRequest) -> dict[str, Any]:
        layout = req.text_layout
        if layout is None and req.pdf_path:
            from .layout import extract_layout
            layout = extract_layout(req.pdf_path)
        doc = Document(doc_id=req.doc_id, text_layout=layout or {})
        result, trace = pipeline.process_verbose(doc)
        return {"doc_id": req.doc_id, "fields": result.fields,
                "evidence_card": _evidence_card(pipeline, trace)}

    @app.get("/skills")
    def skills() -> dict[str, Any]:
        return {"skills": [
            {"skill_id": s.skill_id, "format_id": s.format_id, "version": s.version,
             "state": s.state, "confidence": round(s.confidence, 4),
             "alpha": round(s.alpha, 4), "beta": round(s.beta, 4),
             "deprecated_reason": s.deprecated_reason}
            for s in pipeline.registry.all_skills()]}

    @app.get("/traces")
    def traces(n: int = 50) -> dict[str, Any]:
        return {"traces": [_trace_row(t) for t in pipeline.traces.recent(n)]}

    return app


def _trace_row(t: Trace) -> dict[str, Any]:
    return {"doc_id": t.doc_id, "route_format_id": t.route_format_id,
            "route_confidence": round(t.route_confidence, 4), "route_method": t.route_method,
            "source": t.extraction_source, "skill_id": t.skill_id,
            "passed": t.validation.passed, "rule_failures": t.validation.rule_failures,
            "cost_usd": t.cost_usd, "tokens": t.tokens_in + t.tokens_out}


def create_pipeline(config: Optional[dict] = None, client: Optional[LLMClient] = None) -> Pipeline:
    """Build a pipeline for the service. Without a client, a key-free FakeClient returning empty
    extractions keeps the API runnable for shape/auditability demos; a deployment passes a real
    client (and typically a preloaded registry) in."""
    config = config or load_config()
    client = client or FakeClient(lambda system, user, model: "{}")
    return Pipeline(config, client)


app = create_app(create_pipeline())
