"""The three verification fixes that came out of the first live run's failure analysis.

The live run admitted skills that were silently wrong on exactly the two fields with no
cross-field rule (`invoice_number`, `line_item_count`). Each test here pins one countermeasure:

  A  a structural cross-check derived from the LAYOUT, so `line_item_count` is constrained by
     evidence the extractor did not produce.
  B  identifier comparison that ignores presentational sigils, so a verbatim-copying extractor
     is not scored wrong for a rendered '#'.
  C  a holdout stratified by document complexity, so a 3-document gate has power over the
     structural variation that defeated the random split.
  6  a field-overlap cross-check, after measuring that the ONE field genuinely contaminating the
     pool (vendor_name, 7/98 docs) was swallowing the invoice number off a shared header line.
"""

from __future__ import annotations

from prove.admission import Admission
from prove.sample_pool import PoolSample
from prove.schemas import ExtractionResult
from prove.validator import count_item_rows, field_match, validate


def _layout(n_items: int, extra_lines: list[str] | None = None) -> dict:
    lines = [{"text": f"Item{j} 2 10.00 20.00", "top": 200.0 + j * 12, "x0": 50.0}
             for j in range(n_items)]
    for i, t in enumerate(extra_lines or []):
        lines.insert(0, {"text": t, "top": 50.0 + i * 12, "x0": 50.0})
    return {"schema_version": 1, "page_width": 600.0, "page_height": 800.0,
            "words": [], "lines": lines, "full_text": "\n".join(ln["text"] for ln in lines)}


# --------------------------------------------------------------------------- A


def test_item_rows_counted_from_layout():
    assert count_item_rows(_layout(5)) == 5
    assert count_item_rows(_layout(1)) == 1


def test_totals_and_header_lines_are_not_item_rows():
    # a totals line carries ONE money token, a header/address line none — only an item row
    # (unit price AND amount) carries two. This is what makes the count structural.
    layout = _layout(3, extra_lines=[
        "8 Raffles Quay, Singapore Date: 07 Jan 2024",   # 3 digit tokens, 0 money tokens
        "Subtotal 4425.54",
        "Tax 8.25% 365.11",
        "Total Due 4790.65",
    ])
    assert count_item_rows(layout) == 3


def test_layout_without_item_rows_declines_to_judge():
    # returning 0 would assert "this document has no line items"; None says "no evidence".
    assert count_item_rows({"lines": [{"text": "Thank you", "top": 10.0, "x0": 5.0}]}) is None


def test_miscounted_line_items_now_fail_validation():
    fields = {"vendor_name": "Acme", "invoice_number": "ACM-2024-00001",
              "invoice_date": "2024-03-01", "currency": "USD",
              "subtotal": "100.00", "tax": "7.00", "total": "107.00",
              "line_item_count": "3"}                      # layout actually has 5
    result = ExtractionResult(doc_id="d", fields=fields, source="skill")
    verdict = validate(result, text_layout=_layout(5))
    assert not verdict.passed
    assert "line_item_count_mismatch" in verdict.rule_failures


def test_correct_line_item_count_passes():
    fields = {"vendor_name": "Acme", "invoice_number": "ACM-2024-00001",
              "invoice_date": "2024-03-01", "currency": "USD",
              "subtotal": "100.00", "tax": "7.00", "total": "107.00",
              "line_item_count": "5"}
    verdict = validate(ExtractionResult(doc_id="d", fields=fields, source="skill"),
                       text_layout=_layout(5))
    assert verdict.passed, verdict.rule_failures


def test_structural_rule_is_silent_without_a_layout():
    # production callers that have no layout must not acquire a new failure mode.
    fields = {"vendor_name": "Acme", "invoice_number": "ACM-2024-00001",
              "invoice_date": "2024-03-01", "currency": "USD",
              "subtotal": "100.00", "tax": "7.00", "total": "107.00",
              "line_item_count": "3"}
    assert validate(ExtractionResult(doc_id="d", fields=fields, source="skill")).passed


# --------------------------------------------------------------------------- B


