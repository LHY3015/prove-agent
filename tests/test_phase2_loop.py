"""Integration: the Phase-2 closed loop with a fake LLM (no API key).

Covers the two DoD claims: (1) a format's traffic moves from the LLM to a synthesized+admitted
skill, dropping token cost to zero for those docs; (2) A1 (no admission gate) admits an overfit
skill that emits silent wrong-field outputs, while A2's gate rejects that same skill."""

import pytest

from evals.ablation import SimulatedLLM
from evals.fake_skills import FakeSynthesizer

from prove.config import load_config
from prove.datagen.generator import FORMATS, generate_dataset
from prove.layout import extract_layout
from prove.llm_client import FakeClient
from prove.pipeline import Pipeline
from prove.registry import Registry
from prove.schemas import Document, GroundTruth
from prove.validator import field_f1


@pytest.fixture(scope="module")
def loop_dataset(tmp_path_factory):
    d = tmp_path_factory.mktemp("p2data")
    reps = [FORMATS[0], FORMATS[4]]  # classic + banner, 2 distinct layouts
    man = generate_dataset(d, samples_per_format=14, seed=11, formats=reps)
    docs = [
        (Document(doc_id=e["doc_id"], format_id_true=e["format_id_true"],
                  text_layout=extract_layout(e["pdf_path"])),
         GroundTruth(doc_id=e["doc_id"], fields=e["fields"]))
        for e in man
    ]
    return man, docs


def _run(loop_dataset, mode, overfit_k, tmp_path):
    man, docs = loop_dataset
    cfg = load_config()
    cfg["ablation"] = {"mode": mode}
    sim = SimulatedLLM(man, error_rate=0.0, seed=1)
    synth = FakeSynthesizer(overfit_first_k=overfit_k, mode="once")
    synth_model = cfg["model"]["synthesis"]

    def responder(system, user, model):
        return synth(system, user, model) if model == synth_model else sim(system, user, model)

    reg = Registry(str(tmp_path / "reg.sqlite"), skills_dir=tmp_path / "skills")
    pipe = Pipeline(cfg, FakeClient(responder), registry=reg)

    rows = []
    for doc, gt in docs:
        tr = pipe.process(doc, gt)
        rows.append({
            "source": tr.extraction_source,
            "passed": tr.validation.passed,
            "tokens": tr.tokens_in + tr.tokens_out,
            "silent_wrong": tr.extraction_source == "skill" and tr.validation.passed
            and field_f1(tr.field_results) < 1.0,
        })
    return pipe, rows


def test_skill_comes_online_and_cost_drops(loop_dataset, tmp_path):
    pipe, rows = _run(loop_dataset, "A2", overfit_k=0, tmp_path=tmp_path)
    skill_rows = [r for r in rows if r["source"] == "skill"]
    llm_rows = [r for r in rows if r["source"] == "llm"]

    assert skill_rows, "no doc was ever served by a skill"
    assert all(r["tokens"] == 0 for r in skill_rows)          # skills call no LLM
    assert all(r["tokens"] > 0 for r in llm_rows)
    # a format serves from ~doc 20 (trigger 10 + trial fill), so cold-start LLM docs dominate early
    assert any(s.state in ("trial", "active") for s in pipe.registry.all_skills())


def test_a1_admits_what_a2_rejects(loop_dataset, tmp_path):
    a1_pipe, a1_rows = _run(loop_dataset, "A1", overfit_k=1, tmp_path=tmp_path / "a1")
    a2_pipe, a2_rows = _run(loop_dataset, "A2", overfit_k=1, tmp_path=tmp_path / "a2")

    a1_silent = sum(r["silent_wrong"] for r in a1_rows)
    a2_silent = sum(r["silent_wrong"] for r in a2_rows)

    # A1: the overfit skill went live and emits validation-passing wrong fields.
    assert a1_silent > 0
    assert any(e["event_type"] == "activated_no_gate" for e in a1_pipe.registry.events())

    # A2: the identical overfit candidate was rejected by the gate -> no silent failures.
    assert a2_silent == 0
    assert any(e["event_type"] == "rejected" for e in a2_pipe.registry.events())
