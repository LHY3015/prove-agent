"""FastAPI surface: /extract carries a full evidence card; /skills and /traces expose the
registry ledger and the trace stream."""

import json

from starlette.testclient import TestClient

from prove.config import load_config
from prove.llm_client import FakeClient
from prove.pipeline import Pipeline
from prove.service import create_app

_VALID = {"vendor_name": "Acme", "invoice_number": "ACM-2024-00001",
          "invoice_date": "2024-03-01", "currency": "USD", "subtotal": "100.00",
          "tax": "7.00", "total": "107.00", "line_item_count": "2"}


def _client():
    cfg = load_config()
    cfg["ablation"] = {"mode": "A3"}
    pipe = Pipeline(cfg, FakeClient(lambda s, u, m: json.dumps(_VALID)))
    # a skill on the books so /skills has content
    sk = pipe.registry.create_candidate("FP", "def extract(t):\n    return {}\n", 1)
    pipe.registry.admit_to_trial(sk.skill_id, {"holdout_f1": 1.0, "holdout_n": 3})
    return TestClient(create_app(pipe)), pipe


def test_extract_returns_fields_and_full_evidence_card():
    client, _ = _client()
    resp = client.post("/extract", json={"doc_id": "d1", "text_layout": {"full_text": "x"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["fields"] == _VALID
    card = body["evidence_card"]
    assert set(card) == {"routing", "executor", "validation", "cost"}
    assert card["executor"]["source"] == "llm"          # no skill for this format → LLM fallback
    assert card["validation"]["passed"] is True          # the returned fields are self-consistent
    assert "tokens_in" in card["cost"]


def test_skills_endpoint_exposes_the_ledger():
    client, _ = _client()
    body = client.get("/skills").json()
    assert len(body["skills"]) == 1
    s = body["skills"][0]
    assert s["state"] == "trial" and 0.0 <= s["confidence"] <= 1.0


def test_traces_endpoint_streams_recent_rows():
    client, _ = _client()
    client.post("/extract", json={"doc_id": "d1", "text_layout": {"full_text": "x"}})
    client.post("/extract", json={"doc_id": "d2", "text_layout": {"full_text": "y"}})
    rows = client.get("/traces?n=10").json()["traces"]
    assert [r["doc_id"] for r in rows] == ["d1", "d2"]
    assert rows[0]["source"] == "llm"
