"""Simulated SaaS service.

Runs as one of two roles, chosen with SERVICE_ROLE:
  api  - JSON REST API (orders, users)
  web  - server-rendered web app that calls the API upstream

Both expose /health, /metrics and /chaos/* so the ops platform can monitor
them and you can break them on purpose.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import psutil
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field

from . import __version__
from .chaos import ChaosState
from .logs import log, setup_logging

ROLE = os.getenv("SERVICE_ROLE", "api")
SERVICE = os.getenv("SERVICE_NAME", f"saas-{ROLE}")
API_URL = os.getenv("API_URL", "http://saas-api:8000")
CHAOS_TOKEN = os.getenv("CHAOS_TOKEN", "")
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
DISK_QUOTA_MB = int(os.getenv("DISK_QUOTA_MB", "512"))

logger = setup_logging(SERVICE)
chaos = ChaosState(data_dir=DATA_DIR)
started_at = time.time()
_proc = psutil.Process()
_proc.cpu_percent(None)  # prime the counter

REQUESTS = Counter("http_requests_total", "HTTP requests", ["service", "route", "method", "status"])
LATENCY = Histogram(
    "http_request_duration_seconds",
    "Request latency",
    ["service", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
CHAOS_ACTIVE = Gauge("sim_chaos_active", "Active chaos experiments", ["service", "kind"])
UP_SINCE = Gauge("sim_start_time_seconds", "Process start time", ["service"])
UP_SINCE.labels(SERVICE).set(started_at)

OPS_PATHS = ("/health", "/ready", "/metrics", "/chaos")

# Realistic, varied error messages. The numbers change every time, which is
# exactly what the platform's log fingerprinting has to cope with.
ERROR_TEMPLATES = {
    "api": [
        "database timeout after {ms}ms on query orders_by_user user_id={id}",
        "connection pool exhausted (size=20, waiting={n})",
        "payment provider returned 502 for charge ch_{hex}",
        "unhandled exception in handler: KeyError 'sku_{id}'",
    ],
    "web": [
        "upstream api request failed: status={status} path=/api/v1/orders",
        "template render error in dashboard.html line {n}",
        "session store unavailable: redis timeout after {ms}ms",
    ],
}


def _error_message() -> str:
    tpl = random.choice(ERROR_TEMPLATES.get(ROLE, ERROR_TEMPLATES["api"]))
    return tpl.format(
        ms=random.randint(1000, 9000),
        id=random.randint(1000, 99999),
        n=random.randint(1, 50),
        hex=secrets.token_hex(6),
        status=random.choice([500, 502, 503, 504]),
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 - once, at startup
    log(logger, logging.INFO, "service starting", role=ROLE, version=__version__)
    app.state.http = httpx.AsyncClient(timeout=3.0)
    yield
    chaos.stop_cpu()
    await app.state.http.aclose()
    log(logger, logging.INFO, "service stopping")


app = FastAPI(title=f"Simulated {ROLE}", version=__version__, lifespan=lifespan)


# --------------------------------------------------------------------------
# middleware: latency, metrics, access logs, injected faults
# --------------------------------------------------------------------------
def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    if route is not None:
        return route.path
    # unmatched or short-circuited requests: collapse ids to keep label cardinality bounded
    return re.sub(r"/\d+", "/{id}", request.url.path)


@app.middleware("http")
async def instrument(request: Request, call_next):
    path = request.url.path
    is_ops = path.startswith(OPS_PATHS)
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    start = time.perf_counter()

    if not is_ops:
        delay = chaos.active_latency()
        if delay:
            # jitter keeps the latency histogram realistic
            await asyncio.sleep(delay / 1000 * random.uniform(0.7, 1.3))
        if random.random() < chaos.active_error_rate():
            response = JSONResponse({"error": "internal error", "request_id": request_id}, 500)
            log(logger, logging.ERROR, _error_message(), request_id=request_id, route=path)
        else:
            response = await call_next(request)
    else:
        response = await call_next(request)

    elapsed = time.perf_counter() - start
    route = _route_label(request)
    REQUESTS.labels(SERVICE, route, request.method, str(response.status_code)).inc()
    LATENCY.labels(SERVICE, route).observe(elapsed)
    response.headers["x-request-id"] = request_id

    if not is_ops:
        level = logging.WARNING if response.status_code >= 400 else logging.INFO
        if response.status_code >= 500:
            level = logging.ERROR
        log(
            logger,
            level,
            "request",
            method=request.method,
            route=route,
            status=response.status_code,
            duration_ms=round(elapsed * 1000, 1),
            request_id=request_id,
        )
    return response


# --------------------------------------------------------------------------
# ops endpoints
# --------------------------------------------------------------------------
_children: dict[int, psutil.Process] = {}


def _resources() -> dict:
    """Process-tree usage, so chaos workers (child processes) are counted like a container would."""
    cpu = _proc.cpu_percent(None)
    mem = _proc.memory_info().rss
    live = set()
    for child in _proc.children(recursive=True):
        proc = _children.setdefault(child.pid, child)  # keep the object so cpu_percent has a baseline
        live.add(child.pid)
        try:
            cpu += proc.cpu_percent(None)
            mem += proc.memory_info().rss
        except psutil.Error:
            pass
    for pid in set(_children) - live:
        del _children[pid]
    disk_used = sum(f.stat().st_size for f in DATA_DIR.rglob("*") if f.is_file())
    fs = shutil.disk_usage(DATA_DIR) if DATA_DIR.exists() else None
    return {
        "cpu_percent": round(cpu, 1),
        "memory_rss_bytes": mem,
        "memory_limit_bytes": _cgroup_memory_limit(),
        "disk_used_bytes": disk_used,
        "disk_quota_bytes": DISK_QUOTA_MB * 1024 * 1024,
        "fs_free_bytes": fs.free if fs else None,
    }


def _cgroup_memory_limit() -> int | None:
    """The container's memory limit (cgroup v2, then v1), or None when unlimited."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = Path(path).read_text().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 60:
            return int(raw)
    return None


