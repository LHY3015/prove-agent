"""LLMClient tests — the test double records calls and counts tokens; cost math is
single-sourced in estimate_cost; the factory never invents responses."""

import pytest

from prove.llm_client import FakeClient, build_client, estimate_cost


def test_fake_client_records_and_counts():
    client = FakeClient(lambda s, u, m: "hello world")
    resp = client.complete("sys", "user prompt here", model="qwen-turbo")
    assert resp.text == "hello world"
    assert resp.tokens_in > 0 and resp.tokens_out > 0
    assert resp.model == "qwen-turbo"
    assert len(client.calls) == 1
    assert client.calls[0]["model"] == "qwen-turbo"


def test_estimate_cost_single_source():
    costs = {"m1": {"in_per_mtok": 2.0, "out_per_mtok": 6.0}}
    assert estimate_cost("m1", 1_000_000, 1_000_000, costs) == pytest.approx(8.0)
    assert estimate_cost("unknown", 1000, 1000, costs) == 0.0
    assert estimate_cost("m1", 0, 0, {}) == 0.0


def test_fake_client_applies_cost_table():
    costs = {"qwen-turbo": {"in_per_mtok": 1.0, "out_per_mtok": 1.0}}
    client = FakeClient(lambda s, u, m: "x" * 400, costs=costs)
    resp = client.complete("s", "u" * 400, model="qwen-turbo")
    assert resp.cost_usd > 0.0


def test_build_client_fake_requires_responder():
    cfg = {"llm": {"provider": "fake"}, "costs": {}}
    with pytest.raises(ValueError):
        build_client(cfg)
    client = build_client(cfg, fake_responder=lambda s, u, m: "{}")
    assert isinstance(client, FakeClient)


def test_build_client_rejects_unknown_provider():
    with pytest.raises(ValueError):
        build_client({"llm": {"provider": "telepathy"}, "costs": {}})
