"""Admission harness: no train/holdout leakage, holdout persistence across retries, and the
gate's decisions (good code passes, memorization overfit is rejected)."""

from evals.fake_skills import GENERIC_PARSER, build_overfit

from prove.admission import Admission
from prove.layout import extract_layout
from prove.sample_pool import PoolSample


def _samples(mini_dataset, fmt):
    out = []
    for e in mini_dataset:
        if e["format_id_true"] != fmt:
            continue
        tl = extract_layout(e["pdf_path"])
        out.append(PoolSample(doc_id=e["doc_id"], fingerprint=fmt, text_layout=tl, fields=e["fields"]))
    return out


def _first_format(mini_dataset):
    return mini_dataset[0]["format_id_true"]


def test_split_is_disjoint_and_holds_out_min(mini_dataset):
    fmt = _first_format(mini_dataset)
    samples = _samples(mini_dataset, fmt)
    adm = Admission(min_holdout=3)
    train, holdout = adm.split(fmt, samples)
    train_ids = {s.doc_id for s in train}
    holdout_ids = {s.doc_id for s in holdout}
    assert train_ids.isdisjoint(holdout_ids)                 # no leakage
    assert len(holdout) >= 3
    assert train_ids | holdout_ids == {s.doc_id for s in samples}


def test_holdout_persists_across_retries(mini_dataset):
    """A retry (or pool growth) must reuse the same holdout, else the gate becomes trainable."""
    fmt = _first_format(mini_dataset)
    samples = _samples(mini_dataset, fmt)
    adm = Admission(min_holdout=3)
    _, holdout1 = adm.split(fmt, samples)
    ids1 = {s.doc_id for s in holdout1}

    grown = samples + [PoolSample(doc_id="new_doc", fingerprint=fmt,
                                  text_layout=samples[0].text_layout, fields=samples[0].fields)]
    train2, holdout2 = adm.split(fmt, grown)
    assert {s.doc_id for s in holdout2} == ids1               # identical holdout
    assert "new_doc" in {s.doc_id for s in train2}            # new arrival -> train only


def test_good_code_passes_gate(mini_dataset):
    fmt = _first_format(mini_dataset)
    adm = Admission(min_holdout=3, f1_threshold=0.95)
    _, holdout = adm.split(fmt, _samples(mini_dataset, fmt))
    report = adm.evaluate(GENERIC_PARSER, holdout)
    assert report.passed and report.holdout_f1 == 1.0


def test_memorization_overfit_is_rejected(mini_dataset):
    fmt = _first_format(mini_dataset)
    adm = Admission(min_holdout=3, f1_threshold=0.95)
    train, holdout = adm.split(fmt, _samples(mini_dataset, fmt))
    compact = [{"full_text": s.text_layout["full_text"], "fields": s.fields} for s in train]
    overfit = build_overfit(compact)
    report = adm.evaluate(overfit, holdout)
    assert not report.passed                                 # wrong invoice/date on unseen docs
    assert report.holdout_f1 < 0.95
