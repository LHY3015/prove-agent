"""Phase 4d: compound fault (routing noise + drift together) — the peel as a peel.

Two checks. First a FAST guard for the honesty property the scenario depends on: a date-style drift
is only genuine `data_drift` (fingerprint-stable → routes to the existing skill) for single-token
date styles; a month-name style shifts the fingerprint and would become new-format discovery. Then
one (slower) end-to-end run asserting the peel separates routing_error from data_drift in a single
mixed stream — healthy skills spared under noise, drifted skills charged to drift and healed.
"""

import tempfile
from pathlib import Path

from scenarios.compound_demo import _DRIFT_STYLE, run

from prove.datagen.generator import FORMATS, DriftSpec, generate_stream
from prove.layout import extract_layout
from prove.router import compute_fingerprint, fingerprint_hash


def _fps(manifest, drifted: bool):
    return {fingerprint_hash(compute_fingerprint(extract_layout(e["pdf_path"])))
            for e in manifest if e["drifted"] == drifted}


def test_stable_drift_targets_keep_the_fingerprint_month_name_does_not():
    d = Path(tempfile.mkdtemp())
    # F1 (%Y-%m-%d) and F4 (%d.%m.%Y) are single-token → fingerprint-stable under the drift the
    # compound scenario uses (genuine data_drift on the existing skill).
    for fmt in (FORMATS[0], FORMATS[3]):
        man = generate_stream(d / fmt.format_id, fmt, n=6, seed=1, drift=DriftSpec(3, _DRIFT_STYLE))
        assert _fps(man, False) == _fps(man, True), f"{fmt.format_id} drift must be stable"
    # F2 (%d %b %Y) has a month token → the same drift SHIFTS the fingerprint (new-format
    # discovery, not drift). Guards against silently drifting a month-name format in the scenario.
    man = generate_stream(d / "F2", FORMATS[1], n=6, seed=1, drift=DriftSpec(3, _DRIFT_STYLE))
    assert _fps(man, False) != _fps(man, True)


def test_peel_separates_routing_noise_from_drift_in_one_stream():
    summary = run(n_formats=4, warmup_n=10, prod_n=22, drift_at=9, noise_rate=0.3, seed=2)
    assert summary["routing_error_verdicts"] >= 1          # healthy skills hit by misroutes
    assert sum(summary["drift_family_verdicts"].values()) >= 1   # drifted skills charged to drift
    assert summary["drifted_formats_deprecated"] >= 1
    # every healthy (non-drifted) format survived the routing noise it absorbed
    assert summary["healthy_formats_spared"] == summary["healthy_formats_total"]
