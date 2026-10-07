"""Anomaly detection with an exponentially weighted mean/variance (EWMA).

Fixed thresholds catch "latency > 500ms". This catches "latency is 4x what
this service normally does", even when 4x is still under the threshold.

For each (service, metric) we keep an EWMA baseline. A sample is anomalous
when its z-score exceeds `z_threshold`, it is above an absolute floor (so
2ms -> 6ms isn't paged), and this has held for `sustain` samples in a row.
Anomalous samples barely move the baseline, so a long incident doesn't
quietly become the new normal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class AnomalyResult:
    metric: str
    value: float
    mean: float
    std: float
    z: float
    anomalous: bool


class EwmaDetector:
    def __init__(self, alpha: float, z_threshold: float, warmup: int, sustain: int, min_value: float = 0.0) -> None:
        self.alpha = alpha
        self.z_threshold = z_threshold
        self.warmup = warmup
        self.sustain = sustain
        self.min_value = min_value
        self.mean: float | None = None
        self.var = 0.0
        self.n = 0
        self.streak = 0

    def update(self, metric: str, x: float) -> AnomalyResult:
        if self.mean is None:
            self.mean, self.n = x, 1
            return AnomalyResult(metric, x, x, 0.0, 0.0, False)

        # floor on std so a perfectly flat baseline doesn't make every blip infinite
        std = max(math.sqrt(self.var), abs(self.mean) * 0.05, 1e-6)
        z = (x - self.mean) / std
        warmed = self.n >= self.warmup
        breach = warmed and z > self.z_threshold and x >= self.min_value
        self.streak = self.streak + 1 if breach else 0
        anomalous = self.streak >= self.sustain

        diff = x - self.mean
        if breach:
            # while breaching, nudge the mean only and freeze the variance; otherwise the
            # spike inflates std within a few samples and the incident becomes "normal"
            self.mean += self.alpha * 0.1 * diff
        else:
            self.mean += self.alpha * diff
            self.var = (1 - self.alpha) * (self.var + self.alpha * diff * diff)
        self.n += 1
        return AnomalyResult(metric, x, round(self.mean, 3), round(std, 3), round(z, 2), anomalous)


class AnomalyBank:
    """One detector per (service, metric), created on first use."""

    def __init__(self, alpha: float, z_threshold: float, warmup: int, sustain: int, min_values: dict[str, float]):
        self.params = (alpha, z_threshold, warmup, sustain)
        self.min_values = min_values
        self.detectors: dict[tuple[str, str], EwmaDetector] = {}

    def observe(self, service: str, metric: str, value: float | None) -> AnomalyResult | None:
        if value is None:
            return None
        key = (service, metric)
        if key not in self.detectors:
            self.detectors[key] = EwmaDetector(*self.params, min_value=self.min_values.get(metric, 0.0))
        return self.detectors[key].update(metric, value)
