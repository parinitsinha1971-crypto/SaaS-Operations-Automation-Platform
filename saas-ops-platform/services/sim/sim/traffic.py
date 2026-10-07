"""Background traffic generator so dashboards always have something to show.

Usage: python -m sim.traffic
Env:   TARGETS=http://saas-api:8000,http://saas-web:8000   RPS=5
"""

from __future__ import annotations

import asyncio
import os
import random

import httpx

TARGETS = [t.strip() for t in os.getenv("TARGETS", "http://saas-api:8000,http://saas-web:8000").split(",") if t.strip()]
RPS = float(os.getenv("RPS", "5"))

PATHS = {
    "api": [
        ("GET", "/api/v1/orders", 5),
        ("GET", "/api/v1/orders/{id}", 4),
        ("POST", "/api/v1/orders", 1),
        ("GET", "/api/v1/users/me", 3),
        ("GET", "/api/v1/orders/9999", 1),
    ],
    "web": [("GET", "/", 5), ("GET", "/pricing", 2), ("GET", "/login", 2), ("GET", "/dashboard", 3)],
}


def _pick(target: str):
    role = "web" if "web" in target else "api"
    choices = PATHS[role]
    method, path, _ = random.choices(choices, weights=[w for *_, w in choices])[0]
    return method, path.replace("{id}", str(random.randint(1, 50)))


async def worker(client: httpx.AsyncClient) -> None:
    while True:
        target = random.choice(TARGETS)
        method, path = _pick(target)
        try:
            if method == "POST":
                await client.post(target + path, json={"sku": "plan-team", "amount": random.randint(10, 300)})
            else:
                await client.get(target + path)
        except httpx.HTTPError:
            pass  # the target being down is part of the experiment
        await asyncio.sleep(random.expovariate(RPS / 4))


async def main() -> None:
    print(f"traffic generator: {RPS} rps across {TARGETS}", flush=True)
    async with httpx.AsyncClient(timeout=5.0) as client:
        await asyncio.gather(*(worker(client) for _ in range(4)))


if __name__ == "__main__":
    asyncio.run(main())
