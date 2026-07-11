"""Ablation runner (configs A0-A3).

  A0  no skills, pure LLM extraction (baseline cost/F1).
  A1  synthesis on, admission gate OFF (candidate -> active) — silent wrong-field failures.
  A2  synthesis on, admission gate ON.
  A3  A2 + attribution-corrected ledger (attribution is Phase 4; A3 == A2 until then).

Two modes:
  - default (no --live): a *simulated* LLM (FakeClient) supplies both extraction (ground-truth
    lookup with an error rate) and synthesis (real canned parser code via evals.fake_skills), so
    the whole loop runs end-to-end with no API key (CI-safe). Illustrative plumbing, not science.
    `--overfit-first-k` makes the first k synthesized formats emit the overfit demonstrator
    (identical synthesis stream across arms — only the gate differs).
  - --live: the real OpenAI-compatible client (Qwen). Prints a cost estimate first. Only
    --live numbers are reported as results.

Outputs metrics JSONL (one row per doc) + a summary JSON under evals/out/.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path
from typing import Any

from evals.fake_skills import FakeSynthesizer

from prove.config import load_config
from prove.datagen.generator import FORMATS, generate_dataset, load_manifest
from prove.layout import extract_layout
from prove.llm_client import FakeClient, build_client
from prove.pipeline import Pipeline
from prove.schemas import TARGET_FIELDS, Document, GroundTruth
from prove.traces import TraceStore
from prove.validator import field_f1

_OUT_DIR = Path(__file__).parent / "out"
_INV_RE = re.compile(r"[A-Z]{3}-\d{4}-\d{5}")


# --------------------------------------------------------------------------- data


def build_dataset(samples_per_format: int, seed: int) -> tuple[Path, list[dict[str, Any]]]:
    data_dir = _OUT_DIR / f"data_s{seed}_n{samples_per_format}"
    if (data_dir / "manifest.jsonl").exists():
        return data_dir, load_manifest(data_dir)
    manifest = generate_dataset(data_dir, samples_per_format=samples_per_format, seed=seed)
    return data_dir, manifest


def load_document(entry: dict[str, Any]) -> tuple[Document, GroundTruth]:
    layout = extract_layout(entry["pdf_path"])
    doc = Document(
        doc_id=entry["doc_id"],
        format_id_true=entry["format_id_true"],
        pdf_path=entry["pdf_path"],
        text_layout=layout,
    )
    gt = GroundTruth(doc_id=entry["doc_id"], fields=entry["fields"])
    return doc, gt


# --------------------------------------------------------------------------- fake LLM


class SimulatedLLM:
    """Deterministic stand-in for a real extractor. Looks the document's ground truth up by
    its invoice number (present in the prompt) and returns it as JSON, corrupting each field
    independently with probability `error_rate` — so validation failures and F1 < 1 arise
    the way a fallible LLM would produce them."""

    def __init__(self, manifest: list[dict[str, Any]], error_rate: float, seed: int):
        self.by_inv = {e["fields"]["invoice_number"]: e["fields"] for e in manifest}
        self.error_rate = error_rate
        self.rng = random.Random(seed)

    def _corrupt(self, value: str) -> str:
        if not value:
            return "??"
        return value[:-1] + "X" if value[-1] != "X" else value + "7"

    def __call__(self, system: str, user: str, model: str) -> str:
        m = _INV_RE.search(user)
        fields = dict(self.by_inv.get(m.group(0), {})) if m else {}
        out = {}
        for f in TARGET_FIELDS:
            v = str(fields.get(f, ""))
            if v and self.rng.random() < self.error_rate:
                v = self._corrupt(v)
            out[f] = v
        return json.dumps(out)


# --------------------------------------------------------------------------- run


def run_ablation(
    config_name: str,
    samples_per_format: int,
    seed: int,
    live: bool,
    error_rate: float,
    overfit_first_k: int = 0,
) -> dict[str, Any]:
    cfg = load_config()
    cfg["ablation"]["mode"] = config_name

    data_dir, manifest = build_dataset(samples_per_format, seed)

    if live:
        est_docs = len(manifest)
        print(f"[--live] ~{est_docs} docs against extraction={cfg['model']['extraction']!r} / "
              f"synthesis={cfg['model']['synthesis']!r}; "
              f"costs table {'set' if cfg.get('costs') else 'EMPTY (est. $0.00 — fill it)'}.")
        cfg["llm"]["provider"] = "openai_compat"
        client = build_client(cfg)
    else:
        cfg["llm"]["provider"] = "fake"
        sim = SimulatedLLM(manifest, error_rate, seed)
        synth = FakeSynthesizer(overfit_first_k=overfit_first_k, mode="once")
        synth_model = cfg["model"]["synthesis"]

        def _responder(system: str, user: str, model: str) -> str:
            return synth(system, user, model) if model == synth_model else sim(system, user, model)

        client = FakeClient(_responder, costs=cfg.get("costs"))

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    trace_store = TraceStore(_OUT_DIR / f"{config_name}_traces.sqlite")
    # fresh store each run
    trace_store.close()
    Path(_OUT_DIR / f"{config_name}_traces.sqlite").unlink(missing_ok=True)
    trace_store = TraceStore(_OUT_DIR / f"{config_name}_traces.sqlite")

    pipeline = Pipeline(cfg, client, trace_store=trace_store)

    rows: list[dict[str, Any]] = []
    for entry in manifest:
        doc, gt = load_document(entry)
        trace = pipeline.process(doc, gt)
        f1 = field_f1(trace.field_results)
        rows.append(
            {
                "doc_id": trace.doc_id,
                "format_id_true": entry["format_id_true"],
                "source": trace.extraction_source,
                "route_method": trace.route_method,
                "passed": trace.validation.passed,
                "field_f1": f1,
                # silent failure = a skill output that PASSED validation yet has a wrong field
                "silent_wrong": bool(trace.extraction_source == "skill" and trace.validation.passed and f1 < 1.0),
                "cost_usd": trace.cost_usd,
                "tokens_in": trace.tokens_in,
                "tokens_out": trace.tokens_out,
            }
        )

    metrics_path = _OUT_DIR / f"{config_name}_metrics.jsonl"
    with open(metrics_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    n = len(rows)
    n_skill = sum(1 for r in rows if r["source"] == "skill")
    lifecycle_cost = round(pipeline.lifecycle_cost_usd, 6)
    skills = pipeline.registry.all_skills()
    summary = {
        "config": config_name,
        "mode": "live" if live else "simulated",
        "n_docs": n,
        "mean_field_f1": round(sum(r["field_f1"] for r in rows) / n, 4) if n else 0.0,
        "validation_pass_rate": round(sum(r["passed"] for r in rows) / n, 4) if n else 0.0,
        # silent-failure rate over skill-served docs — the number A1 vs A2 exists to surface
        "skill_docs": n_skill,
        "silent_failure_count": sum(1 for r in rows if r["silent_wrong"]),
        "silent_failure_rate_over_skill": round(
            sum(1 for r in rows if r["silent_wrong"]) / n_skill, 4) if n_skill else 0.0,
        "extraction_cost_per_doc": round(sum(r["cost_usd"] for r in rows) / n, 6) if n else 0.0,
        "extraction_cost_usd": round(sum(r["cost_usd"] for r in rows), 6),
        "synthesis_cost_usd": lifecycle_cost,   # amortized separately, not hidden
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows) + lifecycle_cost, 6),
        "mean_tokens_per_doc": round(sum(r["tokens_in"] + r["tokens_out"] for r in rows) / n, 1) if n else 0.0,
        "source_counts": _counts(r["source"] for r in rows),
        "skills": _counts(s.state for s in skills),
        "n_rejections": sum(len([e for e in pipeline.registry.events(s.skill_id)
                                 if e["event_type"] == "rejected"]) for s in skills),
    }
    with open(_OUT_DIR / f"{config_name}_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    trace_store.close()
    return summary


def _counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="PROVE ablation runner")
    ap.add_argument("--config", default="A0", choices=["A0", "A1", "A2", "A3"])
    ap.add_argument("--samples-per-format", type=int, default=45,
                    help=f"docs per format ({len(FORMATS)} formats)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--live", action="store_true", help="use the real LLM (spends tokens)")
    ap.add_argument("--error-rate", type=float, default=0.04,
                    help="simulated per-field error rate (ignored under --live)")
    ap.add_argument("--overfit-first-k", type=int, default=0,
                    help="simulated only: first k synthesized formats emit the overfit "
                         "demonstrator (use with A1 vs A2 to show the gate catching silent failures)")
    args = ap.parse_args()

    if args.config == "A3":
        print("[note] A3 == A2 until attribution lands (Phase 4).")

    summary = run_ablation(
        args.config, args.samples_per_format, args.seed, args.live, args.error_rate,
        overfit_first_k=args.overfit_first_k,
    )
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
