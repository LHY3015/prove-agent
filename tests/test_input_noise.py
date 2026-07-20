"""input_noise: the ingestion-seam garbler and the integrity signal it is measured by.

The account exists because degraded documents are invisible to every other peel: the garbler
damages the page BODY, leaving the header fingerprint intact, so the document routes exactly at
~1.0 confidence and its failure looks identical to a skill defect.

The A2-kills / A3-spares pipeline pair for this account lives in `test_phase4_attribution.py`
alongside its routing-noise counterpart; this module covers the injector and the signal.
"""

from __future__ import annotations

from prove.datagen.faults import LayoutGarbler
from prove.layout import input_integrity


def _layout(n_words: int = 40, page_height: float = 800.0) -> dict:
    words = [
        {"text": f"token{i}", "x0": 50.0, "top": 20.0 * i,
         "x1": 90.0, "bottom": 20.0 * i + 10}
        for i in range(n_words)
    ]
    return {
        "schema_version": 1, "page_width": 600.0, "page_height": page_height,
        "words": words,
        "lines": [{"text": w["text"], "top": w["top"], "x0": w["x0"]} for w in words],
        "full_text": "\n".join(w["text"] for w in words),
    }


def test_clean_layout_scores_pristine():
    assert input_integrity(_layout()) == 1.0


def test_empty_layout_is_not_reported_as_damaged():
    # no words is an absence of evidence, not evidence of damage; the emptiness surfaces
    # elsewhere (routing miss / validation), never as a fabricated input-noise charge.
    assert input_integrity({"words": []}) == 1.0


def test_ordinary_punctuation_and_currency_symbols_are_not_damage():
    layout = _layout(4)
    for w, text in zip(layout["words"], ["Invoice", "No.:", "$1,204.50", "Ltd&Co"]):
        w["text"] = text
    assert input_integrity(layout) == 1.0


def test_garbler_damages_only_the_configured_band():
    garbler = LayoutGarbler(noise_rate=1.0, token_frac=1.0, band=(0.5, 1.0), seed=1)
    layout = _layout()
    out = garbler.apply("d0", layout)

    tops = [w["top"] for w in layout["words"]]
    cutoff = min(tops) + 0.5 * (max(tops) - min(tops))
    for w in out["words"]:
        if w["top"] < cutoff:
            assert w["text"].startswith("token"), "header region must survive intact"
    assert input_integrity(out) < 1.0
    assert garbler.faults[0]["doc_id"] == "d0"


def test_garbler_rebuilds_the_derived_views():
    # lines/full_text are what the extraction agent and many skills read; a garbler that damaged
    # only `words` would leave consumers disagreeing about the same document.
    garbler = LayoutGarbler(noise_rate=1.0, token_frac=1.0, band=(0.0, 1.0), seed=2)
    out = garbler.apply("d0", _layout())
    assert "token0" not in out["full_text"]
    assert len(out["lines"]) == len(out["words"])


def test_garbler_leaves_the_original_layout_untouched():
    layout = _layout()
    garbler = LayoutGarbler(noise_rate=1.0, token_frac=1.0, band=(0.0, 1.0), seed=3)
    garbler.apply("d0", layout)
    assert all(w["text"].startswith("token") for w in layout["words"])


def test_garbler_respects_its_rate_and_logs_only_what_it_fired_on():
    garbler = LayoutGarbler(noise_rate=0.5, token_frac=1.0, band=(0.0, 1.0), seed=0)
    doc_ids = [f"d{i}" for i in range(40)]
    for d in doc_ids:
        garbler.apply(d, _layout(10))

    labels = garbler.labels(doc_ids)
    fired = [d for d, lab in labels.items() if lab == "input_noise"]
    assert len(fired) == len(garbler.faults)
    assert 0.3 < len(fired) / len(doc_ids) < 0.7
    assert set(labels.values()) <= {"input_noise", "none"}


def test_degraded_documents_still_route_to_their_own_format():
    """The premise of the whole account, on real rendered PDFs: body damage does not change the
    header fingerprint, so the document still exact-matches its format."""
    import tempfile
    from pathlib import Path

    from prove.datagen.generator import FORMATS, generate_dataset
    from prove.layout import extract_layout
    from prove.router import Router

    with tempfile.TemporaryDirectory() as d:
        manifest = generate_dataset(Path(d), samples_per_format=2, seed=0, formats=[FORMATS[0]])
        layouts = [extract_layout(e["pdf_path"]) for e in manifest]

        router = Router()
        router.register("F1", layouts[0])

        garbler = LayoutGarbler(noise_rate=1.0, token_frac=0.8, band=(0.35, 1.0), seed=0)
        degraded = garbler.apply("d1", layouts[1])

        fmt_id, conf, method = router.route(degraded)
        assert method == "exact" and fmt_id == "F1", "body damage must not break routing"
        assert input_integrity(degraded) < 0.95, "damage must be visible to the integrity signal"
