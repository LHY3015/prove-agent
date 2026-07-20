"""Deterministic validation rule engine.

This is the system's judge in Phase 1: its *pass* verdicts are what let a sample join the
verified pool, and that pool is the foundation of the whole trust chain. No LLM is involved
in a verdict (Hard Design Rule 1).

Production mode uses rules only. Eval mode (ground truth available) additionally records
field-level exact-match — used for F1 in the ablations and for admission (Phase 2). The
field-match is diagnostic; it never overrides the rule verdict.
"""

from __future__ import annotations

import datetime
import re
from typing import Optional

from .schemas import (
    CURRENCY_WHITELIST,
    FIELD_SPECS,
    TARGET_FIELDS,
    ExtractionResult,
    GroundTruth,
    ValidationVerdict,
)

_MONEY_TOLERANCE = 0.01
# shortest value worth testing for containment; below this, coincidental overlap dominates.
_MIN_CONTAINMENT_LEN = 6
_MIN_YEAR, _MAX_YEAR = 2000, 2035

# date styles emitted by the generator (kept in sync with datagen.FormatSpec.date_style)
_DATE_FORMATS = [
    "%Y-%m-%d", "%d %b %Y", "%b %d, %Y", "%d.%m.%Y",
    "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%b %d %Y",
]

_MONEY_CLEAN_RE = re.compile(r"[,\s$£€]")

# a money-formatted token: exactly two decimal places, optional thousands separators.
_MONEY_TOKEN_RE = re.compile(r"^\d+(?:,\d{3})*\.\d{2}$")


def count_item_rows(text_layout: dict) -> Optional[int]:
    """Count the document's line items from its LAYOUT, independently of any extractor.

    An item row is the only line carrying two money-formatted tokens (unit price and amount);
    totals lines carry one, and header/address lines carry none. Verified exact on all 14
    synthetic formats.

    This exists to give `line_item_count` a genuine cross-check. Rules 1-4 only constrain a
    field's *form* — "is a positive integer" cannot tell 3 from 5 — so a skill that miscounts
    produces a valid-looking wrong answer that no rule contradicts. Deriving the count from the
    document structure is an independent signal, which is exactly what a self-supervised pool
    cannot supply: if the extractor is systematically wrong, the pool agrees with it, and only
    evidence from outside the extractor can break the tie.

    Returns None when the layout carries no recognisable item row, so the caller can decline to
    judge rather than assert a count of zero.
    """
    lines = text_layout.get("lines") or []
    n = sum(
        1 for ln in lines
        if sum(1 for t in str(ln.get("text", "")).split() if _MONEY_TOKEN_RE.match(t)) >= 2
    )
    return n or None


def _parse_money(value: str) -> Optional[float]:
    if not value:
        return None
    cleaned = _MONEY_CLEAN_RE.sub("", value)
    try:
        return float(cleaned)
    except ValueError:
        return None


_SIGIL_RE = re.compile(r"^[#№\s:.\-]+|[\s:.\-]+$")


def _strip_sigils(value: str) -> str:
    """Drop presentational decoration around an identifier, keeping the identity itself."""
    return _SIGIL_RE.sub("", str(value).strip())


