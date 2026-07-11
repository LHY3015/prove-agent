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

from typing import Any

import pdfplumber

TEXT_LAYOUT_SCHEMA_VERSION = 1

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
