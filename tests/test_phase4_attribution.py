"""Phase 4a: attribution rule chain, the routing_noise injector, and the A3 pipeline wiring.

The headline unit test is `test_a3_spares_healthy_skill_under_routing_noise` /
`test_a2_kills_healthy_skill_under_routing_noise`: the SAME batch (a skill's own high-confidence
traffic passing, interleaved with low-confidence misroutes failing) deprecates the skill under A2
(every failure charged raw) but is exonerated as routing_error under A3 (the misroutes belong to
the router's account, not the skill's).
"""

from __future__ import annotations

from prove.attribution import Attributor
from prove.audit import PoolAuditor
from prove.config import load_config
from prove.datagen.faults import NoisyRouter, corrupt_validator
from prove.llm_client import FakeClient
from prove.pipeline import Pipeline
from prove.registry import Registry
from prove.router import Router
from prove.sample_pool import PoolSample, SamplePool
from prove.schemas import Trace, ValidationVerdict


# --------------------------------------------------------------------------- helpers


def mk_trace(doc_id: str, conf: float, passed: bool, *, rule_failures=None,
             source: str = "skill", skill_id: str = "F1-v1",
             integrity: float = 1.0) -> Trace:
    verdict = ValidationVerdict(doc_id=doc_id, passed=passed,
                               rule_failures=list(rule_failures or []))
    return Trace(
        doc_id=doc_id, ts=0.0, route_format_id="F1", route_confidence=conf,
        route_method="exact" if conf >= 0.9 else "fuzzy", route_fingerprint="fp",
        skill_id=skill_id, skill_version=1, extraction_source=source,
        field_results={}, validation=verdict, input_integrity=integrity,
    )


# --------------------------------------------------------------------------- attribution rules


def test_routing_error_when_failures_are_low_confidence_amid_high_conf_passes():
    batch = ([mk_trace(f"p{i}", 1.0, True) for i in range(8)]
             + [mk_trace(f"f{i}", 0.4, False) for i in range(4)])
    r = Attributor().classify(batch)
    assert r.root_cause == "routing_error"
    assert r.charged_doc_ids == []                      # skill's beta untouched
    assert set(r.exonerated_doc_ids) == {f"f{i}" for i in range(4)}


def test_data_drift_on_abrupt_onset_at_high_confidence():
    batch = ([mk_trace(f"p{i}", 1.0, True) for i in range(6)]
             + [mk_trace(f"f{i}", 1.0, False) for i in range(5)])
    r = Attributor().classify(batch)
    assert r.root_cause == "data_drift"
    assert set(r.charged_doc_ids) == {f"f{i}" for i in range(5)}


def test_skill_defect_when_high_conf_failures_reach_batch_start():
    batch = [mk_trace("f0", 1.0, False), mk_trace("p0", 1.0, True),
             mk_trace("f1", 1.0, False), mk_trace("p1", 1.0, True),
             mk_trace("f2", 1.0, False), mk_trace("f3", 1.0, False)]
    r = Attributor().classify(batch)
    assert r.root_cause == "skill_defect"
    assert set(r.charged_doc_ids) == {"f0", "f1", "f2", "f3"}


def test_rule_defect_when_failures_are_entirely_frozen_rules():
    batch = ([mk_trace(f"p{i}", 1.0, True) for i in range(5)]
             + [mk_trace(f"f{i}", 1.0, False, rule_failures=["money_arithmetic"])
                for i in range(4)])
    r = Attributor().classify(batch, frozen_rules={"money_arithmetic"})
    assert r.root_cause == "rule_defect"
    assert r.charged_doc_ids == []


def test_peel_separates_routing_failures_from_a_concurrent_skill_defect():
    # low-conf misroutes AND high-conf skill failures in one batch: the misroutes are exonerated,
    # the high-conf failures are charged (mixed-cause batch resolves, not collapses to one label).
    batch = ([mk_trace("f_skill0", 1.0, False), mk_trace("p0", 1.0, True)]
             + [mk_trace(f"p{i}", 1.0, True) for i in range(1, 5)]
             + [mk_trace(f"f_route{i}", 0.4, False) for i in range(3)]
             + [mk_trace("f_skill1", 1.0, False)])
    r = Attributor().classify(batch)
    assert r.root_cause == "skill_defect"
    assert set(r.charged_doc_ids) == {"f_skill0", "f_skill1"}
    assert set(r.exonerated_doc_ids) == {"f_route0", "f_route1", "f_route2"}


