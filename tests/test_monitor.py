"""Monitor deprecation triggers: fast window path, its absolute failure floor, the slow ledger
floor, per-skill_id isolation, and the A2-noise calibration (steady low noise must NOT trip)."""

from prove.monitor import Monitor


def _feed(mon, skill_id, pattern):
    for passed in pattern:
        mon.record(skill_id, passed)


def test_fast_path_fires_on_failure_batch():
    mon = Monitor(window=20, failure_rate_threshold=0.2, confidence_floor=0.0,
                  min_window=10, min_failures=3)
    _feed(mon, "s", [True] * 15 + [False] * 5)  # 5/20 = 0.25 > 0.2, 5 >= 3
    assert mon.should_deprecate("s", confidence=0.9).startswith("failure_batch:window")


def test_fast_path_blocked_below_min_window():
    mon = Monitor(window=20, failure_rate_threshold=0.2, confidence_floor=0.0,
                  min_window=10, min_failures=3)
    _feed(mon, "s", [False] * 5)  # 100% fail rate but only 5 docs < min_window
    assert mon.should_deprecate("s", confidence=0.9) is None


def test_failure_floor_blocks_young_skill_noise():
    # rate would trip (0.2 > 0.1) but the absolute floor (>=3) protects a young skill from 2
    # noise-induced failures — the A2 routing-noise calibration Fable required.
    mon = Monitor(window=20, failure_rate_threshold=0.1, confidence_floor=0.0,
                  min_window=10, min_failures=3)
    _feed(mon, "s", [True] * 8 + [False] * 2)  # 2/10 = 0.2 > 0.1 but 2 < 3
    assert mon.should_deprecate("s", confidence=0.9) is None


def test_steady_low_noise_never_trips():
    # ~10% steady failure over a long stream stays under both threshold and floor-per-window.
    mon = Monitor(window=20, failure_rate_threshold=0.2, confidence_floor=0.0,
                  min_window=10, min_failures=3)
    for i in range(200):
        mon.record("s", passed=(i % 10 != 0))  # every 10th fails -> 2 fails per 20-window
        assert mon.should_deprecate("s", confidence=0.9) is None


def test_slow_path_confidence_floor():
    mon = Monitor(window=20, failure_rate_threshold=0.99, confidence_floor=0.3,
                  min_window=10, min_failures=99)
    _feed(mon, "s", [True] * 5)  # window can't fire; ledger floor must
    assert mon.should_deprecate("s", confidence=0.25).startswith("confidence_floor")
    assert mon.should_deprecate("s", confidence=0.5) is None


def test_windows_isolated_by_skill_id():
    # a resynthesized skill (new id) must not inherit the deprecated skill's failure window.
    mon = Monitor(window=20, failure_rate_threshold=0.2, confidence_floor=0.0,
                  min_window=10, min_failures=3)
    _feed(mon, "v1", [False] * 12)
    mon.drop("v1")
    _feed(mon, "v2", [True] * 12)
    assert mon.should_deprecate("v2", confidence=0.9) is None
