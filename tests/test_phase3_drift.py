"""Integration: Phase-3 self-healing under template drift (key-free).

Injects a date-format drift into one format's stream and asserts the whole loop closes: the
admitted skill starts failing, the monitor deprecates it, traffic falls back to the LLM, the
pool re-accumulates fresh samples, and a NEW skill is synthesized, admitted, and serves the
drifted docs correctly. Config is tightened so the heal completes in ~26 docs."""

import pytest

from evals.ablation import SimulatedLLM
from evals.fake_skills import SpecializedSynthesizer

from prove.config import load_config
from prove.datagen.generator import FORMATS, DriftSpec, generate_stream
from prove.layout import extract_layout
from prove.llm_client import FakeClient
from prove.pipeline import Pipeline
from prove.registry import Registry
from prove.schemas import Document, GroundTruth
from prove.validator import field_f1

_DRIFT_AT = 10


@pytest.fixture(scope="module")
def drift_stream(tmp_path_factory):
    d = tmp_path_factory.mktemp("p3drift")
    fmt = FORMATS[0]  # F1_acme, %Y-%m-%d
    man = generate_stream(d, fmt, n=26, seed=7, drift=DriftSpec(_DRIFT_AT, "%d.%m.%Y"))
    docs = [
        (Document(doc_id=e["doc_id"], format_id_true=e["format_id_true"],
                  text_layout=extract_layout(e["pdf_path"])),
         GroundTruth(doc_id=e["doc_id"], fields=e["fields"]), e["drifted"])
        for e in man
    ]
    return docs


def _run(drift_stream, tmp_path):
    cfg = load_config()
    cfg["ablation"] = {"mode": "A2"}
    cfg["synthesis_trigger"] = 6
    cfg["trial_docs"] = 3
    cfg["admission"] = {**cfg["admission"], "min_holdout": 3, "holdout_frac": 0.3}
    cfg["monitor"] = {"window": 8, "failure_rate_threshold": 0.2, "confidence_floor": 0.3,
                      "min_window": 6, "min_failures": 2}

    man = [(d, gt) for d, gt, _ in drift_stream]
    sim = SimulatedLLM([{"fields": gt.fields} for _, gt in man], error_rate=0.0, seed=1)
    synth = SpecializedSynthesizer()
    synth_model = cfg["model"]["synthesis"]

    def responder(system, user, model):
        return synth(system, user, model) if model == synth_model else sim(system, user, model)

    reg = Registry(str(tmp_path / "reg.sqlite"), skills_dir=tmp_path / "skills")
    pipe = Pipeline(cfg, FakeClient(responder), registry=reg)

    rows = []
    for (doc, gt), (_, _, drifted) in zip(man, drift_stream):
        tr = pipe.process(doc, gt)
        rows.append({"index": len(rows), "drifted": drifted, "source": tr.extraction_source,
                     "version": tr.skill_version, "passed": tr.validation.passed,
                     "f1": field_f1(tr.field_results)})
    return pipe, rows


def test_self_heals_after_drift(drift_stream, tmp_path):
    pipe, rows = _run(drift_stream, tmp_path)
    skills = pipe.registry.all_skills()

    # a v1 skill was deprecated by the monitor and a v2 skill exists and serves
    deprecated = [s for s in skills if s.state == "deprecated"]
    serving = [s for s in skills if s.state in ("trial", "active")]
    assert len(deprecated) >= 1 and len(serving) >= 1
    assert deprecated[0].version < serving[0].version

    # deprecation came from the FAST window path, not the slow ledger floor (calibration)
    assert deprecated[0].deprecated_reason.startswith("failure_batch:window")

    # a campaign reset was logged on deprecation (rejection counter reset + new campaign id)
    resets = [e for e in pipe.registry.events() if e["event_type"] == "deprecated_reset"]
    assert resets, "expected a deprecated_reset event opening a new synthesis campaign"

    # the healed skill serves DRIFTED docs correctly (proves the pool was invalidated and v2
    # learned the new date style rather than inheriting stale pre-drift training)
    healed_skill_docs = [r for r in rows if r["drifted"] and r["source"] == "skill"
                         and r["version"] == serving[0].version]
    assert healed_skill_docs, "v2 never served a drifted doc"
    assert all(r["passed"] and r["f1"] == 1.0 for r in healed_skill_docs)


def test_drift_produces_skill_failures_then_llm_fallback(drift_stream, tmp_path):
    _, rows = _run(drift_stream, tmp_path)
    # skill validation failures appear only after drift onset
    skill_fails = [r for r in rows if r["source"] == "skill" and not r["passed"]]
    assert skill_fails and all(r["index"] >= _DRIFT_AT for r in skill_fails)
    # after the deprecation, some docs fall back to the LLM and pass again
    post = [r for r in rows if r["index"] > skill_fails[-1]["index"]]
    assert any(r["source"] == "llm" and r["passed"] for r in post)