def test_mixed_batch_peel_routing_noise_concurrent_with_drift():
    # low-confidence misroutes AND abrupt high-confidence drift failures in one batch: the peel
    # exonerates exactly the misroutes and charges only the drift residual (β = residual, not the
    # whole batch). This is the peel working AS a peel — the design's stated purpose.
    batch = ([mk_trace(f"p{i}", 1.0, True) for i in range(6)]
             + [mk_trace(f"drift{i}", 1.0, False) for i in range(4)]
             + [mk_trace(f"route{i}", 0.4, False) for i in range(3)])
    r = Attributor().classify(batch)
    assert r.root_cause == "data_drift"
    assert set(r.charged_doc_ids) == {f"drift{i}" for i in range(4)}
    assert set(r.exonerated_doc_ids) == {f"route{i}" for i in range(3)}


def test_all_low_confidence_failures_without_a_clean_baseline_charge_the_skill():
    # boundary (documented): with NO high-confidence pass, the peel has no contrast evidence that
    # the skill works, so it cannot exonerate the low-conf failures — the conservative choice is to
    # charge the skill. Under heavy noise (contrast lost) A3 degrades toward A2 by design.
    batch = [mk_trace(f"f{i}", 0.4, False) for i in range(5)]
    r = Attributor().classify(batch)
    assert r.root_cause == "skill_defect"
    assert set(r.charged_doc_ids) == {f"f{i}" for i in range(5)}


def test_input_noise_when_failures_are_on_degraded_documents():
    # the account's reason for existing: a degraded doc still routes EXACTLY (its header survived),
    # so nothing but the integrity signal distinguishes it from a genuine skill defect.
    batch = ([mk_trace(f"p{i}", 1.0, True) for i in range(6)]
             + [mk_trace(f"n{i}", 1.0, False, integrity=0.6) for i in range(4)])
    r = Attributor().classify(batch)
    assert r.root_cause == "input_noise"
    assert r.charged_doc_ids == []
    assert set(r.exonerated_doc_ids) == {f"n{i}" for i in range(4)}


def test_without_the_integrity_peel_degraded_docs_would_be_charged_to_the_skill():
    # regression guard on the miscarriage this account prevents: identical batch, integrity signal
    # absent (tau below the degraded score) → the same failures land on the skill.
    batch = ([mk_trace(f"p{i}", 1.0, True) for i in range(6)]
             + [mk_trace(f"n{i}", 1.0, False, integrity=0.6) for i in range(4)])
    r = Attributor(integrity_tau=0.0).classify(batch)
    assert r.root_cause in ("skill_defect", "data_drift")
    assert set(r.charged_doc_ids) == {f"n{i}" for i in range(4)}


def test_input_noise_peels_before_routing_when_a_doc_qualifies_for_both():
    # garbled header tokens depress route confidence too; the doc must be charged to the root
    # cause (unreadable input), not to the symptom (weak route).
    batch = ([mk_trace(f"p{i}", 1.0, True) for i in range(6)]
             + [mk_trace(f"n{i}", 0.4, False, integrity=0.5) for i in range(4)])
    r = Attributor().classify(batch)
    assert r.root_cause == "input_noise"


def test_degraded_documents_that_pass_validation_are_not_peeled():
    # only failures are peeled — a low-integrity doc the skill parsed correctly harmed nobody.
    batch = ([mk_trace(f"p{i}", 1.0, True, integrity=0.5) for i in range(6)]
             + [mk_trace(f"f{i}", 1.0, False) for i in range(4)])
    r = Attributor().classify(batch)
    assert r.root_cause in ("skill_defect", "data_drift")
    assert set(r.charged_doc_ids) == {f"f{i}" for i in range(4)}


