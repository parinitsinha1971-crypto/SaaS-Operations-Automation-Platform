"""Active checks: HTTP, API (HTTP + assertions), TCP and PostgreSQL.

Every check returns a CheckOutcome and never raises, so one broken service
can't stall the monitor loop.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import ApiCheck, Check, HttpCheck, PostgresCheck, TcpCheck


@dataclass
class CheckOutcome:
    name: str
    type: str
    ok: bool
    critical: bool
    latency_ms: float | None = None
    status_code: int | None = None
    detail: str = ""
    slow: bool = False  # passed, but slower than its latency budget
    extra: dict[str, Any] = field(default_factory=dict)


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


async def run_http(check: HttpCheck, client: httpx.AsyncClient, timeout: float) -> CheckOutcome:
    start = time.perf_counter()
    body = check.json_body if isinstance(check, ApiCheck) else None
    try:
        resp = await client.request(check.method, check.url, json=body, timeout=timeout)
    except httpx.TimeoutException:
        return CheckOutcome(
            check.name, check.type, False, check.critical, _ms(start), detail=f"timeout after {timeout}s"
        )
    except httpx.HTTPError as exc:
        return CheckOutcome(
            check.name, check.type, False, check.critical, _ms(start), detail=f"{type(exc).__name__}: {exc}"
        )

    latency = _ms(start)
    out = CheckOutcome(check.name, check.type, True, check.critical, latency, resp.status_code)
    if "json" in resp.headers.get("content-type", ""):
        try:
            out.extra["json"] = resp.json()
        except ValueError:
            pass

    if resp.status_code not in check.expect_status:
        out.ok = False
        out.detail = f"status {resp.status_code}, expected {check.expect_status}"
        return out

    if isinstance(check, ApiCheck):
        data = out.extra.get("json")
        missing = [k for k in check.expect_json_keys if not isinstance(data, dict) or k not in data]
        if missing:
            out.ok = False
            out.detail = f"response missing keys {missing}"
            return out
        if check.max_latency_ms and latency > check.max_latency_ms:
            out.slow = True
            out.detail = f"slow: {latency:.0f}ms > {check.max_latency_ms:.0f}ms budget"
    return out


async def run_tcp(check: TcpCheck, timeout: float) -> CheckOutcome:
    start = time.perf_counter()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(check.host, check.port), timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return CheckOutcome(
            check.name, "tcp", True, check.critical, _ms(start), detail=f"{check.host}:{check.port} open"
        )
    except TimeoutError:
        return CheckOutcome(
            check.name, "tcp", False, check.critical, _ms(start), detail=f"connect timeout after {timeout}s"
        )
    except OSError as exc:
        return CheckOutcome(
            check.name,
            "tcp",
            False,
            check.critical,
            _ms(start),
            detail=f"{check.host}:{check.port} {exc.strerror or exc}",
        )


def _pg_probe(dsn: str, query: str, timeout: float) -> dict:
    import psycopg2  # imported lazily so tests without Postgres don't need it

    conn = psycopg2.connect(dsn, connect_timeout=max(1, int(timeout)))
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(f"SET statement_timeout = {int(timeout * 1000)}")
            cur.execute(query)
            cur.fetchall()
            cur.execute("SELECT pg_database_size(current_database()), count(*) FROM pg_stat_activity")
            size, conns = cur.fetchone()
        return {"db_size_bytes": int(size), "connections": int(conns)}
    finally:
        conn.close()


async def run_postgres(check: PostgresCheck, timeout: float) -> CheckOutcome:
    dsn = os.getenv(check.dsn_env, "")
    if not dsn:
        return CheckOutcome(check.name, "postgres", False, check.critical, detail=f"env {check.dsn_env} not set")
    start = time.perf_counter()
    try:
        info = await asyncio.wait_for(asyncio.to_thread(_pg_probe, dsn, check.query, timeout), timeout + 1)
    except TimeoutError:
        return CheckOutcome(check.name, "postgres", False, check.critical, _ms(start), detail="query timeout")
    except Exception as exc:  # psycopg2 raises many types
        msg = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        return CheckOutcome(check.name, "postgres", False, check.critical, _ms(start), detail=msg)
    return CheckOutcome(
        check.name,
        "postgres",
        True,
        check.critical,
        _ms(start),
        detail=f"{info['connections']} connections",
        extra=info,
    )


async def run_check(check: Check, client: httpx.AsyncClient, default_timeout: float) -> CheckOutcome:
    timeout = check.timeout_s or default_timeout
    try:
        if isinstance(check, HttpCheck):  # includes ApiCheck
            return await run_http(check, client, timeout)
        if isinstance(check, TcpCheck):
            return await run_tcp(check, timeout)
        if isinstance(check, PostgresCheck):
            return await run_postgres(check, timeout)
    except Exception as exc:  # defensive: a bug in a checker must not kill the loop
        return CheckOutcome(check.name, check.type, False, check.critical, detail=f"checker error: {exc}")
    return CheckOutcome(check.name, check.type, False, check.critical, detail="unknown check type")
