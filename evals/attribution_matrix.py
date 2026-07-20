"""Attribution fault-injection COVERAGE matrix: injected vs attributed root cause (Phase 4e).

Each fault scenario injects ONE known root cause; running it in A3 fires attribution verdicts whose
`root_cause` is compared against the injected label. This is a *coverage* result, NOT a statistical
accuracy claim: the classifier is deterministic, the runs are single-fault, and the injected label
is therefore run-level, so a full diagonal means "every planted root cause leaves a separable
signature the peel recovers, with zero cross-cause confusion" — n is small (report it per cell) and
the honest reading is separability, not "attribution is 100% accurate." Ground truth is planted by
the injectors (cleaner than any human-annotated benchmark). skill_defect has no clean production
injector (a skill that passes admission then intrinsically degrades is none of the four faults) — it
is the residual hypothesis, covered by unit tests; mixed-cause peel resolution is shown at unit level
(`test_mixed_batch_peel_routing_noise_concurrent_with_drift`).

    python -m evals.attribution_matrix   # -> evals/out/attribution_{confusion.png,matrix.json}
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.ablation import SimulatedLLM
from evals.fake_skills import SpecializedSynthesizer
from evals.plots import plot_confusion_matrix, publish
from scenarios.routing_noise_demo import build_data, run_arm

from prove.config import load_config
from prove.datagen.faults import corrupt_validator
from prove.datagen.generator import FORMATS, DriftSpec, generate_stream
from prove.layout import extract_layout
from prove.llm_client import FakeClient
from prove.pipeline import Pipeline
from prove.schemas import Document, GroundTruth
from prove.validator import validate

_OUT = Path(__file__).parent / "out"
LABELS = ["routing_error", "data_drift", "skill_defect", "rule_defect", "ambiguous"]


def _cfg() -> dict:
    cfg = load_config()
    cfg["synthesis_trigger"] = 6
    cfg["trial_docs"] = 3
    cfg["admission"] = {**cfg["admission"], "min_holdout": 3, "holdout_frac": 0.3}
    cfg["monitor"] = {"window": 12, "failure_rate_threshold": 0.2, "confidence_floor": 0.3,
                      "min_window": 6, "min_failures": 2}
    return cfg


def routing_verdicts(seed: int = 5) -> list[str]:
    data_dir = _OUT / f"am_routing_s{seed}"
    warm, prod = build_data(data_dir, n_formats=4, warmup_n=12, prod_n=20, seed=seed)
    _, pipe = run_arm("A3", warm, prod, _cfg(), noise_rate=0.3, seed=seed)
    return [a["root_cause"] for a in pipe.attributions]


def drift_verdicts(seed: int = 5) -> list[str]:
    data_dir = _OUT / f"am_drift_s{seed}"
    fmt = FORMATS[0]
    man = generate_stream(data_dir, fmt, n=40, seed=seed, drift=DriftSpec(20, "%d.%m.%Y"))
    sim = SimulatedLLM([{"fields": e["fields"]} for e in man], error_rate=0.0, seed=seed)
    synth = SpecializedSynthesizer()
    synth_model = _cfg()["model"]["synthesis"]

    def responder(system, user, model):
        return synth(system, user, model) if model == synth_model else sim(system, user, model)

    pipe = Pipeline(_cfg(), FakeClient(responder))
    for e in man:
        doc = Document(doc_id=e["doc_id"], format_id_true=e["format_id_true"],
                       text_layout=extract_layout(e["pdf_path"]))
        pipe.process(doc, GroundTruth(doc_id=e["doc_id"], fields=e["fields"]))
    return [a["root_cause"] for a in pipe.attributions]


class _SwitchableValidator:
    """Clean validator until `.corrupt` is flipped on (mid-run rule_corruption after warm-up). The
    pipeline and its auditor share this one instance, so flipping it corrupts both at once."""

    def __init__(self):
        self.corrupt = False
        self._corrupt_fn = corrupt_validator("USD")

    def __call__(self, result, gt=None, **kwargs):
        return self._corrupt_fn(result, gt, **kwargs) if self.corrupt else validate(result, gt, **kwargs)


def rule_defect_verdicts(seed: int = 5) -> list[str]:
    data_dir = _OUT / f"am_rule_s{seed}"
    fmt = FORMATS[0]  # F1_acme is a USD format → the corrupted currency rule fires on it
    man = generate_stream(data_dir, fmt, n=36, seed=seed)
    sim = SimulatedLLM([{"fields": e["fields"]} for e in man], error_rate=0.0, seed=seed)
    synth = SpecializedSynthesizer()
    synth_model = _cfg()["model"]["synthesis"]

    def responder(system, user, model):
        return synth(system, user, model) if model == synth_model else sim(system, user, model)

    validator = _SwitchableValidator()
    pipe = Pipeline(_cfg(), FakeClient(responder), validator=validator)
    for i, e in enumerate(man):
        if i == 18:
            validator.corrupt = True   # corrupt the currency rule after the skill is admitted
        doc = Document(doc_id=e["doc_id"], format_id_true=e["format_id_true"],
                       text_layout=extract_layout(e["pdf_path"]))
        pipe.process(doc, GroundTruth(doc_id=e["doc_id"], fields=e["fields"]))
    return [a["root_cause"] for a in pipe.attributions]


def build_matrix(seed: int = 5) -> dict:
    injected = {"routing_error": routing_verdicts(seed), "data_drift": drift_verdicts(seed),
                "rule_defect": rule_defect_verdicts(seed)}
    matrix = {inj: {c: 0 for c in LABELS} for inj in LABELS}
    total = correct = 0
    for inj, verdicts in injected.items():
        for v in verdicts:
            matrix[inj][v] += 1
            total += 1
            correct += int(v == inj)
    diagonal_coverage = round(correct / total, 3) if total else 0.0
    return {"matrix": matrix, "diagonal_coverage": diagonal_coverage, "n_verdicts": total,
            "per_cause_counts": {k: len(v) for k, v in injected.items()}}


def main() -> None:
    _OUT.mkdir(parents=True, exist_ok=True)
    result = build_matrix()
    (_OUT / "attribution_matrix.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    plot = publish(plot_confusion_matrix(result["matrix"], LABELS,
                                     _OUT / "attribution_confusion.png"))
    print(json.dumps({k: v for k, v in result.items() if k != "matrix"}, indent=2))
    print(f"confusion matrix -> {plot}")


if __name__ == "__main__":
    main()