def _parse_date(value: str) -> Optional[datetime.date]:
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def validate(
    result: ExtractionResult,
    ground_truth: Optional[GroundTruth] = None,
    *,
    profile: Optional[list[str]] = None,
    text_layout: Optional[dict] = None,
) -> ValidationVerdict:
    """`profile` narrows the field schema to those a dataset actually labels (the real-data
    adapters supply one). Rules 2-5 are already conditional on a field being present, so a
    profile only changes which fields are REQUIRED and which are scored — the surviving rules
    are unchanged in strength. Narrowing the profile genuinely weakens the verifier, and with it
    the trust the pool confers: state the profile whenever reporting numbers from one."""
    fields = result.fields
    target = profile or TARGET_FIELDS
    failures: list[str] = []

    # 1. required fields present
    for f in target:
        if not str(fields.get(f, "")).strip():
            failures.append(f"missing_field:{f}")

    # 2. currency whitelist
    currency = str(fields.get("currency", "")).strip().upper()
    if currency and currency not in CURRENCY_WHITELIST:
        failures.append("currency_invalid")

    # 3. date parseable + sane range
    date_val = _parse_date(str(fields.get("invoice_date", "")))
    if fields.get("invoice_date"):
        if date_val is None:
            failures.append("date_unparseable")
        elif not (_MIN_YEAR <= date_val.year <= _MAX_YEAR):
            failures.append("date_out_of_range")

    # 4. line_item_count is a positive integer, and — when the layout is available — actually
    # matches the number of item rows on the page. The form check alone cannot tell 3 from 5.
    lic = str(fields.get("line_item_count", "")).strip()
    if lic:
        if not lic.isdigit() or int(lic) < 1:
            failures.append("line_item_count_invalid")
        elif text_layout is not None:
            observed = count_item_rows(text_layout)
            if observed is not None and int(lic) != observed:
                failures.append("line_item_count_mismatch")

    # 5. money arithmetic: subtotal + tax == total (within tolerance)
    subtotal = _parse_money(str(fields.get("subtotal", "")))
    tax = _parse_money(str(fields.get("tax", "")))
    total = _parse_money(str(fields.get("total", "")))
    if None in (subtotal, tax, total):
        if any(fields.get(k) for k in ("subtotal", "tax", "total")):
            failures.append("money_unparseable")
    elif abs((subtotal + tax) - total) > _MONEY_TOLERANCE:
        failures.append("money_arithmetic")

    # 6. distinct fields must not swallow one another. When two fields share a rendered line
    # ("NAK-2024-22337  Nakatomi Trading Co" in a two-column header) an extractor can return the
    # whole line for one of them. The result is well-formed, non-empty, and wrong — invisible to
    # every form-level rule. This is a genuine CROSS-FIELD check and it costs nothing, which is
    # the point: the answer to "this field has no constraint" is to write one, not to buy a
    # second opinion from another model.
    # Money fields are excluded: "120.00" legitimately contains "20.00".
    _textual = [f for f in target if _DTYPE.get(f, "str") in ("str", "identifier")]
    for a in _textual:
        va = str(fields.get(a, "")).strip()
        if len(va) < _MIN_CONTAINMENT_LEN:
            continue
        for b in _textual:
            if a == b:
                continue
            vb = str(fields.get(b, "")).strip()
            if len(vb) >= _MIN_CONTAINMENT_LEN and va != vb and va in vb:
                failures.append(f"field_overlap:{b}_contains_{a}")

    field_diffs = None
    if ground_truth is not None:
        # semantic comparison, the SAME notion of correctness the admission gate uses
        # (compare_fields/field_match). A raw `==` here meant the eval metric and the admission
        # oracle disagreed about what "correct" means: money/date normalization applied in one
        # place and not the other, and a presentational sigil counted as a wrong field in the
        # metric while being accepted by the gate. One system, one definition.
        field_diffs = compare_fields(fields, ground_truth.fields, profile=target)

    return ValidationVerdict(
        doc_id=result.doc_id,
        passed=len(failures) == 0,
        rule_failures=failures,
        field_diffs=field_diffs,
    )


_DTYPE = {f.name: f.dtype for f in FIELD_SPECS}


def field_match(name: str, predicted: str, expected: str) -> bool:
    """Semantic per-field equality used by synthesis self-repair and the admission gate.
    Money/date/int fields are compared by parsed value so a live skill returning "1,234.00"
    where the pool captured "1234.00" is not spuriously rejected; other fields compare exactly."""
    p, e = str(predicted), str(expected)
    dtype = _DTYPE.get(name, "str")
    if dtype == "identifier":
        # documents decorate identifiers with sigils ("#WAY-2024-90203", "No. INV-7") that are
        # presentation, not identity. Comparing raw made a verbatim-copying extractor look wrong
        # and — because the pool then encoded the sigil — taught skills to reproduce it.
        return _strip_sigils(p) == _strip_sigils(e)
    if dtype == "money":
        pm, em = _parse_money(p), _parse_money(e)
        return pm is not None and em is not None and abs(pm - em) <= _MONEY_TOLERANCE
    if dtype == "date":
        pd, ed = _parse_date(p), _parse_date(e)
        return pd is not None and pd == ed
    if dtype == "int":
        return p.strip().isdigit() and e.strip().isdigit() and int(p) == int(e)
    return p == e


def compare_fields(
    predicted: dict[str, str],
    expected: dict[str, str],
    *,
    profile: Optional[list[str]] = None,
) -> dict[str, bool]:
    """Per-field match map over the target schema (True = matches expected). `profile` narrows
    it to a dataset's labelled fields, so the admission gate is not scored against fields the
    dataset never labels."""
    return {
        f: field_match(f, predicted.get(f, ""), expected.get(f, ""))
        for f in (profile or TARGET_FIELDS)
    }


def field_f1(field_diffs: dict[str, bool]) -> float:
    """Micro-F1 over fields. With a fixed field schema every field is always predicted and
    always expected, so precision == recall == accuracy; we report it as F1 for continuity
    with the admission gate and the ablation curves."""
    if not field_diffs:
        return 0.0
    correct = sum(1 for v in field_diffs.values() if v)
    return correct / len(field_diffs)
