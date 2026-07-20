"""CORD-v2 adapter: annotation records -> the `text_layout` skill ABI.

Runs against `fixtures/cord_schema_replica.jsonl`, a HAND-AUTHORED file in CORD's record shape
with invented values — it exercises the adapter without redistributing the dataset. The shape it
replicates is verified: a real CORD-v2 download (100 test records, 2026-07-20) has the same
`gt_parse` / `valid_line[].{category,group_id,words}` / `words[].{quad,text}` / `quad.x1..x4,y1..y4`
structure and the same `sub_total.subtotal_price` / `sub_total.tax_price` / `total.total_price`
categories this adapter maps. These tests still cover adapter LOGIC, not that download.
"""

from __future__ import annotations

from pathlib import Path

from prove.datasets.cord import (
    CORD_PROFILE,
    load_jsonl,
    to_fields,
    to_text_layout,
)
from prove.layout import input_integrity
from prove.router import compute_fingerprint
from prove.schemas import ExtractionResult
from prove.validator import validate

FIXTURE = Path(__file__).parent / "fixtures" / "cord_schema_replica.jsonl"


def test_records_load_into_the_abi_shape():
    docs = load_jsonl(FIXTURE)
    assert len(docs) == 3
    for doc, _ in docs:
        layout = doc.text_layout
        assert set(layout) >= {"schema_version", "page_width", "page_height",
                               "words", "lines", "full_text"}
        assert layout["words"], "every record must yield words"
        # the ABI contract: words sorted by (top, x0)
        keys = [(w["top"], w["x0"]) for w in layout["words"]]
        assert keys == sorted(keys)


def test_quads_become_axis_aligned_boxes():
    docs = load_jsonl(FIXTURE, limit=1)
    for w in docs[0][0].text_layout["words"]:
        assert w["x1"] > w["x0"] and w["bottom"] > w["top"]


def test_words_on_one_row_group_into_a_line():
    doc, _ = load_jsonl(FIXTURE, limit=1)[0]
    assert "NASI GORENG" in doc.text_layout["full_text"]


def test_fields_map_to_the_cord_profile():
    docs = load_jsonl(FIXTURE)
    fields = docs[0][1].fields
    assert fields == {"subtotal": "33.000", "tax": "3.300",
                      "total": "36.300", "line_item_count": "2"}
    assert set(fields) <= set(CORD_PROFILE)


def test_single_item_menu_collapsed_to_an_object_is_still_counted():
    # CORD emits a bare object rather than a list when a receipt has one menu group.
    _, gt = load_jsonl(FIXTURE)[1]
    assert gt.fields["line_item_count"] == "1"


def test_absent_labels_are_omitted_rather_than_invented():
    # the third record has no sub_total block; the adapter must not fabricate one.
    _, gt = load_jsonl(FIXTURE)[2]
    assert "subtotal" not in gt.fields and "tax" not in gt.fields
    assert gt.fields["total"] == "45.000"


def test_money_arithmetic_rule_survives_on_the_cord_profile():
    """The reason CORD was chosen over SROIE: the validator's strongest rule still applies."""
    _, gt = load_jsonl(FIXTURE)[0]
    ok = validate(ExtractionResult(doc_id="d", fields=dict(gt.fields), source="llm"),
                  profile=CORD_PROFILE)
    assert ok.passed, ok.rule_failures

    bad = dict(gt.fields)
    bad["total"] = "99.999"          # breaks subtotal + tax == total
    verdict = validate(ExtractionResult(doc_id="d", fields=bad, source="llm"),
                       profile=CORD_PROFILE)
    assert not verdict.passed
    assert "money_arithmetic" in verdict.rule_failures


def test_full_target_schema_would_fail_every_cord_doc():
    """Why the profile seam exists: scored against all eight fields, a correctly-parsed CORD
    receipt fails on the four the dataset never labels."""
    _, gt = load_jsonl(FIXTURE)[0]
    verdict = validate(ExtractionResult(doc_id="d", fields=dict(gt.fields), source="llm"))
    assert not verdict.passed
    assert any(f.startswith("missing_field:vendor_name") for f in verdict.rule_failures)


def test_real_documents_are_fingerprintable_and_measured_clean():
    # the adapter must produce layouts the existing router and integrity signal can consume
    # unmodified — no real-data special-casing anywhere downstream.
    for doc, _ in load_jsonl(FIXTURE):
        assert isinstance(compute_fingerprint(doc.text_layout), frozenset)
        assert input_integrity(doc.text_layout) == 1.0


def test_to_text_layout_and_to_fields_are_usable_standalone():
    record = {"gt_parse": {"total": {"total_price": "10.000"}},
              "valid_line": [{"category": "total.total_price", "group_id": 1,
                              "words": [{"quad": {"x1": 1, "y1": 2, "x2": 5, "y2": 2,
                                                  "x3": 5, "y3": 8, "x4": 1, "y4": 8},
                                         "text": "10.000"}]}]}
    assert to_text_layout(record)["full_text"] == "10.000"
    assert to_fields(record) == {"total": "10.000"}
