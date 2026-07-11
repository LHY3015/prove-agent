"""Router: format fingerprint extraction + exact matching (Hard Design Rule 2 — no
format-detection logic ever lives inside a skill; discrimination is centralised here).

Fingerprint design. A fingerprint must be identical across documents of the SAME format
(so exact-match works) yet differ across formats. The only things stable within a format
are the static header labels + vendor identity and their positions; item descriptions,
values, and row counts vary. So we:
  1. restrict to the header region — everything above the first line that looks like an
     item row (>=3 digit-bearing tokens), which excludes the variable table body/totals;
  2. keep only static tokens (drop digit-bearing tokens and month names → drops invoice
     numbers, dates, amounts, street numbers);
  3. tag each kept token with a coarse (x,y) position bucket.

Confidence is the Jaccard similarity to the best-matching known fingerprint (1.0 = exact).
Graded (not binary) so the attribution module can later tell a near-miss from a clean hit.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Optional

Fingerprint = frozenset  # frozenset[tuple[str, int, int]]  (token, x_bucket, y_bucket)

_GRID_COLS = 12
_GRID_ROWS = 24
_EXACT_THRESHOLD = 0.999

_MONTHS = {
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    "january", "february", "march", "april", "june", "july", "august", "september",
    "october", "november", "december",
}

_DIGIT_RE = re.compile(r"\d")


def _has_digit(token: str) -> bool:
    return bool(_DIGIT_RE.search(token))


def _normalize(token: str) -> Optional[str]:
    """Lowercase, strip surrounding punctuation; drop if it carries a digit or is a month
    name (both are per-document values, not stable structure)."""
    t = token.strip().strip(".,:;#()%/").lower()
    if not t or _has_digit(t) or t in _MONTHS:
        return None
    return t


def _digit_tokens(text: str) -> int:
    return sum(1 for tok in text.split() if _has_digit(tok))


def _header_cutoff(text_layout: dict[str, Any]) -> float:
    """Top-coordinate where the items table begins; everything strictly above is the
    stable header region.

    The first item row is the first line with >=3 digit-bearing tokens (qty/price/amount).
    A table's column-header row (e.g. "Item Qty Rate ...") sits directly above it with 0
    digits, and its token x-positions are content-driven (auto column widths) — unstable —
    so we cut above it too when present. Templates without a column header (receipt) have a
    digit-bearing meta line above the items and cut at the item row itself."""
    lines = sorted(text_layout.get("lines", []), key=lambda ln: ln["top"])
    for i, line in enumerate(lines):
        if _digit_tokens(line["text"]) >= 3:
            if i > 0 and _digit_tokens(lines[i - 1]["text"]) == 0:
                return float(lines[i - 1]["top"])
            return float(line["top"])
    return float("inf")


def compute_fingerprint(text_layout: dict[str, Any]) -> Fingerprint:
    cutoff = _header_cutoff(text_layout)
    pw = float(text_layout.get("page_width", 1.0)) or 1.0
    ph = float(text_layout.get("page_height", 1.0)) or 1.0
    col_w = pw / _GRID_COLS
    row_h = ph / _GRID_ROWS

    features: set[tuple[str, int, int]] = set()
    for w in text_layout.get("words", []):
        if float(w["top"]) >= cutoff:
            continue
        tok = _normalize(w["text"])
        if tok is None:
            continue
        xb = int(float(w["x0"]) / col_w)
        yb = int(float(w["top"]) / row_h)
        features.add((tok, xb, yb))
    return frozenset(features)


def fingerprint_hash(fp: Fingerprint) -> str:
    """Stable short hash for storage/debug."""
    payload = "|".join(sorted(f"{t}:{x}:{y}" for t, x, y in fp))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _jaccard(a: Fingerprint, b: Fingerprint) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


class Router:
    """Holds known format fingerprints; exact-matches an incoming document against them.

    In A0 (baseline) no fingerprints are registered → every document is a miss → LLM
    fallback. Fingerprints get registered when a skill is admitted for a format (Phase 2)."""

    def __init__(self) -> None:
        self._known: dict[str, Fingerprint] = {}

    def register(self, format_id: str, text_layout: dict[str, Any]) -> Fingerprint:
        fp = compute_fingerprint(text_layout)
        self._known[format_id] = fp
        return fp

    def register_fingerprint(self, format_id: str, fp: Fingerprint) -> None:
        self._known[format_id] = fp

    @property
    def known_formats(self) -> list[str]:
        return list(self._known)

    def match(self, fp: Fingerprint) -> tuple[Optional[str], float, str]:
        """Match a precomputed fingerprint. Return (format_id | None, confidence, method);
        method ∈ {exact, fuzzy, miss} — fuzzy is reserved for the Phase-3 mem0 fallback."""
        if not self._known:
            return None, 0.0, "miss"
        best_id, best_sim = None, 0.0
        for fmt, known in self._known.items():
            sim = _jaccard(fp, known)
            if sim > best_sim:
                best_id, best_sim = fmt, sim
        if best_sim >= _EXACT_THRESHOLD:
            return best_id, best_sim, "exact"
        return None, best_sim, "miss"

    def route(self, text_layout: dict[str, Any]) -> tuple[Optional[str], float, str]:
        return self.match(compute_fingerprint(text_layout))
