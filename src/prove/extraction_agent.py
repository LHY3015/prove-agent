"""LLM fallback extractor — used only on a routing miss (cold start, or an unknown format).

Strict-JSON contract, one retry on parse failure, token/cost recorded on every call
(Hard Design Rule 4). This agent NEVER judges quality; it only proposes field values, which
the deterministic validator then checks.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .llm_client import LLMClient
from .schemas import TARGET_FIELDS, Document, ExtractionResult

_SYSTEM = (
    "You are a precise invoice field extractor. You output ONLY a single JSON object and "
    "nothing else — no prose, no markdown fences. Copy each value exactly as it appears in "
    "the document (do not reformat dates or numbers). If a field is absent, use an empty "
    "string."
)

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _build_user_prompt(document: Document) -> str:
    layout = document.text_layout
    lines = layout.get("full_text", "")
    fields = ", ".join(TARGET_FIELDS)
    return (
        f"Extract these fields as JSON string values: {fields}.\n"
        f"For line_item_count, give the number of line items as a string.\n\n"
        f"Document text (one line per row):\n{lines}\n"
    )


def _parse_json(text: str) -> dict[str, Any] | None:
    """Best-effort extraction of the JSON object from a model response: try a fenced
    block, then the first {...} span, then the raw text."""
    candidates: list[str] = []
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1))
    obj = _OBJ_RE.search(text)
    if obj:
        candidates.append(obj.group(0))
    candidates.append(text)
    for c in candidates:
        try:
            result = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict):
            return result
    return None


class ExtractionAgent:
    def __init__(self, client: LLMClient, model: str):
        self.client = client
        self.model = model

    def extract(self, document: Document) -> ExtractionResult:
        user = _build_user_prompt(document)
        tokens_in = tokens_out = 0
        cost = 0.0
        latency = 0
        parsed: dict[str, Any] | None = None

        for attempt in range(2):  # initial + one retry on parse failure
            prompt = user if attempt == 0 else user + "\nReturn ONLY valid JSON, no other text."
            resp = self.client.complete(_SYSTEM, prompt, model=self.model)
            tokens_in += resp.tokens_in
            tokens_out += resp.tokens_out
            cost += resp.cost_usd
            latency += resp.latency_ms
            parsed = _parse_json(resp.text)
            if parsed is not None:
                break

        fields = {f: "" for f in TARGET_FIELDS}
        if parsed:
            for f in TARGET_FIELDS:
                val = parsed.get(f, "")
                fields[f] = "" if val is None else str(val)

        return ExtractionResult(
            doc_id=document.doc_id,
            fields=fields,
            source="llm",
            skill_id=None,
            cost_usd=cost,
            latency_ms=latency,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
