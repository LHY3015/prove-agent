"""Run the pipeline over a real dataset (CORD-v2) — the external-validity probe.

WHAT THIS MEASURES (the safety claim), and what it deliberately does not:

  MEASURED   The router fails CLOSED on documents it has not genuinely seen before: unfamiliar
             real receipts fall back to the LLM rather than being mis-served by a skill built
             for something else. No skill is admitted that cannot clear the held-out gate, and
             per-doc cost stays bounded by the pure-LLM A0 baseline.
  NOT MEASURED  Recurrence. The fingerprint is a born-digital construct (exact Jaccard >= 0.999
             over positional token buckets); real scans carry OCR jitter and crop/skew variation,
             so same-vendor receipts are not expected to exact-match. A near-zero skill-hit rate
             here is the EXPECTED result and is reported as such — it is a scoped limitation of
             this router, not evidence about the memory mechanism, which the synthetic ablations
             measure. Fuzzy routing with normalized coordinates is roadmap.

Offline vs live:
  offline (default)  the simulated LLM answers from the dataset's ground truth. This exercises
                     ingestion, routing, validation and cost accounting on real layouts, but it
                     CANNOT demonstrate the synthesize->admit->serve loop: the offline
                     synthesizer (`evals.fake_skills`) only has hand-written skills for the
                     synthetic formats.
  --live             the real LLM extracts and synthesizes. This is the only mode in which a
                     skill can actually be born from real data.

The validator runs on CORD_PROFILE (subtotal/tax/total/line_item_count) — the four fields CORD
labels. Fewer rules apply than on synthetic invoices, so a *pass* means less here; the profile is
printed with every result so no number is quoted without it.

    python -m evals.real_data --source tests/fixtures/cord_schema_replica.jsonl
    python -m evals.real_data --source hf --limit 200            # downloads CORD-v2
    python -m evals.real_data --source cord.jsonl --limit 200 --live
"""

from __future__ import annotations

import argparse
import functools
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prove.config import load_config  # noqa: E402
from prove.datasets.cord import CORD_PROFILE, iter_documents  # noqa: E402
from prove.llm_client import FakeClient, build_client  # noqa: E402
from prove.pipeline import Pipeline  # noqa: E402
from prove.traces import TraceStore  # noqa: E402
from prove.validator import field_f1, validate  # noqa: E402

_OUT_DIR = Path(__file__).parent / "out"


class _GroundTruthResponder:
    """Offline stand-in for the extractor: answers with the dataset's own labels. No error
    injection — on real data the question is whether the pipeline handles real LAYOUTS, and
    injected value noise would only obscure that.

    Keyed on the document's `full_text`, which is what the prompt actually carries: a real
    receipt has no doc_id printed on it, so there is nothing else in the prompt to key on.
    """

    def __init__(self, docs):
        self._by_text = [(doc.text_layout.get("full_text", ""), gt.fields) for doc, gt in docs]

    def __call__(self, system: str, user: str, model: str) -> str:
        for text, fields in self._by_text:
            if text and text in user:
                return json.dumps(fields)
        return json.dumps({})


def run(source: str, limit: int | None, live: bool, config_name: str) -> dict[str, Any]:
    cfg = load_config()
    cfg["ablation"]["mode"] = config_name

    docs = list(iter_documents(source, limit=limit))
    if not docs:
        raise SystemExit(f"no documents loaded from {source!r}")

    if live:
        print(f"[--live] {len(docs)} real docs against "
              f"extraction={cfg['model']['extraction']!r} / synthesis={cfg['model']['synthesis']!r}; "
              f"costs table {'set' if cfg.get('costs') else 'EMPTY (est. $0.00 — fill it)'}.")
        cfg["llm"]["provider"] = "openai_compat"
        client = build_client(cfg)
    else:
        cfg["llm"]["provider"] = "fake"
        client = FakeClient(_GroundTruthResponder(docs), costs=cfg.get("costs"))

    _OUT_DIR.mkdir(parents=True, exist_ok=True)
    db = _OUT_DIR / f"real_{config_name}_traces.sqlite"
    db.unlink(missing_ok=True)
    trace_store = TraceStore(db)

    # the dataset's field profile is the validator seam the pipeline already exposes
    pipeline = Pipeline(cfg, client, trace_store=trace_store,
                        validator=functools.partial(validate, profile=CORD_PROFILE))

    rows: list[dict[str, Any]] = []
    for doc, gt in docs:
        trace = pipeline.process(doc, gt)
        rows.append({
            "doc_id": trace.doc_id,
            "source": trace.extraction_source,
            "route_method": trace.route_method,
            "route_confidence": round(trace.route_confidence, 4),
            "passed": trace.validation.passed,
            "field_f1": field_f1(trace.field_results),
            "input_integrity": trace.input_integrity,
            "cost_usd": trace.cost_usd,
            "tokens": trace.tokens_in + trace.tokens_out,
        })

    n = len(rows)
    n_skill = sum(1 for r in rows if r["source"] == "skill")
    summary = {
        "dataset": "cord-v2",
        "source": str(source),
        "config": config_name,
        "mode": "live" if live else "simulated",
        "field_profile": CORD_PROFILE,
        "n_docs": n,
        "mean_field_f1": round(sum(r["field_f1"] for r in rows) / n, 4),
        "validation_pass_rate": round(sum(r["passed"] for r in rows) / n, 4),
        # the safety numbers: how often a skill served, and whether routing ever fired loosely
        "skill_docs": n_skill,
        "skill_hit_rate": round(n_skill / n, 4),
        "route_methods": _counts(r["route_method"] for r in rows),
        "mean_route_confidence": round(sum(r["route_confidence"] for r in rows) / n, 4),
        "mean_input_integrity": round(sum(r["input_integrity"] for r in rows) / n, 4),
        "cost_per_doc": round(sum(r["cost_usd"] for r in rows) / n, 6),
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows)
                                + pipeline.lifecycle_cost_usd, 6),
        "skills": _counts(s.state for s in pipeline.registry.all_skills()),
    }

    with open(_OUT_DIR / f"real_{config_name}_metrics.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    with open(_OUT_DIR / f"real_{config_name}_summary.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)

    trace_store.close()
    return summary


def _counts(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="PROVE real-dataset (CORD-v2) runner")
    ap.add_argument("--source", default="tests/fixtures/cord_schema_replica.jsonl",
                    help="JSONL path, or 'hf' to download CORD-v2 from HuggingFace")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--config", default="A3", choices=["A0", "A1", "A2", "A3"])
    ap.add_argument("--live", action="store_true", help="use the real LLM (spends tokens)")
    args = ap.parse_args()

    summary = run(args.source, args.limit, args.live, args.config)
    print(json.dumps(summary, indent=2))
    if summary["skill_hit_rate"] == 0.0:
        print("\n[note] zero skill hits: the exact-match router did not recognise any layout as "
              "recurring. On real scans this is the expected, safe outcome — traffic fell back "
              "to the LLM. See this module's docstring before reporting it as a result.")


if __name__ == "__main__":
    main()
