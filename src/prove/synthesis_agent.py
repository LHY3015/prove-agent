"""Synthesis agent: an LLM compiles a format's verified samples into a parser skill.

Input = the TRAINING split of a fingerprint's verified pool (text_layout + validated fields,
capped ~10; the admission holdout is split off first and never shown here). The LLM writes
`def extract(text_layout: dict) -> dict[str, str]`. A self-repair loop then runs the candidate
in the sandbox against those same training samples; on any mismatch/error the failure report is
fed back and the LLM tries again, up to `max_attempts`. The agent never judges quality — the
sandbox run against the verified samples is the objective signal (Hard Design Rule 1).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .llm_client import LLMClient
from .sample_pool import PoolSample
from .sandbox import ALLOWED_IMPORTS, run_skill
from .schemas import TARGET_FIELDS
from .validator import compare_fields

_MAX_SAMPLES = 10

_SYSTEM = (
    "You are a careful Python engineer. You write a single self-contained function\n"
    "    def extract(text_layout: dict) -> dict:\n"
    "that reads a pre-parsed document layout and returns the requested fields as strings.\n"
    f"You may import ONLY from: {', '.join(sorted(ALLOWED_IMPORTS))}. No file, OS, or network\n"
    "access is available. Copy values verbatim as they appear (do not reformat dates/numbers).\n"
    "Output ONLY the code — no prose, no markdown fences."
)

_FENCE_RE = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL)


def _compact_sample(s: PoolSample) -> dict[str, Any]:
    """A prompt-sized view of a sample: the reconstructed lines + full_text + target fields.
    The skill still receives the FULL text_layout (words + bboxes) at runtime."""
    tl = s.text_layout
    return {
        "lines": [ln["text"] for ln in tl.get("lines", [])],
        "full_text": tl.get("full_text", ""),
        "fields": s.fields,
    }


def _build_prompt(format_id: str, samples: list[PoolSample], failure: str | None) -> str:
    payload = [_compact_sample(s) for s in samples]
    parts = [
        f"Extract these fields (all as strings): {', '.join(TARGET_FIELDS)}.",
        "line_item_count is the number of line items, as a string.",
        "",
        "text_layout has keys: page_width, page_height, "
        "words (list of {text,x0,top,x1,bottom}), lines (list of {text,top,x0}), full_text.",
        "",
        f"Format id: {format_id}",
        f"{len(samples)} verified example(s) (SAMPLES_JSON):",
        json.dumps(payload),
    ]
    if failure:
        parts += [
            "",
            "Your previous attempt was INCORRECT on these examples:",
            failure,
            "Return a corrected full function.",
        ]
    return "\n".join(parts)


def _extract_code(text: str) -> str:
    m = _FENCE_RE.search(text)
    return (m.group(1) if m else text).strip()


@dataclass
class SynthesisResult:
    code: str
    passed_training: bool
    attempts: int
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    report: dict = field(default_factory=dict)


class SynthesisAgent:
    def __init__(
        self,
        client: LLMClient,
        model: str,
        *,
        max_attempts: int = 3,
        cpu_seconds: int = 5,
        mem_mb: int = 512,
    ):
        self.client = client
        self.model = model
        self.max_attempts = max_attempts
        self.cpu_seconds = cpu_seconds
        self.mem_mb = mem_mb

    def synthesize(self, format_id: str, samples: list[PoolSample]) -> SynthesisResult:
        train = samples[:_MAX_SAMPLES]
        cost = 0.0
        tin = tout = 0
        code = ""
        failure: str | None = None

        for attempt in range(1, self.max_attempts + 1):
            user = _build_prompt(format_id, train, failure)
            resp = self.client.complete(_SYSTEM, user, model=self.model)
            cost += resp.cost_usd
            tin += resp.tokens_in
            tout += resp.tokens_out
            code = _extract_code(resp.text)

            failures = self._run_training(code, train)
            if not failures:
                return SynthesisResult(
                    code=code, passed_training=True, attempts=attempt,
                    cost_usd=cost, tokens_in=tin, tokens_out=tout,
                    report={"final_attempt": attempt},
                )
            failure = self._format_failures(failures)

        return SynthesisResult(
            code=code, passed_training=False, attempts=self.max_attempts,
            cost_usd=cost, tokens_in=tin, tokens_out=tout,
            report={"training_failures": failure},
        )

    def _run_training(self, code: str, samples: list[PoolSample]) -> list[str]:
        """Return human-readable failure lines for training samples the candidate gets wrong;
        empty list means the candidate reproduces every training sample."""
        failures: list[str] = []
        for s in samples:
            res = run_skill(
                code, s.text_layout, cpu_seconds=self.cpu_seconds, mem_mb=self.mem_mb
            )
            if not res.ok:
                failures.append(f"{s.doc_id}: raised/blocked -> {res.error}")
                continue
            diffs = compare_fields(res.value or {}, s.fields)
            wrong = {
                f: (res.value.get(f, ""), s.fields.get(f, ""))
                for f, ok in diffs.items() if not ok
            }
            if wrong:
                detail = ", ".join(f"{f}: got {g!r} expected {e!r}" for f, (g, e) in wrong.items())
                failures.append(f"{s.doc_id}: {detail}")
        return failures

    @staticmethod
    def _format_failures(failures: list[str]) -> str:
        return "\n".join(failures[:8])