@app.get("/health")
async def health():
    if chaos.hanging:
        # simulate a deadlocked process: the socket accepts, nothing answers
        await asyncio.sleep(3600)
    status_code = 503 if chaos.is_unhealthy() else 200
    body = {
        "status": "unhealthy" if status_code == 503 else "ok",
        "service": SERVICE,
        "role": ROLE,
        "version": __version__,
        "uptime_s": round(time.time() - started_at, 1),
        "resources": _resources(),
        "chaos": chaos.snapshot(),
    }
    return JSONResponse(body, status_code=status_code)


@app.get("/ready")
async def ready():
    return {"ready": not chaos.hanging}


@app.get("/metrics")
async def metrics():
    snap = chaos.snapshot()
    CHAOS_ACTIVE.labels(SERVICE, "latency").set(1 if snap["latency_ms"] else 0)
    CHAOS_ACTIVE.labels(SERVICE, "errors").set(1 if snap["error_rate"] else 0)
    CHAOS_ACTIVE.labels(SERVICE, "cpu").set(1 if snap["cpu_burn_remaining_s"] else 0)
    CHAOS_ACTIVE.labels(SERVICE, "memory").set(snap["memory_held_mb"])
    CHAOS_ACTIVE.labels(SERVICE, "disk").set(snap["disk_fill_mb"])
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# --------------------------------------------------------------------------
# chaos endpoints
# --------------------------------------------------------------------------
def require_chaos_token(x_chaos_token: str | None = Header(default=None)) -> None:
    if CHAOS_TOKEN and not secrets.compare_digest(x_chaos_token or "", CHAOS_TOKEN):
        raise HTTPException(status_code=401, detail="invalid chaos token")


class Timed(BaseModel):
    seconds: int = Field(default=120, ge=1, le=3600)


class LatencyReq(Timed):
    ms: int = Field(default=800, ge=1, le=30000)


class ErrorsReq(Timed):
    rate: float = Field(default=0.5, ge=0, le=1)


class CpuReq(Timed):
    workers: int = Field(default=1, ge=1, le=8)


class SizeReq(BaseModel):
    mb: int = Field(default=200, ge=1, le=4096)


chaos_deps = [Depends(require_chaos_token)]


@app.get("/chaos", dependencies=chaos_deps)
async def chaos_status():
    return chaos.snapshot()


@app.post("/chaos/latency", dependencies=chaos_deps)
async def chaos_latency(req: LatencyReq):
    chaos.set_latency(req.ms, req.seconds)
    log(logger, logging.WARNING, "chaos: latency injected", ms=req.ms, seconds=req.seconds)
    return chaos.snapshot()


@app.post("/chaos/errors", dependencies=chaos_deps)
async def chaos_errors(req: ErrorsReq):
    chaos.set_errors(req.rate, req.seconds)
    log(logger, logging.WARNING, "chaos: error injection", rate=req.rate, seconds=req.seconds)
    return chaos.snapshot()


@app.post("/chaos/unhealthy", dependencies=chaos_deps)
async def chaos_unhealthy(req: Timed):
    chaos.set_unhealthy(req.seconds)
    log(logger, logging.WARNING, "chaos: health check forced to fail", seconds=req.seconds)
    return chaos.snapshot()


