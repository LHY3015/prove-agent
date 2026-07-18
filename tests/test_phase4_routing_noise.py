"""Phase 4a integration (key-free): the A2-vs-A3 routing-noise headline, end to end.

A tiny 3-format run through the real pipeline (datagen -> router -> sandbox -> registry): under the
same injected routing noise, A2 deprecates healthy skills while A3 keeps them alive by attributing
the failures to the router. Slow-ish (renders PDFs); one scenario, asserted, not just plotted."""

import pytest

from scenarios.routing_noise_demo import build_data, run_arm

from prove.config import load_config


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    d = tmp_path_factory.mktemp("rn")
    return build_data(d, n_formats=3, warmup_n=12, prod_n=18, seed=3)


def _cfg():
    cfg = load_config()
    cfg["synthesis_trigger"] = 6
    cfg["trial_docs"] = 3
    cfg["admission"] = {**cfg["admission"], "min_holdout": 3, "holdout_frac": 0.3}
    cfg["monitor"] = {"window": 12, "failure_rate_threshold": 0.2, "confidence_floor": 0.3,
                      "min_window": 6, "min_failures": 3}
    return cfg


def test_a2_kills_healthy_skills_a3_does_not(data):
    warm, prod = data
    cfg = _cfg()
    a2, _ = run_arm("A2", warm, prod, cfg, noise_rate=0.3, seed=3)
    a3, _ = run_arm("A3", warm, prod, cfg, noise_rate=0.3, seed=3)

    a2_kills = sum(r["deprecated"] for r in a2)
    a3_kills = sum(r["deprecated"] for r in a3)

    # the whole point: attribution spares the healthy skills that A2 wrongly kills
    assert a2_kills >= 1, "expected routing noise to wrongly deprecate a skill under A2"
    assert a3_kills == 0, f"A3 should not deprecate healthy skills, got {a3_kills}"
    # and A3 keeps more traffic on cheap skills than the thrashing A2 arm
    a2_skill = sum(r["source"] == "skill" for r in a2)
    a3_skill = sum(r["source"] == "skill" for r in a3)
    assert a3_skill >= a2_skill