def test_mixed_batch_splits_input_noise_from_a_genuine_defect():
    # composition: the residual (clean, high-confidence failures) still reaches the skill.
    batch = ([mk_trace(f"p{i}", 1.0, True) for i in range(4)]
             + [mk_trace(f"n{i}", 1.0, False, integrity=0.5) for i in range(3)]
             + [mk_trace(f"d{i}", 1.0, False) for i in range(4)])
    r = Attributor().classify(batch)
    assert set(r.charged_doc_ids) == {f"d{i}" for i in range(4)}
    assert set(r.exonerated_doc_ids) == {f"n{i}" for i in range(3)}


def test_ambiguous_when_no_failures_in_batch():
    batch = [mk_trace(f"p{i}", 1.0, True) for i in range(5)]
    r = Attributor().classify(batch)
    assert r.root_cause == "ambiguous"


def test_llm_fallback_docs_are_not_the_skills_account():
    # an LLM-served failure in the window is not attributable to the skill and must be ignored.
    batch = ([mk_trace(f"p{i}", 1.0, True) for i in range(6)]
             + [mk_trace("llm_fail", 0.0, False, source="llm")])
    r = Attributor().classify(batch)
    assert r.root_cause == "ambiguous"   # only the passes remain → no skill failures


# --------------------------------------------------------------------------- NoisyRouter


def _two_format_router(cls=Router, **kw):
    fp1 = frozenset({("acme", 1, 1), ("invoice", 2, 1), ("bill", 3, 4)})
    fp2 = frozenset({("globex", 1, 1), ("statement", 2, 1), ("bill", 3, 4)})
    r = cls(**kw) if cls is Router else cls(**kw)
    r.register_fingerprint("F1", fp1)
    r.register_fingerprint("F2", fp2)
    return r, fp1, fp2


def test_noisy_router_forces_a_genuine_low_confidence_misroute():
    nr = NoisyRouter(lambda: {"F1", "F2"}, noise_rate=1.0, seed=1)
    _, fp1, fp2 = _two_format_router()
    nr.register_fingerprint("F1", fp1)
    nr.register_fingerprint("F2", fp2)
    nr.begin("doc0", "F1")
    fmt, conf, method = nr.match(fp1)          # fp1 would match F1 exactly
    assert fmt == "F2" and method == "fuzzy"   # delivered to the wrong served format
    assert 0.0 <= conf < 0.9                    # a real Jaccard vs F2, never fabricated
    assert nr.faults == [{"doc_id": "doc0", "true_format": "F1",
                          "forced_format": "F2", "confidence": round(conf, 4)}]


def test_noisy_router_passes_through_when_no_other_served_format():
    nr = NoisyRouter(lambda: {"F1"}, noise_rate=1.0, seed=1)  # only F1 served → no misroute target
    _, fp1, fp2 = _two_format_router()
    nr.register_fingerprint("F1", fp1)
    nr.register_fingerprint("F2", fp2)
    nr.begin("doc0", "F1")
    fmt, conf, method = nr.match(fp1)
    assert (fmt, method) == ("F1", "exact") and conf >= 0.9
    assert nr.faults == []


def test_noisy_router_never_misroutes_a_miss():
    nr = NoisyRouter(lambda: {"F1", "F2"}, noise_rate=1.0, seed=1)
    _, fp1, fp2 = _two_format_router()
    nr.register_fingerprint("F1", fp1)
    nr.register_fingerprint("F2", fp2)
    nr.begin("doc0", None)
    fmt, _, method = nr.match(frozenset({("unknown", 9, 9)}))
    assert method == "miss" and fmt is None and nr.faults == []


# --------------------------------------------------------------------------- A3 pipeline wiring


def _pipeline_with_serving_skill(mode: str):
    cfg = load_config()
    cfg["ablation"] = {"mode": mode}
    cfg["monitor"] = {"window": 20, "failure_rate_threshold": 0.2, "confidence_floor": 0.3,
                      "min_window": 10, "min_failures": 3}
    reg = Registry(":memory:")
    skill = reg.create_candidate("F1", "def extract(t):\n    return {}\n", 1)
    reg.admit_to_trial(skill.skill_id, {"holdout_f1": 1.0, "holdout_n": 3})
    reg.activate(skill.skill_id)
    pipe = Pipeline(cfg, FakeClient(lambda s, u, m: ""), registry=reg)
    return pipe, skill.skill_id


