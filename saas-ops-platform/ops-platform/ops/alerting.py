"""Alert delivery through Alertmanager.

The platform pushes its own incidents to Alertmanager's v2 API, so routing,
grouping, silencing and receivers live in one place alongside Prometheus's
alert rules. Open incidents are re-pushed every cycle (Alertmanager expires
alerts that stop being refreshed); resolution sends endsAt=now.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx

from .incidents import IncidentChange
from .metrics import ALERTS_SENT
from .models import Incident

log = logging.getLogger("ops.alerting")


def _iso(dt: datetime) -> str:
    return dt.replace(tzinfo=UTC).isoformat().replace("+00:00", "Z")


class Alerter:
    def __init__(self, alertmanager_url: str, public_url: str, client: httpx.AsyncClient) -> None:
        self.url = alertmanager_url.rstrip("/")
        self.public_url = public_url.rstrip("/")
        self.client = client

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def _payload(
        self,
        *,
        incident_id: int,
        service: str,
        key: str,
        kind: str,
        severity: str,
        title: str,
        detail: str,
        starts_at: datetime,
        ends_at: datetime | None,
        hint: str | None = None,
    ) -> dict:
        alert = {
            "labels": {
                "alertname": f"Ops{kind.title()}",
                "service": service,
                "severity": severity,
                "source": "ops-platform",
                "incident_key": key,
            },
            "annotations": {
                "summary": title,
                "description": detail or title,
                "incident_id": str(incident_id),
                "runbook_url": f"{self.public_url}/incidents/{incident_id}",
            },
            "startsAt": _iso(starts_at),
            "generatorURL": f"{self.public_url}/incidents/{incident_id}",
        }
        if hint:
            alert["annotations"]["hint"] = hint
        if ends_at:
            alert["endsAt"] = _iso(ends_at)
        return alert

    async def _post(self, alerts: list[dict]) -> bool:
        if not self.enabled or not alerts:
            return False
        try:
            r = await self.client.post(f"{self.url}/api/v2/alerts", json=alerts, timeout=5)
            r.raise_for_status()
            return True
        except httpx.HTTPError as exc:
            log.warning("Alertmanager push failed: %s", exc)
            return False

    async def refresh(self, incidents: list[Incident]) -> None:
        """Re-assert every open incident; Alertmanager dedupes on labels."""
        alerts = [
            self._payload(
                incident_id=i.id,
                service=i.service,
                key=i.key,
                kind=i.kind,
                severity=i.severity,
                title=i.title,
                detail=i.title,
                starts_at=i.opened_at,
                # if the platform dies, alerts auto-expire after 5 minutes instead of firing forever
                ends_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5),
                hint=i.root_cause_hint,
            )
            for i in incidents
        ]
        await self._post(alerts)

    async def notify(self, changes: list[IncidentChange]) -> list[IncidentChange]:
        """Send opened/escalated/resolved changes. Returns the changes that were delivered."""
        if not changes:
            return []
        now = datetime.now(UTC).replace(tzinfo=None)
        alerts = []
        for c in changes:
            starts = datetime.fromisoformat(c.opened_at.rstrip("Z"))
            alerts.append(
                self._payload(
                    incident_id=c.incident_id,
                    service=c.service,
                    key=c.key,
                    kind=c.kind,
                    severity=c.severity,
                    title=c.title,
                    detail=c.detail,
                    starts_at=starts,
                    ends_at=now if c.change == "resolved" else None,
                )
            )
        for c in changes:
            log.info("incident %s %s [%s] %s: %s", c.incident_id, c.change, c.severity, c.service, c.title)
        if await self._post(alerts):
            for c in changes:
                ALERTS_SENT.labels(c.service, "resolved" if c.change == "resolved" else "firing").inc()
            return changes
        return []