@app.post("/chaos/cpu", dependencies=chaos_deps)
async def chaos_cpu(req: CpuReq):
    chaos.burn_cpu(req.seconds, req.workers)
    log(logger, logging.WARNING, "chaos: cpu burn", workers=req.workers, seconds=req.seconds)
    return chaos.snapshot()


@app.post("/chaos/memory", dependencies=chaos_deps)
async def chaos_memory(req: SizeReq):
    await asyncio.to_thread(chaos.hold_memory, req.mb)
    log(logger, logging.WARNING, "chaos: memory leak", mb=req.mb, held_mb=chaos.memory_mb())
    return chaos.snapshot()


@app.post("/chaos/disk", dependencies=chaos_deps)
async def chaos_disk(req: SizeReq):
    path = await asyncio.to_thread(chaos.fill_disk, req.mb)
    log(logger, logging.WARNING, "chaos: disk fill", mb=req.mb, file=str(path))
    return chaos.snapshot()


@app.post("/chaos/hang", dependencies=chaos_deps)
async def chaos_hang():
    chaos.hanging = True
    log(logger, logging.ERROR, "chaos: worker deadlock simulated, health endpoint will hang")
    return {"hanging": True}


@app.post("/chaos/crash", dependencies=chaos_deps)
async def chaos_crash():
    log(logger, logging.CRITICAL, "chaos: fatal error, process exiting with code 1")

    async def _die():
        await asyncio.sleep(0.3)
        os._exit(1)

    asyncio.get_running_loop().create_task(_die())
    return {"crashing": True}


@app.post("/chaos/reset", dependencies=chaos_deps)
async def chaos_reset():
    chaos.reset()
    log(logger, logging.INFO, "chaos: all experiments reset")
    return chaos.snapshot()


# --------------------------------------------------------------------------
# business endpoints
# --------------------------------------------------------------------------
_ORDERS = {
    i: {"id": i, "sku": f"plan-{random.choice(['starter', 'team', 'scale'])}", "amount": random.randint(9, 499)}
    for i in range(1, 51)
}

if ROLE == "api":

    class NewOrder(BaseModel):
        sku: str
        amount: int = Field(gt=0)

    @app.get("/api/v1/orders")
    async def list_orders(limit: int = 20):
        await asyncio.sleep(random.uniform(0.005, 0.04))
        return {"orders": list(_ORDERS.values())[:limit], "total": len(_ORDERS)}

    @app.get("/api/v1/orders/{order_id}")
    async def get_order(order_id: int):
        await asyncio.sleep(random.uniform(0.002, 0.02))
        if order_id not in _ORDERS:
            raise HTTPException(404, "order not found")
        return _ORDERS[order_id]

    @app.post("/api/v1/orders", status_code=201)
    async def create_order(order: NewOrder):
        await asyncio.sleep(random.uniform(0.02, 0.08))
        oid = max(_ORDERS) + 1
        _ORDERS[oid] = {"id": oid, **order.model_dump()}
        return _ORDERS[oid]

    @app.get("/api/v1/users/me")
    async def me():
        return {"id": 42, "email": "demo@example.com", "plan": "team"}

else:
    PAGE = """<!doctype html><html><head><title>{title}</title></head>
<body style="font-family:system-ui;max-width:640px;margin:40px auto">
<h1>{title}</h1>{body}</body></html>"""

    @app.get("/", response_class=HTMLResponse)
    async def home():
        return PAGE.format(title="Acme Cloud", body="<p>Welcome to the simulated web app.</p>")

    @app.get("/pricing", response_class=HTMLResponse)
    async def pricing():
        return PAGE.format(title="Pricing", body="<ul><li>Starter</li><li>Team</li><li>Scale</li></ul>")

    @app.get("/login", response_class=HTMLResponse)
    async def login():
        return PAGE.format(title="Sign in", body="<form><input name=email><button>Go</button></form>")

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        """Calls the API upstream, so an API outage shows up here too."""
        try:
            r = await request.app.state.http.get(f"{API_URL}/api/v1/orders", params={"limit": 5})
            r.raise_for_status()
            rows = "".join(f"<li>#{o['id']} {o['sku']} ${o['amount']}</li>" for o in r.json()["orders"])
        except httpx.HTTPStatusError as exc:
            log(
                logger,
                logging.ERROR,
                f"upstream api request failed: status={exc.response.status_code} path=/api/v1/orders",
            )
            raise HTTPException(502, "upstream error") from exc
        except httpx.HTTPError as exc:
            log(logger, logging.ERROR, f"upstream api unreachable: {type(exc).__name__}")
            raise HTTPException(502, "upstream unreachable") from exc
        return PAGE.format(title="Dashboard", body=f"<ul>{rows}</ul>")
