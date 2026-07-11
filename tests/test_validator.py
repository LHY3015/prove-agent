"""Validator rule tests — crafted pass/fail documents for each deterministic rule."""

from prove.schemas import ExtractionResult, GroundTruth
from prove.validator import field_f1, validate

_VALID = {
    "vendor_name": "Acme Corporation",
    "invoice_number": "ACM-2024-00001",
    "invoice_date": "2024-03-15",
    "currency": "USD",
    "subtotal": "100.00",
    "tax": "7.00",
    "total": "107.00",
    "line_item_count": "2",
}


def _result(**overrides):
    fields = dict(_VALID)
    fields.update(overrides)
    return ExtractionResult(doc_id="d", fields=fields, source="llm")


def test_valid_document_passes():
    v = validate(_result())
    assert v.passed
    assert v.rule_failures == []


def test_money_arithmetic_failure():
    v = validate(_result(total="200.00"))
    assert not v.passed
    assert "money_arithmetic" in v.rule_failures


def test_money_arithmetic_within_tolerance_passes():
    v = validate(_result(subtotal="100.00", tax="7.005", total="107.00"))
    assert v.passed


def test_money_unparseable():
    v = validate(_result(subtotal="not-money"))
    assert "money_unparseable" in v.rule_failures


def test_currency_whitelist():
    v = validate(_result(currency="XYZ"))
    assert "currency_invalid" in v.rule_failures


def test_date_unparseable():
    v = validate(_result(invoice_date="15th of March"))
    assert "date_unparseable" in v.rule_failures


def test_date_out_of_range():
    v = validate(_result(invoice_date="1850-01-01"))
    assert "date_out_of_range" in v.rule_failures


def test_alternate_date_formats_parse():
    for d in ["15/03/2024", "Mar 15, 2024", "15 Mar 2024", "15.03.2024"]:
        v = validate(_result(invoice_date=d))
        assert "date_unparseable" not in v.rule_failures


def test_missing_required_field():
    v = validate(_result(invoice_number=""))
    assert "missing_field:invoice_number" in v.rule_failures


def test_line_item_count_invalid():
    assert "line_item_count_invalid" in validate(_result(line_item_count="0")).rule_failures
    assert "line_item_count_invalid" in validate(_result(line_item_count="x")).rule_failures


def test_field_diffs_and_f1_in_eval_mode():
    gt = GroundTruth(doc_id="d", fields=dict(_VALID))
    v = validate(_result(vendor_name="Wrong Vendor"), ground_truth=gt)
    assert v.field_diffs is not None
    assert v.field_diffs["vendor_name"] is False
    assert v.field_diffs["invoice_number"] is True
    assert field_f1(v.field_diffs) == 7 / 8


def test_no_field_diffs_in_production_mode():
    v = validate(_result())
    assert v.field_diffs is None