def _feed(pipe, skill_id, n_pass, n_fail, fail_conf):
    for i in range(n_pass):
        pipe._record_skill_outcome(mk_trace(f"p{i}", 1.0, True, skill_id=skill_id))
    for i in range(n_fail):
        pipe._record_skill_outcome(mk_trace(f"f{i}", fail_conf, False, skill_id=skill_id))


def test_a2_kills_healthy_skill_under_routing_noise():
    pipe, sid = _pipeline_with_serving_skill("A2")
    _feed(pipe, sid, n_pass=10, n_fail=4, fail_conf=0.4)   # low-conf misroute failures
    assert pipe.registry.get_skill(sid).state == "deprecated"


def test_a3_spares_healthy_skill_under_routing_noise():
    pipe, sid = _pipeline_with_serving_skill("A3")
    _feed(pipe, sid, n_pass=10, n_fail=4, fail_conf=0.4)
    skill = pipe.registry.get_skill(sid)
    assert skill.state in ("trial", "active")              # NOT deprecated
    assert skill.confidence > 0.5                          # beta never charged for the misroutes
    events = [e["event_type"] for e in pipe.registry.events(sid)]
    assert "routing_quarantine" in events
    assert any(a["root_cause"] == "routing_error" for a in pipe.attributions)


def test_a2_kills_healthy_skill_under_input_noise():
    # the counterfactual the fifth account exists for: degraded docs route EXACTLY (their headers
    # survived), so without an integrity peel these are indistinguishable from a skill defect.
    pipe, sid = _pipeline_with_serving_skill("A2")
    for i in range(10):
        pipe._record_skill_outcome(mk_trace(f"p{i}", 1.0, True, skill_id=sid))
    for i in range(4):
        pipe._record_skill_outcome(mk_trace(f"f{i}", 1.0, False, skill_id=sid, integrity=0.6))
    assert pipe.registry.get_skill(sid).state == "deprecated"


def test_a3_spares_healthy_skill_under_input_noise():
    pipe, sid = _pipeline_with_serving_skill("A3")
    for i in range(10):
        pipe._record_skill_outcome(mk_trace(f"p{i}", 1.0, True, skill_id=sid))
    for i in range(4):
        pipe._record_skill_outcome(mk_trace(f"f{i}", 1.0, False, skill_id=sid, integrity=0.6))
    skill = pipe.registry.get_skill(sid)
    assert skill.state in ("trial", "active")              # NOT deprecated
    assert skill.confidence > 0.5                          # beta never charged for the bad input
    events = [e["event_type"] for e in pipe.registry.events(sid)]
    assert "input_noise_quarantine" in events
    verdicts = [a for a in pipe.attributions if a["root_cause"] == "input_noise"]
    assert verdicts and verdicts[0]["action_taken"] == "quarantine_documents"


def test_a3_deprecates_on_a_genuine_skill_defect():
    pipe, sid = _pipeline_with_serving_skill("A3")
    # high-confidence failures from the batch start → skill_defect → deprecate + charge
    for i in range(4):
        pipe._record_skill_outcome(mk_trace(f"f{i}", 1.0, False, skill_id=sid))
    for i in range(8):
        pipe._record_skill_outcome(mk_trace(f"p{i}", 1.0, True, skill_id=sid))
    for i in range(4, 8):
        pipe._record_skill_outcome(mk_trace(f"f{i}", 1.0, False, skill_id=sid))
    assert pipe.registry.get_skill(sid).state == "deprecated"
    assert any(a["root_cause"] == "skill_defect" for a in pipe.attributions)


# --------------------------------------------------------------------------- audit + rule_defect


def _usd_pool(fp: str, n: int = 12) -> SamplePool:
    pool = SamplePool()
    for i in range(n):
        pool.add(PoolSample(
            doc_id=f"{fp}_{i}", fingerprint=fp, text_layout={"full_text": ""},
            fields={"vendor_name": "Acme", "invoice_number": f"ACM-2024-{i:05d}",
                    "invoice_date": "2024-03-01", "currency": "USD", "subtotal": "100.00",
                    "tax": "7.00", "total": "107.00", "line_item_count": "2"}))
    return pool


