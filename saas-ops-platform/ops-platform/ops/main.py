"""Application entry point: `uvicorn ops.main:app`."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from . import __version__
from .alerting import Alerter
from .api import router as api_router
from .config import PlatformConfig, load_config
from .db import Database
from .metrics import REGISTRY
from .monitor import Monitor
from .reports import update_slo_gauges
from .runtime import Runtime, make_runtime
from .scheduler import Scheduler
from .settings import Settings
from .web import router as web_router

log = logging.getLogger("ops")
HERE = Path(__file__).parent


class Platform:
    """Everything a request handler may need, hung off app.state.platform."""

    def __init__(
        self, settings: Settings, config: PlatformConfig, db: Database, runtime: Runtime, client: httpx.AsyncClient
    ) -> None:
        self.settings = settings
        self.config = config
        self.db = db
        self.runtime = runtime
        self.client = client
        self.alerter = Alerter(settings.alertmanager_url, settings.public_url, client)
        self.monitor = Monitor(config, db, runtime, self.alerter, client)
        self.scheduler = Scheduler(db, config, settings, client)

    @property
    def slo_cache(self) -> dict:
        return self.scheduler.slo_cache

    @slo_cache.setter
    def slo_cache(self, value: dict) -> None:
        self.scheduler.slo_cache = value


def create_app(
    settings: Settings | None = None,
    config: PlatformConfig | None = None,
    runtime: Runtime | None = None,
    start_background: bool = True,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or Settings()
    logging.basicConfig(level=settings.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per probe would flood the logs

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        cfg = config or load_config(settings.config_path)
        db = Database(settings.database_url)
        db.create_all()
        rt = runtime or make_runtime(settings.runtime)
        client = httpx.AsyncClient(
            headers={"User-Agent": f"ops-platform/{__version__}"}, follow_redirects=False, transport=http_transport
        )
        platform = Platform(settings, cfg, db, rt, client)
        app.state.platform = platform
        settings.effective_api_key()  # log the generated key early if none was configured
        platform.monitor.restore_state()
        platform.slo_cache = await asyncio.to_thread(update_slo_gauges, db, cfg)

        stop = asyncio.Event()
        tasks = []
        if start_background and settings.run_agent:
            tasks.append(asyncio.create_task(platform.monitor.run_forever(stop), name="monitor"))
            tasks.append(asyncio.create_task(platform.scheduler.run(stop), name="scheduler"))
        log.info("ops platform %s ready (%d services)", __version__, len(cfg.services))
        try:
            yield
        finally:
            stop.set()
            for t in tasks:
                try:
                    await asyncio.wait_for(t, timeout=10)
                except (TimeoutError, asyncio.CancelledError):
                    t.cancel()
            await client.aclose()
            db.engine.dispose()

    app = FastAPI(
        title="SaaS Ops Platform",
        version=__version__,
        description="Monitors simulated SaaS services, opens incidents, alerts and auto-remediates.",
        lifespan=lifespan,
    )
    app.include_router(api_router)
    app.include_router(web_router)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok", "version": __version__}

    @app.get("/readyz", include_in_schema=False)
    def readyz(request: Request):
        p = request.app.state.platform
        try:
            with p.db.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:
            return JSONResponse({"ready": False, "reason": f"database: {exc}"}, 503)
        return {"ready": True, "cycles": p.monitor.cycles}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
