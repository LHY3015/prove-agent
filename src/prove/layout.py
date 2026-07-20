"""PDF -> text_layout extraction (pdfplumber).

Runs ONCE at ingestion. The resulting dict is what the router fingerprints, the
extraction agent reads, and (Phase 2) skills parse — skills never see the raw PDF,
so they need no file I/O (Hard Design Rule: sandbox import whitelist).

text_layout schema (this is the skill ABI — once a skill is admitted it parses this shape,
so the schema is frozen and versioned; additive changes only, bump the version otherwise):
    {
      "schema_version": int,
      "page_width": float, "page_height": float,
      "words": [{"text": str, "x0","top","x1","bottom": float}, ...],   # sorted top,x0
      "lines": [{"text": str, "top": float, "x0": float}, ...],         # words grouped by row
      "full_text": str,
    }
"""

from __future__ import annotations

import re
from typing import Any

import pdfplumber

TEXT_LAYOUT_SCHEMA_VERSION = 1

# characters a text extractor should never emit from a legible document; their presence marks a
# token as unreadable. Kept deliberately narrow (control/replacement/box glyphs), so ordinary
# punctuation and currency symbols never count as damage.
_JUNK_RE = re.compile(r"[^\w\s.,:;/()%\-+&#'\"@$£€*]|�")

# words whose top-coordinates differ by less than this are treated as the same row
_ROW_TOLERANCE = 3.0


def _group_lines(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Cluster words into rows by their `top` coordinate, left-to-right within a row."""
    lines: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    current_top: float | None = None
    for w in sorted(words, key=lambda w: (round(w["top"], 1), w["x0"])):
        if current_top is None or abs(w["top"] - current_top) <= _ROW_TOLERANCE:
            current.append(w)
            current_top = w["top"] if current_top is None else current_top
        else:
            lines.append(_finish_line(current))
            current = [w]
            current_top = w["top"]
    if current:
        lines.append(_finish_line(current))
    return lines


def _finish_line(words: list[dict[str, Any]]) -> dict[str, Any]:
    words = sorted(words, key=lambda w: w["x0"])
    return {
        "text": " ".join(w["text"] for w in words),
        "top": round(min(w["top"] for w in words), 2),
        "x0": round(min(w["x0"] for w in words), 2),
    }


def input_integrity(text_layout: dict[str, Any]) -> float:
    """Fraction of tokens that are character-class clean — a deterministic, ground-truth-free
    measure of how readable the extracted text is, computed at ingestion.

    Scope (stated, not hidden): this detects the *unreadable-region* noise model — an OCR engine
    emitting junk glyphs where the page is smudged or occluded. It does NOT detect plausible
    charset confusions (0/O, 1/l, rn/m), which produce clean-looking tokens; those remain
    out of scope, as does any judgement of whether a token is the *right* one. Character class
    only — never semantics, so this stays a measurement and not a quality verdict.

    Born-digital PDFs score 1.0, so the threshold that consumes this sits on a wide margin.

    Two calibration limits, both stated because they point in opposite directions:
      - This is a WHOLE-DOCUMENT fraction, so a small unreadable region on a text-dense page
        dilutes toward 1.0 and is missed. That under-detection fails toward charging the skill
        (the behaviour that existed before this signal), never toward false exoneration.
      - The threshold is validated against the synthetic damage model only. Real OCR typography
        can depress the score without any unreadability: 3 of 100 real CORD receipts scored
        below 0.95 (min 0.8889, p05 0.9574, median 1.0000). None were skill-served, so no peel
        fired — the real-text false-positive rate is UNMEASURED, and a false peel exonerates,
        which is the dangerous direction. Recalibrate before any real-data attribution claim.
    """
    words = text_layout.get("words", [])
    if not words:
        return 1.0
    clean = sum(1 for w in words if not _JUNK_RE.search(str(w.get("text", ""))))
    return round(clean / len(words), 4)


def extract_layout(pdf_path: str) -> dict[str, Any]:
    """Extract a text_layout dict from the first page of a born-digital PDF."""
    with pdfplumber.open(pdf_path) as pdf:
        page = pdf.pages[0]
        raw = page.extract_words(use_text_flow=False, keep_blank_chars=False)
        words = [
            {
                "text": w["text"],
                "x0": round(float(w["x0"]), 2),
                "top": round(float(w["top"]), 2),
                "x1": round(float(w["x1"]), 2),
                "bottom": round(float(w["bottom"]), 2),
            }
            for w in raw
        ]
        words.sort(key=lambda w: (w["top"], w["x0"]))
        lines = _group_lines(words)
        return {
            "schema_version": TEXT_LAYOUT_SCHEMA_VERSION,
            "page_width": round(float(page.width), 2),
            "page_height": round(float(page.height), 2),
            "words": words,
            "lines": lines,
            "full_text": "\n".join(line["text"] for line in lines),
        }
