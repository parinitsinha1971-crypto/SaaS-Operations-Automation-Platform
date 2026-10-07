"""Fault injection for the simulated services.

Each fault is time-boxed (except crash/hang, which need a restart) so a
forgotten experiment cleans itself up.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import time
from dataclasses import dataclass, field
from pathlib import Path


def _burn(deadline: float) -> None:  # pragma: no cover - runs in a child process
    x = 0
    while time.time() < deadline:
        x = (x * 31 + 7) % 1_000_003


@dataclass
class ChaosState:
    data_dir: Path
    latency_ms: int = 0
    latency_until: float = 0.0
    error_rate: float = 0.0
    errors_until: float = 0.0
    unhealthy_until: float = 0.0
    hanging: bool = False
    cpu_until: float = 0.0
    _cpu_procs: list = field(default_factory=list)
    _memory: list = field(default_factory=list)

    # ---- queries -------------------------------------------------------
    def active_latency(self) -> int:
        return self.latency_ms if time.time() < self.latency_until else 0

    def active_error_rate(self) -> float:
        return self.error_rate if time.time() < self.errors_until else 0.0

    def is_unhealthy(self) -> bool:
        return time.time() < self.unhealthy_until

    def memory_mb(self) -> int:
        return sum(len(b) for b in self._memory) // (1024 * 1024)

    def disk_fill_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.data_dir.glob("chaos-fill-*.bin"))

    def snapshot(self) -> dict:
        now = time.time()
        return {
            "latency_ms": self.active_latency(),
            "latency_remaining_s": max(0, round(self.latency_until - now)),
            "error_rate": self.active_error_rate(),
            "errors_remaining_s": max(0, round(self.errors_until - now)),
            "unhealthy": self.is_unhealthy(),
            "hanging": self.hanging,
            "cpu_burn_remaining_s": max(0, round(self.cpu_until - now)),
            "memory_held_mb": self.memory_mb(),
            "disk_fill_mb": self.disk_fill_bytes() // (1024 * 1024),
        }

    # ---- faults --------------------------------------------------------
    def set_latency(self, ms: int, seconds: int) -> None:
        self.latency_ms = ms
        self.latency_until = time.time() + seconds

    def set_errors(self, rate: float, seconds: int) -> None:
        self.error_rate = max(0.0, min(1.0, rate))
        self.errors_until = time.time() + seconds

    def set_unhealthy(self, seconds: int) -> None:
        self.unhealthy_until = time.time() + seconds

    def burn_cpu(self, seconds: int, workers: int) -> None:
        self.stop_cpu()
        deadline = time.time() + seconds
        ctx = mp.get_context("spawn")
        for _ in range(max(1, workers)):
            p = ctx.Process(target=_burn, args=(deadline,), daemon=True)
            p.start()
            self._cpu_procs.append(p)
        self.cpu_until = deadline

    def stop_cpu(self) -> None:
        for p in self._cpu_procs:
            if p.is_alive():
                p.terminate()
        self._cpu_procs.clear()
        self.cpu_until = 0.0

    def hold_memory(self, mb: int) -> None:
        # bytearray pages are touched so RSS really grows
        chunk = bytearray(mb * 1024 * 1024)
        for i in range(0, len(chunk), 4096):
            chunk[i] = 1
        self._memory.append(chunk)

    def fill_disk(self, mb: int) -> Path:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.data_dir / f"chaos-fill-{int(time.time() * 1000)}.bin"
        block = os.urandom(1024 * 1024)
        with path.open("wb") as fh:
            for _ in range(mb):
                fh.write(block)
        return path

    def reset(self) -> None:
        self.latency_ms = 0
        self.latency_until = 0.0
        self.error_rate = 0.0
        self.errors_until = 0.0
        self.unhealthy_until = 0.0
        self.hanging = False
        self.stop_cpu()
        self._memory.clear()
        for p in self.data_dir.glob("chaos-fill-*.bin"):
            p.unlink(missing_ok=True)
