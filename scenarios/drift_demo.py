"""End-to-end self-healing demo (Phase 3 DoD).

Runs one format as an ordered stream with a date-format drift injected mid-way, in A2 mode
(admission gate + monitor, no attribution). The closed loop, with zero manual steps:

    skill synthesized + admitted  ->  drift onset  ->  skill's validation failures accumulate
    ->  monitor deprecates it  ->  traffic falls back to the LLM  ->  the pool re-accumulates
    fresh post-drift samples  ->  resynthesis  ->  a new skill is admitted and serves again.

Key-free: the extractor is a ground-truth-lookup `SimulatedLLM`, and synthesis is the
`SpecializedSynthesizer` double whose date competence is LEARNED from its training samples
(so the pre-drift skill genuinely fails on the new date style and the resynthesized one heals).
`--live` would swap in the real Qwen client; here we exercise the machinery.

    python scenarios/drift_demo.py            # writes evals/out/drift_demo_{timeline.json,timeline.png}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root -> import evals/prove

from evals.ablation import SimulatedLLM  # noqa: E402
from evals.fake_skills import SpecializedSynthesizer  # noqa: E402
from evals.plots import plot_drift_timeline, publish  # noqa: E402

from prove.config import load_config  # noqa: E402
from prove.datagen.generator import FORMATS, DriftSpec, generate_stream  # noqa: E402
from prove.layout import extract_layout  # noqa: E402
from prove.llm_client import FakeClient  # noqa: E402
from prove.pipeline import Pipeline  # noqa: E402
from prove.schemas import Document, GroundTruth  # noqa: E402
from prove.validator import field_f1  # noqa: E402

_OUT = Path(__file__).resolve().parent.parent / "evals" / "out"


def run(n: int, drift_at: int, seed: int) -> dict:
    fmt = FORMATS[0]  # F1_acme, date style %Y-%m-%d
    drift = DriftSpec(at_index=drift_at, new_date_style="%d.%m.%Y")  # dashed -> dotted
    data_dir = _OUT / f"drift_data_s{seed}_n{n}"
    manifest = generate_stream(data_dir, fmt, n, seed=seed, drift=drift)

    cfg = load_config()
    cfg["ablation"] = {"mode": "A2"}
    sim = SimulatedLLM(manifest, error_rate=0.0, seed=seed)
    synth = SpecializedSynthesizer()
    synth_model = cfg["model"]["synthesis"]

    def responder(system: str, user: str, model: str) -> str:
        return synth(system, user, model) if model == synth_model else sim(system, user, model)

    pipe = Pipeline(cfg, FakeClient(responder))

    rows = []
    for i, entry in enumerate(manifest):
        doc = Document(doc_id=entry["doc_id"], format_id_true=entry["format_id_true"],
                       text_layout=extract_layout(entry["pdf_path"]))
        gt = GroundTruth(doc_id=entry["doc_id"], fields=entry["fields"])
        tr = pipe.process(doc, gt)
        rows.append({
            "index": i, "doc_id": tr.doc_id, "drifted": entry["drifted"],
            "source": tr.extraction_source,
            "skill_id": tr.skill_id, "skill_version": tr.skill_version,
            "passed": tr.validation.passed, "field_f1": field_f1(tr.field_results),
        })

    markers = _derive_markers(rows, drift_at)
    skills = pipe.registry.all_skills()
    healed = (markers["readmit"] is not None
              and sum(1 for s in skills if s.state == "deprecated") >= 1
              and sum(1 for s in skills if s.state in ("trial", "active")) >= 1)
    summary = {
        "n_docs": n, "drift_at": drift_at, "markers": markers, "self_healed": healed,
        "skills": [{"skill_id": s.skill_id, "version": s.version, "state": s.state,
                    "reason": s.deprecated_reason} for s in skills],
    }
    return {"rows": rows, "summary": summary}


def _derive_markers(rows: list[dict], drift_at: int) -> dict:
    """Locate deprecation (skill -> LLM transition after drift) and re-admission (a higher skill
    version begins serving) from the timeline itself."""
    depr = readmit = None
    base_version = next((r["skill_version"] for r in rows if r["source"] == "skill"), None)
    for prev, cur in zip(rows, rows[1:]):
        if depr is None and prev["source"] == "skill" and cur["source"] == "llm":
            depr = cur["index"]
        if (readmit is None and cur["source"] == "skill" and base_version is not None
                and cur["skill_version"] is not None and cur["skill_version"] > base_version):
            readmit = cur["index"]
    return {"drift": drift_at, "deprecate": depr, "readmit": readmit}


def main() -> None:
    ap = argparse.ArgumentParser(description="PROVE self-healing drift demo")
    ap.add_argument("--n", type=int, default=60, help="docs in the stream")
    ap.add_argument("--drift-at", type=int, default=30, help="doc index where the date style drifts")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    result = run(args.n, args.drift_at, args.seed)
    _OUT.mkdir(parents=True, exist_ok=True)
    (_OUT / "drift_demo_timeline.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    plot_path = publish(plot_drift_timeline(result["rows"], result["summary"]["markers"],
                                            _OUT / "drift_demo_timeline.png"))

    s = result["summary"]
    print(json.dumps(s, indent=2))
    print(f"\ntimeline plot -> {plot_path}")
    print("SELF-HEALED" if s["self_healed"] else "NOT HEALED — inspect the timeline")


if __name__ == "__main__":
    main()
