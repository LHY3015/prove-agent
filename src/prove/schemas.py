"""Pydantic models shared across the pipeline. Define these first; everything depends on them.

One structured `Trace` row is written per document processed — it is the attribution
module's only data source (Hard Design Rule 4).
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Target extraction schema (uniform across every template family).
# All field values travel as strings; the validator parses/types them.
# ---------------------------------------------------------------------------

FieldDtype = Literal["str", "date", "money", "int", "identifier"]


class FieldSpec(BaseModel):
    name: str
    dtype: FieldDtype


FIELD_SPECS: list[FieldSpec] = [
    FieldSpec(name="vendor_name", dtype="str"),
    FieldSpec(name="invoice_number", dtype="identifier"),
    FieldSpec(name="invoice_date", dtype="date"),
    FieldSpec(name="currency", dtype="str"),
    FieldSpec(name="subtotal", dtype="money"),
    FieldSpec(name="tax", dtype="money"),
    FieldSpec(name="total", dtype="money"),
    FieldSpec(name="line_item_count", dtype="int"),
]

TARGET_FIELDS: list[str] = [f.name for f in FIELD_SPECS]

CURRENCY_WHITELIST: set[str] = {"USD", "EUR", "GBP", "SGD", "JPY", "CNY"}


# ---------------------------------------------------------------------------
# Documents and ground truth
# ---------------------------------------------------------------------------


class Document(BaseModel):
    """A single input document. `text_layout` is the pre-extracted pdfplumber view
    (see layout.extract_layout) — skills and the router consume this, never the raw PDF."""

    doc_id: str
    format_id_true: Optional[str] = None  # generator-known ground-truth format (eval only)
    pdf_path: Optional[str] = None
    text_layout: dict


class GroundTruth(BaseModel):
    doc_id: str
    fields: dict[str, str]  # field name -> canonical string value


# ---------------------------------------------------------------------------
# Extraction / validation
# ---------------------------------------------------------------------------


class ExtractionResult(BaseModel):
    doc_id: str
    fields: dict[str, str]
    source: Literal["skill", "llm"]
    skill_id: Optional[str] = None
    cost_usd: float = 0.0
    latency_ms: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class ValidationVerdict(BaseModel):
    doc_id: str
    passed: bool
    rule_failures: list[str] = Field(default_factory=list)
    # eval-mode only: field name -> exact-match against ground truth
    field_diffs: Optional[dict[str, bool]] = None


# ---------------------------------------------------------------------------
# Trace — one row per document processed
# ---------------------------------------------------------------------------


class Trace(BaseModel):
    doc_id: str
    ts: float  # unix seconds
    route_format_id: Optional[str]
    # CONTRACT (frozen): route_confidence is the fingerprint similarity to the format whose
    # skill actually executed. On a miss (LLM path) it is the best similarity found (0.0 if
    # no formats known). Under Phase-4 routing_noise the injector corrupts *matching*, so a
    # forced misroute records a genuinely low confidence — this is what lets attribution tell
    # routing_error (low confidence) from data_drift (high confidence). Never back-fill a
    # misroute with 1.0.
    route_confidence: float
    route_method: Literal["exact", "fuzzy", "miss"]
    route_fingerprint: Optional[str] = None  # raw fingerprint hash of this doc
    skill_id: Optional[str] = None
    skill_version: Optional[int] = None
    extraction_source: Literal["skill", "llm"]
    field_results: dict[str, bool] = Field(default_factory=dict)  # eval-mode field correctness
    # the VALUES that were extracted, not just whether they matched. Booleans alone make a run
    # un-replayable: "invoice_number was wrong" cannot be distinguished from "invoice_number was
    # wrong by one leading '#'" after the fact, and that distinction changed this project's
    # reading of its own live results.
    extracted_fields: dict[str, str] = Field(default_factory=dict)
    validation: ValidationVerdict
    # CONTRACT (frozen): fraction of this document's tokens that are character-class clean, as
    # measured on the text_layout at ingestion (layout.input_integrity). 1.0 = pristine. It is a
    # property of the INPUT, computed before any component runs, so attribution can peel a
    # degraded-input failure off a batch without charging any component.
    input_integrity: float = 1.0
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0


# ---------------------------------------------------------------------------
# Skill + attribution (populated from Phase 2 onward; defined now so storage layers
# and the trace schema are stable from day one)
# ---------------------------------------------------------------------------


class Skill(BaseModel):
    skill_id: str
    format_id: str
    version: int
    code_path: str
    state: Literal["candidate", "trial", "active", "deprecated"]
    # discounted-Beta ledger; confidence = alpha / (alpha + beta)
    alpha: float = 1.0
    beta: float = 1.0
    admission_report: dict = Field(default_factory=dict)
    created_ts: float = 0.0
    deprecated_reason: Optional[str] = None

    @property
    def confidence(self) -> float:
        return self.alpha / (self.alpha + self.beta)


class AttributionVerdict(BaseModel):
    failure_batch_id: str
    root_cause: Literal[
        "skill_defect", "routing_error", "rule_defect", "data_drift",
        "input_noise", "ambiguous",
    ]
    evidence: dict = Field(default_factory=dict)
    action_taken: str = ""
