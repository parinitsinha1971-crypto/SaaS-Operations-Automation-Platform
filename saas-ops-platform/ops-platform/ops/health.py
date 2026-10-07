"""Service health state machine.

Each cycle produces a raw verdict (healthy / degraded / down). The public
state only changes after the verdict has held for N consecutive cycles,
which stops a single dropped packet from paging anyone or triggering a
restart (flap protection).

    unknown --(N ok)--> healthy --(N degraded)--> degraded
        \\                 \\________(N down)______> down
         \\________________________(N down)_______/

Recovery to healthy needs `recovery_threshold` consecutive healthy cycles.
"""

from __future__ import annotations

from dataclasses import dataclass

HEALTHY, DEGRADED, DOWN, UNKNOWN = "healthy", "degraded", "down", "unknown"
SEVERITY = {UNKNOWN: 0, HEALTHY: 0, DEGRADED: 1, DOWN: 2}


@dataclass
class Transition:
    old: str
    new: str


class HealthTracker:
    def __init__(self, failure_threshold: int, degraded_threshold: int, recovery_threshold: int) -> None:
        self.thresholds = {DOWN: failure_threshold, DEGRADED: degraded_threshold, HEALTHY: recovery_threshold}
        self.state = UNKNOWN
        self.candidate: str | None = None
        self.streak = 0

    def observe(self, verdict: str) -> Transition | None:
        if verdict == self.state:
            self.candidate, self.streak = None, 0
            return None
        if verdict == self.candidate:
            self.streak += 1
        else:
            self.candidate, self.streak = verdict, 1

        # escalating DOWN -> worse needs the full count; going from down to degraded
        # (partially recovered) uses the degraded threshold
        needed = self.thresholds[verdict]
        if self.streak >= needed:
            old, self.state = self.state, verdict
            self.candidate, self.streak = None, 0
            return Transition(old, verdict)
        return None

    def reset(self) -> None:
        self.candidate, self.streak = None, 0


def verdict_from(critical_failed: bool, degraded_reasons: list[str]) -> str:
    if critical_failed:
        return DOWN
    if degraded_reasons:
        return DEGRADED
    return HEALTHY
