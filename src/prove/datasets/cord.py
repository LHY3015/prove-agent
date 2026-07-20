"""CORD-v2 adapter: real receipts -> the `text_layout` skill ABI.

CORD (Consolidated Receipt Dataset, `naver-clova-ix/cord-v2`) ships OCR word boxes together
with field labels, so no OCR engine is needed here — the dataset's own annotations are
converted into exactly the dict `layout.extract_layout` produces for a born-digital PDF. Every
downstream component (router, skills, validator) is therefore unmodified on real data; only the
ingestion source changes.

The record shape below is **verified against a real CORD-v2 download** (100 test records,
2026-07-20); the dataset is public and ungated (CC-BY-4.0), so no credentials are needed. The
committed test fixture stays hand-authored to avoid redistributing dataset rows.

Why CORD rather than SROIE: CORD labels `sub_total.subtotal_price`, `sub_total.tax_price` and
`total.total_price`, so the validator's strongest rule — the money-arithmetic cross-check that
makes a *pass* verdict mean something — survives on real data. SROIE labels only
company/date/address/total, which would reduce the verifier to presence checks and hollow out
the pool's trust guarantee.

WHAT THIS ADAPTER DOES **NOT** CLAIM (read before quoting any number from it):
  - CORD has no vendor/invoice-number/currency/date labels, so `CORD_PROFILE` is a 4-field
    schema. The verifier is genuinely weaker here than on synthetic invoices; any metric from
    this path must be reported alongside the profile.
  - **`CORD_PROFILE` is known to be mis-specified and the run's 0.22 pass rate reflects that,
    not the pipeline.** Measured over 100 real test receipts with extraction 100% correct:
    `missing_field:tax` fires on 57 and `missing_field:subtotal` on 35, because real receipts
    mostly carry no subtotal/tax line — requiring them rejects correct extractions, so the
    verified pool could never fill on this dataset. A further 59 `money_unparseable` are a
    knock-on: the cross-field money rule reports "unparseable" for fields that are merely
    absent rather than malformed. Narrowing the required set (and firing the money rule only
    when its inputs are present) was proposed and declined; until that lands, read the pass
    rate as a statement about this profile.
  - The 15 `money_arithmetic` failures in that same run are NOT a defect of the profile: CORD
    labels a `sub_total.discount_price` category, and real receipts carry discounts, service
    charges and rounding, so such a receipt is internally consistent under the FULL accounting
    identity while failing the rule's narrower one (subtotal + tax == total). The strongest
    synthetic rule does not transfer unmodified to real receipts; extending the identity with
    the terms CORD already labels is roadmap.
  - The router's fingerprint was designed for born-digital PDFs. Real scans carry OCR jitter and
    varying crop/skew, so same-vendor receipts are NOT expected to reach the 0.999 exact-match
    threshold. Expect few or no recall hits: the router fails CLOSED, traffic falls back to the
    LLM, and the run demonstrates the SAFETY claim (nothing unsafe is admitted, cost is bounded
    by the A0 baseline) — not the recurrence claim. Making recurrence work on scans needs a
    fuzzy router with normalized coordinates; that is roadmap, not this adapter.
  - Offline (no `--live`) the loop can only reach the LLM-fallback path, because the simulated
    synthesizer in `evals/fake_skills` has no hand-written skill for a real format. The full
    synthesize->admit->serve cycle on CORD requires `--live`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator, Optional

from ..layout import TEXT_LAYOUT_SCHEMA_VERSION, _group_lines
from ..schemas import Document, GroundTruth

# CORD labels these four of the project's eight target fields. `line_item_count` is derived from
# the menu-group count rather than read from a label.
CORD_PROFILE: list[str] = ["subtotal", "tax", "total", "line_item_count"]

# CORD category -> project field. Categories are the `valid_line[].category` strings.
_CATEGORY_MAP = {
    "sub_total.subtotal_price": "subtotal",
    "sub_total.tax_price": "tax",
    "total.total_price": "total",
}


def _quad_to_box(quad: dict[str, Any]) -> tuple[float, float, float, float]:
    """CORD stores each word as a quadrilateral (x1..x4, y1..y4, clockwise from top-left).
    The ABI wants an axis-aligned box, so take the extent."""
    xs = [float(quad[f"x{i}"]) for i in (1, 2, 3, 4)]
    ys = [float(quad[f"y{i}"]) for i in (1, 2, 3, 4)]
    return min(xs), min(ys), max(xs), max(ys)


def _clean_price(text: str) -> str:
    """CORD price strings carry thousands separators and stray spaces ('12.000', '1, 500')."""
    return text.replace(" ", "").strip()


def to_text_layout(ground_truth: dict[str, Any]) -> dict[str, Any]:
    """Build the `text_layout` ABI dict from one CORD `ground_truth` record."""
    words: list[dict[str, Any]] = []
    for line in ground_truth.get("valid_line", []):
        for w in line.get("words", []):
            text = str(w.get("text", "")).strip()
            if not text:
                continue
            x0, top, x1, bottom = _quad_to_box(w["quad"])
            words.append({"text": text, "x0": round(x0, 2), "top": round(top, 2),
                          "x1": round(x1, 2), "bottom": round(bottom, 2)})

    words.sort(key=lambda w: (w["top"], w["x0"]))
    lines = _group_lines(words)
    # CORD carries no page box; the content extent stands in for it, which is what the router's
    # positional bucketing actually needs (it bucketises relative to the page dimensions).
    return {
        "schema_version": TEXT_LAYOUT_SCHEMA_VERSION,
        "page_width": round(max((w["x1"] for w in words), default=1.0), 2),
        "page_height": round(max((w["bottom"] for w in words), default=1.0), 2),
        "words": words,
        "lines": lines,
        "full_text": "\n".join(ln["text"] for ln in lines),
    }


def to_fields(ground_truth: dict[str, Any]) -> dict[str, str]:
    """Extract the CORD_PROFILE fields from one CORD record.

    Values are read from `gt_parse` (the dataset's normalized parse) rather than re-derived from
    the word boxes, so the ground truth here is the dataset's, not ours.
    """
    parse = ground_truth.get("gt_parse", {})
    fields: dict[str, str] = {}

    sub_total = parse.get("sub_total", {}) or {}
    if "subtotal_price" in sub_total:
        fields["subtotal"] = _clean_price(str(sub_total["subtotal_price"]))
    if "tax_price" in sub_total:
        fields["tax"] = _clean_price(str(sub_total["tax_price"]))

    total = parse.get("total", {}) or {}
    if "total_price" in total:
        fields["total"] = _clean_price(str(total["total_price"]))

    menu = parse.get("menu", [])
    if isinstance(menu, dict):      # CORD collapses a single-item menu to a bare object
        menu = [menu]
    if menu:
        fields["line_item_count"] = str(len(menu))

    return fields


def _record_to_doc(doc_id: str, ground_truth: dict[str, Any]) -> tuple[Document, GroundTruth]:
    layout = to_text_layout(ground_truth)
    doc = Document(doc_id=doc_id, format_id_true=None, pdf_path=None, text_layout=layout)
    return doc, GroundTruth(doc_id=doc_id, fields=to_fields(ground_truth))


def load_jsonl(path: str | Path, limit: Optional[int] = None) -> list[tuple[Document, GroundTruth]]:
    """Load CORD records from a JSONL file, one `ground_truth` object per line.

    This is the offline path: `export_jsonl` writes this shape from the HuggingFace dataset, and
    the committed fixture uses it, so tests and full runs exercise the same code.
    """
    out: list[tuple[Document, GroundTruth]] = []
    with open(path, "r", encoding="utf-8") as f:
        for i, raw in enumerate(f):
            if not raw.strip():
                continue
            if limit is not None and len(out) >= limit:
                break
            record = json.loads(raw)
            # tolerate both the raw HF row ({"ground_truth": "<json string>"}) and a pre-unwrapped
            # record, since the fixture and an export may differ in nesting.
            gt = record.get("ground_truth", record)
            if isinstance(gt, str):
                gt = json.loads(gt)
            out.append(_record_to_doc(record.get("doc_id", f"cord_{i:05d}"), gt))
    return out


def load_hf(split: str = "test", limit: Optional[int] = None) -> list[tuple[Document, GroundTruth]]:
    """Load directly from HuggingFace (`--live`-style path; needs `datasets` + network).

    Kept separate from `load_jsonl` so the offline path never imports `datasets`.
    """
    from datasets import load_dataset  # imported lazily: not a project dependency

    ds = load_dataset("naver-clova-ix/cord-v2", split=split)
    out: list[tuple[Document, GroundTruth]] = []
    for i, row in enumerate(ds):
        if limit is not None and i >= limit:
            break
        gt = row["ground_truth"]
        if isinstance(gt, str):
            gt = json.loads(gt)
        out.append(_record_to_doc(f"cord_{split}_{i:05d}", gt))
    return out


def export_jsonl(out_path: str | Path, split: str = "test", limit: Optional[int] = None) -> int:
    """Materialize a HuggingFace split to the JSONL shape `load_jsonl` reads, so a machine with
    network access can produce a file that an offline machine replays. Returns the row count."""
    from datasets import load_dataset

    ds = load_dataset("naver-clova-ix/cord-v2", split=split)
    n = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for i, row in enumerate(ds):
            if limit is not None and i >= limit:
                break
            gt = row["ground_truth"]
            f.write(json.dumps({"doc_id": f"cord_{split}_{i:05d}",
                                "ground_truth": json.loads(gt) if isinstance(gt, str) else gt}) + "\n")
            n += 1
    return n


def iter_documents(
    source: str | Path, limit: Optional[int] = None
) -> Iterator[tuple[Document, GroundTruth]]:
    """Uniform entry point: a filesystem path replays JSONL, the literal string 'hf' downloads."""
    if str(source) == "hf":
        yield from load_hf(limit=limit)
    else:
        yield from load_jsonl(source, limit=limit)
