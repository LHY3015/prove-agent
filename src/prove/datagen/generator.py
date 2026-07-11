"""Synthetic invoice generator: Jinja2 template -> WeasyPrint PDF + ground-truth JSON.

Each *format* is one template family with fixed vendor / currency / date-style so that
same-format docs share a layout (same router fingerprint) while field values vary.
Ground-truth values are the literal strings as displayed, so field-level exact-match in
eval mode is unambiguous (no normalization guesswork).

Drift / fault injection lands here in Phase 3-4; Phase 1 is the clean generator only.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATE_DIR = Path(__file__).parent / "templates"

_ITEM_DESCRIPTIONS = [
    "Consulting hours", "Widget assembly", "License seat", "Support plan",
    "Freight handling", "Cloud storage", "Design revision", "Bulk fasteners",
    "Calibration service", "Extended warranty", "On-site training", "Data migration",
]


@dataclass
class FormatSpec:
    format_id: str
    template: str          # template file name in templates/
    vendor_name: str
    vendor_address: str
    currency: str
    date_style: str        # strftime pattern
    inv_prefix: str
    tax_rate: float        # e.g. 0.07


@dataclass
class DriftSpec:
    """Template drift injected mid-stream (Phase 3). From `at_index` on, a format's invoice
    date is rendered in `new_date_style`. Because the router fingerprint drops digit/month
    tokens, this is fingerprint-STABLE — drifted docs still route to the existing skill, whose
    baked-in date handling misses the new style, so the failure surfaces as a validation
    failure on that skill (genuine template drift, not new-format discovery)."""
    at_index: int
    new_date_style: str


# Eight formats over four distinct layouts (two formats per layout, differentiated by
# vendor/currency/date-style/tax). Layout count is a run-scale knob per the plan.
FORMATS: list[FormatSpec] = [
    FormatSpec("F1_acme", "classic.html", "Acme Corporation",
               "120 Market St, Springfield", "USD", "%Y-%m-%d", "ACM-", 0.07),
    FormatSpec("F2_globex", "classic.html", "Globex Trading Pte Ltd",
               "8 Raffles Quay, Singapore", "SGD", "%d %b %Y", "GLX-", 0.09),
    FormatSpec("F3_initech", "left_meta.html", "Initech Solutions",
               "42 Tech Park, Austin", "USD", "%b %d, %Y", "INI-", 0.0825),
    FormatSpec("F4_umbrella", "left_meta.html", "Umbrella Supplies GmbH",
               "17 Industrie Allee, Berlin", "EUR", "%d.%m.%Y", "UMB-", 0.19),
    FormatSpec("F5_wayne", "banner.html", "Wayne Industries",
               "1007 Mountain Dr, Gotham", "USD", "%m/%d/%Y", "WAY-", 0.06),
    FormatSpec("F6_stark", "banner.html", "Stark Components Ltd",
               "10880 Malibu Point", "GBP", "%d/%m/%Y", "STK-", 0.20),
    FormatSpec("F7_wonka", "receipt.html", "Wonka Logistics",
               "1 Chocolate Way, London", "GBP", "%Y/%m/%d", "WNK-", 0.10),
    FormatSpec("F8_cyberdyne", "receipt.html", "Cyberdyne Systems",
               "18144 El Camino, Sunnyvale", "USD", "%b %d %Y", "CYB-", 0.05),
]


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
    )


def _money(x: float) -> str:
    return f"{x:.2f}"


def _random_doc_context(
    fmt: FormatSpec, rng: random.Random, date_style: str | None = None
) -> dict[str, Any]:
    """Randomize field values for one document; totals stay internally consistent.
    `date_style` overrides the format's default (used to inject template drift)."""
    import datetime

    date_style = date_style or fmt.date_style

    n_items = rng.randint(1, 5)
    items = []
    subtotal = 0.0
    for _ in range(n_items):
        qty = rng.randint(1, 9)
        unit = round(rng.uniform(5.0, 500.0), 2)
        amount = round(qty * unit, 2)
        subtotal += amount
        items.append(
            {
                "desc": rng.choice(_ITEM_DESCRIPTIONS),
                "qty": str(qty),
                "unit_price": _money(unit),
                "amount": _money(amount),
            }
        )
    subtotal = round(subtotal, 2)
    tax = round(subtotal * fmt.tax_rate, 2)
    total = round(subtotal + tax, 2)

    day = datetime.date(2024, 1, 1) + datetime.timedelta(days=rng.randint(0, 364))
    date_str = day.strftime(date_style)
    invoice_number = f"{fmt.inv_prefix}{day.year}-{rng.randint(0, 99999):05d}"

    return {
        "vendor_name": fmt.vendor_name,
        "vendor_address": fmt.vendor_address,
        "invoice_number": invoice_number,
        "invoice_date_str": date_str,
        "currency": fmt.currency,
        "items": items,
        "subtotal_str": _money(subtotal),
        "tax_str": _money(tax),
        "total_str": _money(total),
        "tax_rate_str": f"{fmt.tax_rate * 100:.2f}%",
    }


