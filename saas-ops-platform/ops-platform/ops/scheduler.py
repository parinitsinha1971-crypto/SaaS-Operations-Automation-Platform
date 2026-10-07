"""Background jobs: daily report, SLO gauges, data retention."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta

import httpx
from sqlalchemy import delete, select

from .config import PlatformConfig
from .db import Database, utcnow
from .models import AlertRecord, CheckResult, Report, ServiceSample
from .reports import build_daily_report, deliver_report, save_report, update_slo_gauges
from .settings import Settings

log = logging.getLogger("ops.scheduler")


def seconds_until(hhmm: str, now: datetime) -> float:
    hour, minute = (int(x) for x in hhmm.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def generate_report(db: Database, config: PlatformConfig, day: date) -> dict:
    data = build_daily_report(db, config, day)
    save_report(db, data)
    log.info("daily report for %s generated: fleet availability %s", day, data["fleet_availability"])
    return data


def prune(db: Database, retention_days: int) -> dict[str, int]:
    cutoff = utcnow() - timedelta(days=retention_days)
    out = {}
    with db.session() as s:
        for model in (CheckResult, ServiceSample):
            out[model.__tablename__] = s.execute(delete(model).where(model.ts < cutoff)).rowcount
        out["alerts"] = s.execute(delete(AlertRecord).where(AlertRecord.received_at < cutoff)).rowcount
    return out


class Scheduler:
    def __init__(self, db: Database, config: PlatformConfig, settings: Settings, client: httpx.AsyncClient):
        self.db, self.config, self.settings, self.client = db, config, settings, client
        self.slo_cache: dict = {}

    async def run(self, stop: asyncio.Event) -> None:
        await asyncio.gather(self._daily(stop), self._slo(stop))

    async def _sleep(self, stop: asyncio.Event, seconds: float) -> bool:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    async def _daily(self, stop: asyncio.Event) -> None:
        # catch up: if yesterday's report is missing (platform was down at report time), make it now
        yesterday = utcnow().date() - timedelta(days=1)
        exists = await asyncio.to_thread(self._has_report, yesterday)
        if not exists and await asyncio.to_thread(self._has_samples, yesterday):
            await self._report(yesterday)
        while True:
            wait = seconds_until(self.settings.report_time_utc, utcnow())
            log.info("next daily report in %.0f minutes", wait / 60)
            if await self._sleep(stop, wait):
                return
            await self._report(utcnow().date() - timedelta(days=1))
            removed = await asyncio.to_thread(prune, self.db, self.settings.retention_days)
            log.info("retention: removed %s", removed)

    async def _report(self, day: date) -> None:
        try:
            data = await asyncio.to_thread(generate_report, self.db, self.config, day)
            await deliver_report(self.settings.report_webhook_url, data, self.settings.public_url, self.client)
        except Exception:
            log.exception("daily report failed for %s", day)

    async def _slo(self, stop: asyncio.Event) -> None:
        while True:
            try:
                self.slo_cache = await asyncio.to_thread(update_slo_gauges, self.db, self.config)
            except Exception:
                log.exception("SLO gauge update failed")
            if await self._sleep(stop, 60):
                return

    def _has_report(self, day: date) -> bool:
        with self.db.session() as s:
            return s.scalar(select(Report.id).where(Report.day == day)) is not None

    def _has_samples(self, day: date) -> bool:
        start = datetime.combine(day, datetime.min.time())
        with self.db.session() as s:
            return (
                s.scalar(
                    select(ServiceSample.id)
                    .where(ServiceSample.ts >= start, ServiceSample.ts < start + timedelta(days=1))
                    .limit(1)
                )
                is not None
            )
