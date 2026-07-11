"""Integration: a mini end-to-end run of the A0 loop with a fake LLM (no API key).

This is the Phase-1 form of the plan's "full-loop mini-run" — route -> miss -> LLM extract
-> validate -> pool + trace, over every document, deterministically."""

from evals.ablation import SimulatedLLM

from prove.config import load_config
from prove.layout import extract_layout
from prove.llm_client import FakeClient
from prove.pipeline import Pipeline
from prove.schemas import Document, GroundTruth
from prove.validator import field_f1


def _docs(manifest):
    for e in manifest:
        layout = extract_layout(e["pdf_path"])
        yield (
            Document(doc_id=e["doc_id"], format_id_true=e["format_id_true"], text_layout=layout),
            GroundTruth(doc_id=e["doc_id"], fields=e["fields"]),
        )


def _pipeline(manifest, error_rate):
    cfg = load_config()
    cfg["llm"]["provider"] = "fake"
    client = FakeClient(SimulatedLLM(manifest, error_rate=error_rate, seed=1))
    return Pipeline(cfg, client)


def test_clean_run_all_pass(mini_dataset):
    pipe = _pipeline(mini_dataset, error_rate=0.0)
    f1s = []
    for doc, gt in _docs(mini_dataset):
        trace = pipe.process(doc, gt)
        assert trace.extraction_source == "llm"   # A0: no skills -> always LLM
        assert trace.route_method == "miss"        # nothing registered
        assert trace.tokens_in > 0 and trace.tokens_out > 0
        f1s.append(field_f1(trace.field_results))

    n = len(mini_dataset)
    assert pipe.traces.count() == n
    assert pipe.pool.total() == n                  # every clean doc joins the pool
    assert sum(f1s) / n == 1.0


def test_errors_reduce_pool_and_f1(mini_dataset):
    pipe = _pipeline(mini_dataset, error_rate=0.5)
    for doc, gt in _docs(mini_dataset):
        pipe.process(doc, gt)

    n = len(mini_dataset)
    assert pipe.traces.count() == n                # every doc is traced regardless
    assert pipe.pool.total() < n                   # some fail validation -> excluded


def test_pool_grouped_by_fingerprint(mini_dataset):
    pipe = _pipeline(mini_dataset, error_rate=0.0)
    for doc, gt in _docs(mini_dataset):
        pipe.process(doc, gt)
    # 4 formats -> 4 fingerprint groups, each with its docs
    assert len(pipe.pool.fingerprints()) == 4