def _ground_truth(ctx: dict[str, Any]) -> dict[str, str]:
    """Ground truth = literal displayed strings."""
    return {
        "vendor_name": ctx["vendor_name"],
        "invoice_number": ctx["invoice_number"],
        "invoice_date": ctx["invoice_date_str"],
        "currency": ctx["currency"],
        "subtotal": ctx["subtotal_str"],
        "tax": ctx["tax_str"],
        "total": ctx["total_str"],
        "line_item_count": str(len(ctx["items"])),
    }


def generate_dataset(
    out_dir: str | Path,
    samples_per_format: int = 40,
    seed: int = 0,
    formats: list[FormatSpec] | None = None,
) -> list[dict[str, Any]]:
    """Render every format `samples_per_format` times. Writes {doc_id}.pdf per doc and a
    manifest.jsonl; returns the manifest entries (doc_id, format_id_true, pdf_path, fields)."""
    from weasyprint import HTML

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    formats = formats or FORMATS
    env = _env()
    rng = random.Random(seed)

    manifest: list[dict[str, Any]] = []
    for fmt in formats:
        template = env.get_template(fmt.template)
        for i in range(samples_per_format):
            ctx = _random_doc_context(fmt, rng)
            html = template.render(**ctx)
            doc_id = f"{fmt.format_id}_{i:04d}"
            pdf_path = out_dir / f"{doc_id}.pdf"
            HTML(string=html).write_pdf(str(pdf_path))
            entry = {
                "doc_id": doc_id,
                "format_id_true": fmt.format_id,
                "pdf_path": str(pdf_path),
                "fields": _ground_truth(ctx),
            }
            manifest.append(entry)

    with open(out_dir / "manifest.jsonl", "w", encoding="utf-8") as f:
        for entry in manifest:
            f.write(json.dumps(entry) + "\n")
    return manifest


def generate_stream(
    out_dir: str | Path,
    fmt: FormatSpec,
    n: int,
    seed: int = 0,
    drift: DriftSpec | None = None,
) -> list[dict[str, Any]]:
    """Render one format `n` times in a single ordered stream (the drift-demo driver). If `drift`
    is set, docs at index >= `drift.at_index` use `drift.new_date_style`. Writes {doc_id}.pdf +
    manifest.jsonl; returns manifest entries in stream order."""
    from weasyprint import HTML

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    template = _env().get_template(fmt.template)
    rng = random.Random(seed)

    manifest: list[dict[str, Any]] = []
    for i in range(n):
        style = drift.new_date_style if (drift and i >= drift.at_index) else fmt.date_style
        ctx = _random_doc_context(fmt, rng, date_style=style)
        doc_id = f"{fmt.format_id}_{i:04d}"
        pdf_path = out_dir / f"{doc_id}.pdf"
        HTML(string=template.render(**ctx)).write_pdf(str(pdf_path))
        manifest.append({
            "doc_id": doc_id,
            "format_id_true": fmt.format_id,
            "pdf_path": str(pdf_path),
            "drifted": bool(drift and i >= drift.at_index),
            "fields": _ground_truth(ctx),
        })

    with open(out_dir / "manifest.jsonl", "w", encoding="utf-8") as f:
        for entry in manifest:
            f.write(json.dumps(entry) + "\n")
    return manifest


def load_manifest(out_dir: str | Path) -> list[dict[str, Any]]:
    path = Path(out_dir) / "manifest.jsonl"
    with open(path, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]