def test_audit_flags_a_corrupted_rule_on_the_immutable_pool():
    pool = _usd_pool("FP")
    # a healthy validator: the pool that passed at admission still passes, nothing flagged
    clean = PoolAuditor().audit(pool, "FP")
    assert clean.pass_rate == 1.0 and clean.corrupted_rules == []
    # a corrupted validator: USD is spuriously rejected → the rule fires on the unchanged pool
    corrupt = PoolAuditor(validator=corrupt_validator("USD")).audit(pool, "FP")
    assert corrupt.corrupted_rules == ["currency_invalid"] and corrupt.anomalous()


def test_a3_rule_defect_spares_skill_and_freezes_rule():
    cfg = load_config()
    cfg["ablation"] = {"mode": "A3"}
    cfg["monitor"] = {"window": 20, "failure_rate_threshold": 0.2, "confidence_floor": 0.3,
                      "min_window": 10, "min_failures": 3}
    reg = Registry(":memory:")
    skill = reg.create_candidate("FP", "def extract(t):\n    return {}\n", 1)
    reg.admit_to_trial(skill.skill_id, {"holdout_f1": 1.0, "holdout_n": 3})
    reg.activate(skill.skill_id)
    pool = _usd_pool("FP")
    pipe = Pipeline(cfg, FakeClient(lambda s, u, m: ""), registry=reg, sample_pool=pool,
                    validator=corrupt_validator("USD"))
    # the skill's own (correct) USD docs now fail with the corrupted rule
    for i in range(12):
        pipe._record_skill_outcome(
            mk_trace(f"f{i}", 1.0, False, rule_failures=["currency_invalid"],
                     skill_id=skill.skill_id))
    surviving = pipe.registry.get_skill(skill.skill_id)
    assert surviving.state in ("trial", "active")               # not deprecated
    assert "currency_invalid" in pipe._frozen_rules             # rule frozen for repair
    assert any(a["root_cause"] == "rule_defect" for a in pipe.attributions)


def test_rule_defect_batch_does_not_freeze_legitimate_rules_of_misrouted_docs():
    # regression: a mixed rule_defect batch (routing-exonerated misroutes firing a LEGITIMATE date
    # rule + corrupted-currency failures) must freeze ONLY the audit-confirmed corrupted rule. The
    # audit is the sole freezing authority; freezing the misroutes' legit rules would permanently
    # mask future genuine skill failures (global monotone _frozen_rules).
    cfg = load_config()
    cfg["ablation"] = {"mode": "A3"}
    cfg["monitor"] = {"window": 20, "failure_rate_threshold": 0.2, "confidence_floor": 0.3,
                      "min_window": 14, "min_failures": 3}
    reg = Registry(":memory:")
    skill = reg.create_candidate("FP", "def extract(t):\n    return {}\n", 1)
    reg.admit_to_trial(skill.skill_id, {"holdout_f1": 1.0, "holdout_n": 3})
    reg.activate(skill.skill_id)
    pipe = Pipeline(cfg, FakeClient(lambda s, u, m: ""), registry=reg, sample_pool=_usd_pool("FP"),
                    validator=corrupt_validator("USD"))
    sid = skill.skill_id
    for i in range(6):                                           # clean high-conf baseline
        pipe._record_skill_outcome(mk_trace(f"p{i}", 1.0, True, skill_id=sid))
    for i in range(3):                                           # low-conf misroutes on a legit rule
        pipe._record_skill_outcome(mk_trace(f"route{i}", 0.4, False,
                                            rule_failures=["missing_field:invoice_date"], skill_id=sid))
    for i in range(5):                                           # corrupted-currency victims
        pipe._record_skill_outcome(mk_trace(f"cur{i}", 1.0, False,
                                            rule_failures=["currency_invalid"], skill_id=sid))
    assert any(a["root_cause"] == "rule_defect" for a in pipe.attributions)
    assert "currency_invalid" in pipe._frozen_rules
    assert "missing_field:invoice_date" not in pipe._frozen_rules   # the legit rule stays live
    assert reg.get_skill(sid).state in ("trial", "active")
