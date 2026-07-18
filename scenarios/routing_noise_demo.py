"""A2-vs-A3 under routing noise — the attribution headline (Phase 4a DoD).

Two arms over an identical stream: a clean warm-up admits a specialised skill per format, then a
production phase injects routing noise (a fraction of correctly-fingerprinted docs are force-
delivered to *another* format's skill at genuine low confidence). Because each format has its own
date style and skills are date-specialised, a misrouted doc fails validation (its date style is
blanked) while every skill's own traffic keeps passing — so the failures are the router's fault,
not the skill's.

  A2 (no attribution): every misroute failure charges the executing skill's ledger and counts in
    its monitor window → healthy skills get deprecated → their traffic falls back to the LLM and
    resynthesises → token cost rebounds.
  A3 (attribution): the monitor batch is classified routing_error (failures concentrated at low
    confidence while the skill's high-confidence traffic passes) → the skill's ledger is untouched
    and it keeps serving → cost stays low, zero healthy-skill kills.

Key-free: `SimulatedLLM` (ground-truth lookup) + `SpecializedSynthesizer` (date competence learned
from training samples). `--live` would swap in the real Qwen client.

    python scenarios/routing_noise_demo.py   # -> evals/out/routing_noise_{summary.json,comparison.png}
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.ablation import SimulatedLLM  # noqa: E402
from evals.fake_skills import SpecializedSynthesizer  # noqa: E402
from evals.plots import plot_routing_noise_comparison  # noqa: E402

from prove.config import load_config  # noqa: E402
from prove.datagen.faults import NoisyRouter  # noqa: E402
from prove.datagen.generator import FORMATS, generate_dataset  # noqa: E402
from prove.layout import extract_layout  # noqa: E402
from prove.llm_client import FakeClient  # noqa: E402
from prove.pipeline import Pipeline  # noqa: E402
from prove.registry import Registry  # noqa: E402
from prove.schemas import Document, GroundTruth  # noqa: E402

_OUT = Path(__file__).resolve().parent.parent / "evals" / "out"


def _load(entry: dict) -> tuple[Document, GroundTruth]:
    doc = Document(doc_id=entry["doc_id"], format_id_true=entry["format_id_true"],
                   text_layout=extract_layout(entry["pdf_path"]))
    return doc, GroundTruth(doc_id=entry["doc_id"], fields=entry["fields"])


def _serving(reg: Registry) -> set[str]:
    return {s.format_id for s in reg.all_skills() if s.state in ("active", "trial")}


def build_data(out_dir: Path, n_formats: int, warmup_n: int, prod_n: int, seed: int):
    """Warm-up docs (format-ordered, clean) + a shuffled production stream. Returns manifests."""
    formats = FORMATS[:n_formats]
    warm = generate_dataset(out_dir / "warm", samples_per_format=warmup_n, seed=seed,
                            formats=formats)
    prod = generate_dataset(out_dir / "prod", samples_per_format=prod_n, seed=seed + 1000,
                            formats=formats)
    random.Random(seed).shuffle(prod)
    return warm, prod


def run_arm(mode: str, warm: list[dict], prod: list[dict], cfg: dict,
            noise_rate: float, seed: int):
    """Run one ablation arm through warm-up + noisy production. Returns (rows, pipeline); the
    pipeline carries `.attributions` (the fired verdicts) for the confusion matrix."""
    reg = Registry(":memory:")
    router = NoisyRouter(lambda: _serving(reg), noise_rate=0.0, seed=seed)
    sim = SimulatedLLM([{"fields": e["fields"]} for e in warm + prod], error_rate=0.0, seed=seed)
    synth = SpecializedSynthesizer()
    synth_model = cfg["model"]["synthesis"]

    def responder(system, user, model):
        return synth(system, user, model) if model == synth_model else sim(system, user, model)

    cfg = {**cfg, "ablation": {"mode": mode}}
    pipe = Pipeline(cfg, FakeClient(responder), router=router, registry=reg)

    for entry in warm:  # warm-up: noise OFF, admit one specialised skill per format
        doc, gt = _load(entry)
        router.begin(doc.doc_id, doc.format_id_true)
        pipe.process(doc, gt)

    router.noise_rate = noise_rate
    rows = []
    for entry in prod:  # production: noise ON
        doc, gt = _load(entry)
        router.begin(doc.doc_id, doc.format_id_true)
        before = _n_deprecations(reg)
        tr = pipe.process(doc, gt)
        rows.append({
            "tokens": tr.tokens_in + tr.tokens_out,
            "source": tr.extraction_source,
            "passed": tr.validation.passed,
            "deprecated": _n_deprecations(reg) - before,
        })
    return rows, pipe


def _n_deprecations(reg: Registry) -> int:
    return sum(1 for e in reg.events() if e["event_type"] == "deprecated")


def _summarize(rows: list[dict]) -> dict:
    n = len(rows)
    return {
        "n_prod_docs": n,
        "healthy_kills": sum(r["deprecated"] for r in rows),
        "skill_doc_frac": round(sum(r["source"] == "skill" for r in rows) / n, 3),
        "mean_tokens_per_doc": round(sum(r["tokens"] for r in rows) / n, 1),
        "prod_pass_rate": round(sum(r["passed"] for r in rows) / n, 3),
    }


def run(n_formats: int, warmup_n: int, prod_n: int, noise_rate: float, seed: int) -> dict:
    cfg = load_config()
    cfg["synthesis_trigger"] = 6
    cfg["trial_docs"] = 3
    cfg["admission"] = {**cfg["admission"], "min_holdout": 3, "holdout_frac": 0.3}
    cfg["monitor"] = {"window": 15, "failure_rate_threshold": 0.2, "confidence_floor": 0.3,
                      "min_window": 8, "min_failures": 3}

    data_dir = _OUT / f"rn_data_f{n_formats}_w{warmup_n}_p{prod_n}_s{seed}"
    warm, prod = build_data(data_dir, n_formats, warmup_n, prod_n, seed)

    a2, _ = run_arm("A2", warm, prod, cfg, noise_rate, seed)
    a3, _ = run_arm("A3", warm, prod, cfg, noise_rate, seed)
    return {"a2_rows": a2, "a3_rows": a3,
            "summary": {"noise_rate": noise_rate, "n_formats": n_formats,
                        "A2": _summarize(a2), "A3": _summarize(a3)}}


def main() -> None:
    ap = argparse.ArgumentParser(description="PROVE routing-noise A2-vs-A3 demo")
    ap.add_argument("--n-formats", type=int, default=4)
    ap.add_argument("--warmup-n", type=int, default=14)
    ap.add_argument("--prod-n", type=int, default=25)
    ap.add_argument("--noise-rate", type=float, default=0.2)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    result = run(args.n_formats, args.warmup_n, args.prod_n, args.noise_rate, args.seed)
    _OUT.mkdir(parents=True, exist_ok=True)
    (_OUT / "routing_noise_summary.json").write_text(
        json.dumps(result["summary"], indent=2), encoding="utf-8")
    plot = plot_routing_noise_comparison(result["a2_rows"], result["a3_rows"],
                                         _OUT / "routing_noise_comparison.png")
    print(json.dumps(result["summary"], indent=2))
    s = result["summary"]
    print(f"\nSmart data-forgetting — under {int(s['noise_rate'] * 100)}% routing noise: "
          f"A2 forgot {s['A2']['healthy_kills']} healthy memories, A3 forgot {s['A3']['healthy_kills']}.")
    print(f"comparison plot -> {plot}")


if __name__ == "__main__":
    main()
