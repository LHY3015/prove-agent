"""Synthesis agent: the self-repair loop runs candidates in the sandbox against the training
samples and feeds failures back until they pass (or attempts run out)."""

from evals.fake_skills import GENERIC_PARSER

from prove.layout import extract_layout
from prove.llm_client import FakeClient
from prove.sample_pool import PoolSample
from prove.synthesis_agent import SynthesisAgent

_BROKEN = "def extract(tl):\n    return {}"  # passes the sandbox but matches no field


def _samples(mini_dataset):
    fmt = mini_dataset[0]["format_id_true"]
    out = []
    for e in mini_dataset:
        if e["format_id_true"] == fmt:
            out.append(PoolSample(doc_id=e["doc_id"], fingerprint=fmt,
                                  text_layout=extract_layout(e["pdf_path"]), fields=e["fields"]))
    return out


def test_self_repair_converges(mini_dataset):
    calls = {"n": 0}

    def responder(system, user, model):
        calls["n"] += 1
        if calls["n"] == 1:
            return _BROKEN          # first attempt is wrong on training
        assert "was INCORRECT" in user  # the failure report is fed back
        return GENERIC_PARSER       # second attempt is correct

    agent = SynthesisAgent(FakeClient(responder), "synth-model", max_attempts=3)
    result = agent.synthesize("fp", _samples(mini_dataset))
    assert result.passed_training and result.attempts == 2


def test_gives_up_after_max_attempts(mini_dataset):
    agent = SynthesisAgent(FakeClient(lambda s, u, m: _BROKEN), "synth-model", max_attempts=3)
    result = agent.synthesize("fp", _samples(mini_dataset))
    assert not result.passed_training and result.attempts == 3