def test_rendered_sigil_does_not_make_an_identifier_wrong():
    # the live failure: banner.html renders '#WAY-2024-90203', ground truth omits the '#',
    # and the extractor was instructed to copy verbatim.
    assert field_match("invoice_number", "#WAY-2024-90203", "WAY-2024-90203")


def test_identifier_canonicalization_still_distinguishes_real_differences():
    assert not field_match("invoice_number", "WAY-2024-90204", "WAY-2024-90203")


def test_canonicalization_is_scoped_to_identifiers():
    # a leading '#' on a vendor name is not presentational — don't launder other fields.
    assert not field_match("vendor_name", "#Acme", "Acme")


# --------------------------------------------------------------------------- C


def _sample(i: int, n_items: int) -> PoolSample:
    return PoolSample(doc_id=f"d{i:03d}", fingerprint="fp", text_layout=_layout(n_items),
                      fields={"line_item_count": str(n_items)})


def test_holdout_spans_complexity_instead_of_sampling_one_point():
    # 9 simple documents, 3 complex ones: a uniform 3-document draw very likely misses the
    # complex ones entirely, which is how the live generalization defect passed the gate.
    samples = [_sample(i, 1 if i < 5 else 2) for i in range(9)] + [_sample(i, 5) for i in range(9, 12)]
    _, holdout = Admission(holdout_frac=0.3, min_holdout=3).split("fp", samples)
    levels = {count_item_rows(s.text_layout) for s in holdout}
    assert len(levels) >= 3, f"holdout collapsed onto {levels}"
    assert 5 in levels, "the complex stratum must be probed"


def test_holdout_stays_frozen_across_retries():
    samples = [_sample(i, 1 + i % 4) for i in range(12)]
    adm = Admission(holdout_frac=0.3, min_holdout=3)
    first = {s.doc_id for s in adm.split("fp", samples)[1]}
    later = {s.doc_id for s in adm.split("fp", samples + [_sample(99, 2)])[1]}
    assert first == later, "a re-chosen holdout would let synthesis train on it"


def test_train_and_holdout_are_disjoint():
    samples = [_sample(i, 1 + i % 4) for i in range(12)]
    train, holdout = Admission(holdout_frac=0.3, min_holdout=3).split("fp", samples)
    assert not ({s.doc_id for s in train} & {s.doc_id for s in holdout})


# --------------------------------------------------------------------------- rule 6


_BASE = {"vendor_name": "Nakatomi Trading Co", "invoice_number": "NAK-2024-22337",
         "invoice_date": "2024-03-01", "currency": "USD",
         "subtotal": "100.00", "tax": "7.00", "total": "107.00", "line_item_count": "2"}


def test_field_that_swallowed_another_field_is_rejected():
    # measured on real qwen-turbo output: a two-column header renders invoice number and vendor
    # on one line, and the extractor returned the whole line as vendor_name. Well-formed,
    # non-empty, and wrong — no form-level rule can see it.
    fields = dict(_BASE, vendor_name="NAK-2024-22337 Nakatomi Trading Co")
    verdict = validate(ExtractionResult(doc_id="d", fields=fields, source="llm"))
    assert not verdict.passed
    assert any(f.startswith("field_overlap") for f in verdict.rule_failures)


def test_clean_fields_do_not_trip_the_overlap_rule():
    assert validate(ExtractionResult(doc_id="d", fields=dict(_BASE), source="llm")).passed


def test_money_fields_are_exempt_from_the_overlap_rule():
    # "120.00" legitimately contains "20.00"; applying containment to money would fail every
    # invoice whose total happens to embed its tax.
    fields = dict(_BASE, subtotal="100.00", tax="20.00", total="120.00")
    verdict = validate(ExtractionResult(doc_id="d", fields=fields, source="llm"))
    assert not any(f.startswith("field_overlap") for f in verdict.rule_failures)


def test_short_values_do_not_trip_containment_by_coincidence():
    fields = dict(_BASE, currency="USD", vendor_name="USD Holdings Ltd")
    verdict = validate(ExtractionResult(doc_id="d", fields=fields, source="llm"))
    assert not any(f.startswith("field_overlap") for f in verdict.rule_failures)
