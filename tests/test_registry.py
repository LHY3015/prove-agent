"""Registry: state-machine transitions + discounted-Beta ledger (plan §6)."""

import math

from prove.registry import Registry

_CODE = "def extract(tl):\n    return {}"


def _reg(tmp_path):
    return Registry(str(tmp_path / "reg.sqlite"), skills_dir=tmp_path / "skills",
                    prior=1.0, decay=0.97)


def test_create_candidate_persists_code(tmp_path):
    reg = _reg(tmp_path)
    s = reg.create_candidate("fpABC", _CODE)
    assert s.state == "candidate" and s.version == 1
    assert reg.get_code(s.skill_id) == _CODE
    assert s.alpha == 1.0 and s.beta == 1.0  # prior only, not born confident


def test_versions_increment_per_format(tmp_path):
    reg = _reg(tmp_path)
    a = reg.create_candidate("fp", _CODE)
    b = reg.create_candidate("fp", _CODE)
    assert (a.version, b.version) == (1, 2)
    assert a.skill_id != b.skill_id


def test_admit_to_trial_seeds_ledger_from_holdout(tmp_path):
    reg = _reg(tmp_path)
    s = reg.create_candidate("fp", _CODE)
    s = reg.admit_to_trial(s.skill_id, {"holdout_f1": 1.0, "holdout_n": 3})
    # alpha = prior + f1*H = 1 + 3 = 4 ; beta = prior + (1-f1)*H = 1 ; conf = 0.8 (never 1.0)
    assert s.state == "trial" and s.alpha == 4.0 and s.beta == 1.0
    assert math.isclose(s.confidence, 0.8)


def test_direct_active_shortcut_keeps_prior_ledger(tmp_path):
    reg = _reg(tmp_path)
    s = reg.create_candidate("fp", _CODE)
    s = reg.admit_direct_active(s.skill_id)
    assert s.state == "active" and s.confidence == 0.5  # A1: no gate, no seed


def test_discounted_beta_update(tmp_path):
    reg = _reg(tmp_path)
    s = reg.create_candidate("fp", _CODE)  # alpha=beta=1
    s = reg.record_outcome(s.skill_id, success=True)   # alpha = 0.97*1 + 1 = 1.97
    assert math.isclose(s.alpha, 1.97) and math.isclose(s.beta, 0.97)
    s = reg.record_outcome(s.skill_id, success=False)  # beta = 0.97*0.97 + 1
    assert math.isclose(s.beta, 0.97 * 0.97 + 1.0)


def test_unattributed_outcome_leaves_ledger_untouched(tmp_path):
    reg = _reg(tmp_path)
    s = reg.create_candidate("fp", _CODE)
    s = reg.record_outcome(s.skill_id, success=False, attributed=False)
    assert s.alpha == 1.0 and s.beta == 1.0  # routing_error / rule_defect never charge the skill


def test_serving_skill_prefers_active_over_trial(tmp_path):
    reg = _reg(tmp_path)
    t = reg.create_candidate("fp", _CODE)
    reg.admit_to_trial(t.skill_id, {"holdout_f1": 1.0, "holdout_n": 3})
    assert reg.serving_skill("fp").state == "trial"
    a = reg.create_candidate("fp", _CODE)
    reg.admit_direct_active(a.skill_id)
    assert reg.serving_skill("fp").skill_id == a.skill_id  # active wins


def test_deprecate(tmp_path):
    reg = _reg(tmp_path)
    s = reg.create_candidate("fp", _CODE)
    reg.admit_direct_active(s.skill_id)
    s = reg.deprecate(s.skill_id, "test")
    assert s.state == "deprecated" and s.deprecated_reason == "test"
    assert reg.serving_skill("fp") is None
