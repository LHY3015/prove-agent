"""Key-free synthesis double for CI/eval — the fake "synthesis LLM" returns real parser code.

The machinery under test (trigger -> synthesis -> sandbox -> admission -> registry -> routing)
is content-agnostic, so a deterministic canned parser exercises the whole loop without an API
key; `--live` runs carry the actual per-format-code evidence. A single hand-written generic
parser solves all 8 synthetic formats (their field VALUES share formats even though labels
differ) — this reflects the synthetic data's simplicity, not the real task.

Two variants, selected per fingerprint by a schedule so BOTH ablation arms see the identical
synthesis stream (only the admission gate differs between A1 and A2):
  - GENERIC_PARSER: correct on all formats -> passes admission.
  - overfit builder: MEMORIZES the training samples (keyed on raw full_text, never builtin
    hash — salted per process) so it passes the self-repair loop, but on unseen docs returns
    the first training sample's invoice_number + invoice_date (non-empty, valid, WRONG) while
    extracting the rest correctly. Validation PASSES yet those two fields are silently wrong.
    A constructed demonstrator of the silent-failure class — not a claim about live LLM output.
"""

from __future__ import annotations

import json
import re

# --- the generic parser (verified 100% field accuracy across all 8 formats) ---------------
GENERIC_PARSER = r'''
import re

_CURRENCIES = {"USD", "EUR", "GBP", "SGD", "JPY", "CNY"}
_INV_RE = re.compile(r"[A-Z]{2,4}-\d{4}-\d{5}")
_MONEY_RE = re.compile(r"\d+\.\d{2}")
_TITLE_WORDS = {"invoice", "bill", "to", "billto"}
_DATE_PATTERNS = [
    re.compile(r"\d{4}-\d{2}-\d{2}"),
    re.compile(r"\d{4}/\d{2}/\d{2}"),
    re.compile(r"\d{2}/\d{2}/\d{4}"),
    re.compile(r"\d{2}\.\d{2}\.\d{4}"),
    re.compile(r"\d{1,2} [A-Z][a-z]{2} \d{4}"),
    re.compile(r"[A-Z][a-z]{2} \d{1,2}, \d{4}"),
    re.compile(r"[A-Z][a-z]{2} \d{1,2} \d{4}"),
]


def _last_money(line):
    m = _MONEY_RE.findall(line)
    return m[-1] if m else ""


def extract(text_layout):
    lines = [ln["text"] for ln in text_layout.get("lines", [])]
    full = text_layout.get("full_text", "")
    out = {k: "" for k in (
        "vendor_name", "invoice_number", "invoice_date", "currency",
        "subtotal", "tax", "total", "line_item_count")}

    m = _INV_RE.search(full)
    if m:
        out["invoice_number"] = m.group(0)

    for tok in re.findall(r"[A-Z]{3}", full):
        if tok in _CURRENCIES:
            out["currency"] = tok
            break

    for pat in _DATE_PATTERNS:
        m = pat.search(full)
        if m:
            out["invoice_date"] = m.group(0)
            break

    for ln in lines:
        toks = ln.split()
        if not toks or all(len(t) == 1 for t in toks):
            continue
        vend = []
        for t in toks:
            if t.lower().rstrip(":") in _TITLE_WORDS or t.endswith(":"):
                break
            if any(c.isdigit() for c in t):
                break
            if not (t[0].isupper() and t.replace(".", "").isalpha()):
                break
            vend.append(t)
        if vend:
            out["vendor_name"] = " ".join(vend)
            break

    sub_idx = None
    for i, ln in enumerate(lines):
        low = ln.lower()
        if low.startswith("subtotal"):
            out["subtotal"] = _last_money(ln)
            sub_idx = i
        elif low.startswith("tax"):
            out["tax"] = _last_money(ln)
        elif re.match(r"(total|amount due|total due|grand total|balance)", low):
            out["total"] = _last_money(ln)

    count = 0
    for i, ln in enumerate(lines):
        if sub_idx is not None and i >= sub_idx:
            break
        if len(_MONEY_RE.findall(ln)) >= 2:
            count += 1
    if count:
        out["line_item_count"] = str(count)

    return out
'''


def build_overfit(samples: list[dict]) -> str:
    """Memorize training samples; on unseen docs, silently emit the first sample's
    invoice_number + invoice_date (wrong, but non-empty and validation-passing)."""
    memo = {s["full_text"]: s["fields"] for s in samples}
    fallback = {
        "invoice_number": samples[0]["fields"]["invoice_number"],
        "invoice_date": samples[0]["fields"]["invoice_date"],
    }
    generic = GENERIC_PARSER.replace("def extract(text_layout):", "def _generic(text_layout):")
    tail = (
        f"\n_MEMO = {json.dumps(memo)}\n"
        f"_FALLBACK = {json.dumps(fallback)}\n"
        "def extract(text_layout):\n"
        "    ft = text_layout.get('full_text', '')\n"
        "    if ft in _MEMO:\n"
        "        return dict(_MEMO[ft])\n"
        "    out = _generic(text_layout)\n"
        "    out['invoice_number'] = _FALLBACK['invoice_number']\n"
        "    out['invoice_date'] = _FALLBACK['invoice_date']\n"
        "    return out\n"
    )
    return generic + tail


_FP_RE = re.compile(r"Format id: (\S+)")
# the samples payload is a single-line json array right after the SAMPLES_JSON marker; it may
# be the last line of the prompt (no trailing newline) or be followed by a repair section.
_SAMPLES_RE = re.compile(r"SAMPLES_JSON\):\n(\[.*?\])\s*$", re.MULTILINE)


def _parse_prompt(user: str) -> tuple[str, list[dict]]:
    fp = _FP_RE.search(user)
    sm = _SAMPLES_RE.search(user)
    samples = json.loads(sm.group(1)) if sm else []
    return (fp.group(1) if fp else "?"), samples


class FakeSynthesizer:
    """Deterministic synthesis responder. `overfit_first_k` distinct fingerprints get the
    overfit variant; `mode` controls whether that lasts one episode ('once') or every episode
    ('always'). Repair sub-attempts within an episode return the same code."""

    def __init__(self, overfit_first_k: int = 0, mode: str = "once"):
        self.overfit_first_k = overfit_first_k
        self.mode = mode
        self._order: list[str] = []      # distinct fingerprints, first-seen order
        self._episodes: dict[str, int] = {}
        self._last: dict[str, str] = {}

    def __call__(self, system: str, user: str, model: str) -> str:
        fp, samples = _parse_prompt(user)
        if "was INCORRECT" in user and fp in self._last:
            return self._last[fp]
        if fp not in self._order:
            self._order.append(fp)
        self._episodes[fp] = self._episodes.get(fp, 0) + 1

        overfit_fp = fp in self._order[: self.overfit_first_k]
        use_overfit = overfit_fp and (
            self.mode == "always" or self._episodes[fp] == 1
        )
        code = build_overfit(samples) if (use_overfit and samples) else GENERIC_PARSER
        self._last[fp] = code
        return code
