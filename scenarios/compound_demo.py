"""Compound fault: routing noise AND template drift in one stream (Phase 4d, the peel as a peel).

Every other eval injects a single fault, so attribution's per-doc *peel* is never actually
exercised as a peel — a single-fault batch collapses to one label without needing to separate
anything. This scenario runs both faults at once: routing noise misroutes a fraction of all
traffic, while two of the formats drift their date style mid-production. Now a drifted format's
skill sees a genuinely MIXED batch — low-confidence misroutes (the router's fault) interleaved with
its own high-confidence post-drift failures (real drift) — and the peel must exonerate the former
while charging the latter.

What A3 should do, all in one run:
  - healthy (non-drifted) formats hit by misroutes → routing_error → spared, ledger untouched;
  - drifted formats → data_drift → deprecate + resynthesise + heal (a v2 serves the new style).

Documented boundary (do NOT tune it away): if a noise-driven trip fires just BEFORE a format's
drift onset, the window reset leaves the refilled window starting at onset with no clean prefix, so
that drift reads as skill_defect. That cell is *remedy-equivalent* — skill_defect and data_drift
both deprecate-and-resynthesise, so the format heals identically; only the label differs.

    python scenarios/compound_demo.py   # -> evals/out/compound_summary.json
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.ablation import SimulatedLLM  # noqa: E402
from evals.fake_skills import SpecializedSynthesizer  # noqa: E402
from scenarios.routing_noise_demo import _load, _serving  # noqa: E402

from prove.config import load_config  # noqa: E402
from prove.datagen.faults import NoisyRouter  # noqa: E402
from prove.datagen.generator import FORMATS, DriftSpec, generate_dataset, generate_stream  # noqa: E402
from prove.layout import extract_layout  # noqa: E402
from prove.llm_client import FakeClient  # noqa: E402
from prove.pipeline import Pipeline  # noqa: E402
from prove.registry import Registry  # noqa: E402
from prove.router import compute_fingerprint, fingerprint_hash  # noqa: E402

_OUT = Path(__file__).resolve().parent.parent / "evals" / "out"
# Drift target style must be fingerprint-STABLE, else the drift becomes new-format discovery
# (routes as a miss) rather than data_drift on the existing skill. Only single-token date styles
# drift stably: a month-name style (e.g. "%d %b %Y") spans 3 header tokens, so changing it shifts
# the fingerprint. "%m/%d/%Y" is single-token and unlearned by the F1/F4 skills below.
_DRIFT_STYLE = "%m/%d/%Y"
# formats[0]=F1 (%Y-%m-%d) and formats[3]=F4 (%d.%m.%Y) both have single-token dates → stable drift.
_DRIFT_FORMAT_INDICES = (0, 3)


def build_compound_data(out_dir: Path, formats, warmup_n: int, prod_n: int,
                        drift_formats: set[str], drift_at: int, seed: int):
    """Warm-up (clean, format-ordered) + an interleaved production stream where `drift_formats`
    change date style at `drift_at`. Round-robin interleave preserves each format's internal order,
    so a drifted skill still sees pre-drift-then-post-drift onset in its own window."""
    warm = generate_dataset(out_dir / "warm", samples_per_format=warmup_n, seed=seed,
                            formats=formats)
    streams = []
    for i, fmt in enumerate(formats):
        drift = DriftSpec(drift_at, _DRIFT_STYLE) if fmt.format_id in drift_formats else None
        streams.append(generate_stream(out_dir / f"prod_{fmt.format_id}", fmt, prod_n,
                                       seed=seed + 100 + i, drift=drift))
    prod = [doc for row in zip(*streams) for doc in row]   # round-robin, per-format order intact
    return warm, prod


def run(n_formats: int, warmup_n: int, prod_n: int, drift_at: int,
        noise_rate: float, seed: int) -> dict:
    cfg = load_config()
    cfg["synthesis_trigger"] = 6
    cfg["trial_docs"] = 3
    cfg["admission"] = {**cfg["admission"], "min_holdout": 3, "holdout_frac": 0.3}
    cfg["monitor"] = {"window": 15, "failure_rate_threshold": 0.2, "confidence_floor": 0.3,
                      "min_window": 8, "min_failures": 3}
    cfg["ablation"] = {"mode": "A3"}

    formats = FORMATS[:n_formats]
    drift_formats = {formats[i].format_id for i in _DRIFT_FORMAT_INDICES}
    data_dir = _OUT / f"compound_f{n_formats}_s{seed}"
    warm, prod = build_compound_data(data_dir, formats, warmup_n, prod_n,
                                     drift_formats, drift_at, seed)

    # skills are keyed by fingerprint hash, not the generator's format id — map the drifted
    # generator formats to their fingerprints (stable across the date-style drift by design).
    drift_fps = {fingerprint_hash(compute_fingerprint(extract_layout(e["pdf_path"])))
                 for e in warm if e["format_id_true"] in drift_formats}

    reg = Registry(":memory:")
    router = NoisyRouter(lambda: _serving(reg), noise_rate=0.0, seed=seed)
    sim = SimulatedLLM([{"fields": e["fields"]} for e in warm + prod], error_rate=0.0, seed=seed)
    synth = SpecializedSynthesizer()
    synth_model = cfg["model"]["synthesis"]

    def responder(system, user, model):
        return synth(system, user, model) if model == synth_model else sim(system, user, model)

    pipe = Pipeline(cfg, FakeClient(responder), router=router, registry=reg)

    for e in warm:
        doc, gt = _load(e)
        router.begin(doc.doc_id, doc.format_id_true)
        pipe.process(doc, gt)

    router.noise_rate = noise_rate
    for e in prod:
        doc, gt = _load(e)
        router.begin(doc.doc_id, doc.format_id_true)
        pipe.process(doc, gt)

    return _summarize(pipe, reg, drift_fps)


def _summarize(pipe: Pipeline, reg: Registry, drift_fps: set[str]) -> dict:
    causes = Counter(a["root_cause"] for a in pipe.attributions)
    served = _serving(reg)
    all_fps = {s.format_id for s in reg.all_skills()}

    def deprecated(fp: str) -> bool:
        return any(s.state == "deprecated" for s in reg.all_skills() if s.format_id == fp)

    drifted_deprecated = sorted(fp for fp in drift_fps if deprecated(fp))
    drifted_healed = sorted(fp for fp in drift_fps if deprecated(fp) and fp in served)
    healthy_fps = all_fps - drift_fps
    healthy_spared = sorted(fp for fp in healthy_fps if not deprecated(fp))

    drift_family = {c: causes.get(c, 0) for c in ("data_drift", "skill_defect")}
    return {
        "verdicts_by_cause": dict(causes),
        "routing_error_verdicts": causes.get("routing_error", 0),
        "drift_family_verdicts": drift_family,           # data_drift + remedy-equivalent skill_defect
        "drifted_formats_deprecated": len(drifted_deprecated),
        "drifted_formats_healed": len(drifted_healed),   # deprecated AND a v2 already re-serving
        "healthy_formats_spared": len(healthy_spared),
        "healthy_formats_total": len(healthy_fps),
    }


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    summary = run(n_formats=4, warmup_n=14, prod_n=40, drift_at=12, noise_rate=0.25, seed=1)
    (_OUT / "compound_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    # the claim is peel SEPARATION in one mixed stream: healthy skills spared under noise, drifted
    # skills correctly charged to drift (deprecated). Full re-admission is a bonus of a long-enough run.
    ok = (summary["routing_error_verdicts"] >= 1
          and sum(summary["drift_family_verdicts"].values()) >= 1
          and summary["drifted_formats_deprecated"] >= 1
          and summary["healthy_formats_spared"] == summary["healthy_formats_total"])
    print("PEEL SEPARATED routing noise from drift in one stream"
          if ok else "inspect the summary — separation not demonstrated")


if __name__ == "__main__":
    main()
