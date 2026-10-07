import random

from ops.anomaly import AnomalyBank, EwmaDetector
from ops.health import DEGRADED, DOWN, HEALTHY, UNKNOWN, HealthTracker, verdict_from


def _det(**kw):
    params = {"alpha": 0.1, "z_threshold": 3.5, "warmup": 20, "sustain": 2, "min_value": 50}
    params.update(kw)
    return EwmaDetector(**params)


def test_stable_noise_is_not_anomalous():
    rng = random.Random(1)
    d = _det()
    flagged = [d.update("lat", rng.gauss(100, 5)).anomalous for _ in range(500)]
    assert not any(flagged)


def test_spike_detected_only_when_sustained():
    rng = random.Random(2)
    d = _det()
    for _ in range(50):
        d.update("lat", rng.gauss(100, 5))
    first = d.update("lat", 400)
    assert first.z > 3.5 and not first.anomalous  # one sample isn't enough
    assert d.update("lat", 420).anomalous


def test_no_alerts_during_warmup_and_below_floor():
    d = _det(min_value=1000)
    for _ in range(50):
        d.update("lat", 100)
    assert not d.update("lat", 500).anomalous and not d.update("lat", 500).anomalous  # below absolute floor
    fresh = _det()
    fresh.update("lat", 100)
    assert not fresh.update("lat", 10_000).anomalous  # still warming up


def test_long_incident_does_not_become_baseline():
    rng = random.Random(3)
    d = _det()
    for _ in range(60):
        d.update("lat", rng.gauss(100, 5))
    results = [d.update("lat", 500) for _ in range(30)]
    assert all(r.anomalous for r in results[1:])
    assert d.mean < 250  # baseline learned slowly while breaching


def test_bank_tracks_metrics_independently():
    bank = AnomalyBank(0.1, 3.5, 5, 1, {"a": 0})
    for _ in range(10):
        bank.observe("svc", "a", 1.0)
        bank.observe("svc", "b", 1000.0)
    assert bank.observe("svc", "a", None) is None
    assert len(bank.detectors) == 2


def test_health_tracker_debounces_and_recovers():
    t = HealthTracker(failure_threshold=3, degraded_threshold=2, recovery_threshold=2)
    assert t.state == UNKNOWN
    assert t.observe(HEALTHY) is None
    assert t.observe(HEALTHY).new == HEALTHY
    assert t.observe(DOWN) is None and t.observe(DOWN) is None
    assert t.observe(HEALTHY) is None  # blip resets the streak
    assert t.observe(DOWN) is None and t.observe(DOWN) is None
    tr = t.observe(DOWN)
    assert tr.old == HEALTHY and tr.new == DOWN
    assert t.observe(DEGRADED) is None
    assert t.observe(DEGRADED).new == DEGRADED
    assert t.observe(HEALTHY) is None
    assert t.observe(HEALTHY).new == HEALTHY


def test_verdict():
    assert verdict_from(True, ["slow"]) == DOWN
    assert verdict_from(False, ["slow"]) == DEGRADED
    assert verdict_from(False, []) == HEALTHY
