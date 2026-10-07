"""Server-rendered dashboard pages. Live pages poll the JSON API from static/app.js."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select

from .api import get_incident, list_incidents
from .models import Report

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def _fmt_dur(s) -> str:
    if s is None:
        return "n/a"
    s = int(s)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {s % 3600 // 60}m"


def _pct(x, digits=2) -> str:
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


templates.env.filters["dur"] = _fmt_dur
templates.env.filters["pct"] = _pct


def _ctx(request: Request, page: str, **kw) -> dict:
    p = request.app.state.platform
    return {"request": request, "page": page, "grafana": p.settings.grafana_url, "services": p.config.services, **kw}


@router.get("/", response_class=HTMLResponse)
def overview_page(request: Request):
    return templates.TemplateResponse(request, "overview.html", _ctx(request, "overview"))


@router.get("/services/{name}", response_class=HTMLResponse)
def service_page(name: str, request: Request):
    svc = request.app.state.platform.config.service(name)
    if svc is None:
        raise HTTPException(404, "unknown service")
    return templates.TemplateResponse(request, "service.html", _ctx(request, "service", svc=svc))


@router.get("/incidents", response_class=HTMLResponse)
def incidents_page(request: Request, status: str = "all", service: str | None = None):
    status = status if status in ("open", "resolved", "all") else "all"
    rows = list_incidents(request, status=status, service=service, severity=None, limit=200, offset=0)
    return templates.TemplateResponse(
        request, "incidents.html", _ctx(request, "incidents", rows=rows, status=status, service=service)
    )


@router.get("/incidents/{incident_id}", response_class=HTMLResponse)
def incident_page(incident_id: int, request: Request):
    inc = get_incident(incident_id, request)
    return templates.TemplateResponse(request, "incident.html", _ctx(request, "incidents", inc=inc))


@router.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request):
    with request.app.state.platform.db.session() as s:
        rows = [r.data for r in s.scalars(select(Report).order_by(desc(Report.day)).limit(60)).all()]
    return templates.TemplateResponse(request, "reports.html", _ctx(request, "reports", rows=rows))


@router.get("/reports/{day}", response_class=HTMLResponse)
def report_page(day: date, request: Request):
    with request.app.state.platform.db.session() as s:
        r = s.scalar(select(Report).where(Report.day == day))
        if r is None:
            raise HTTPException(404, "no report for that day")
        data = r.data
    return templates.TemplateResponse(request, "report.html", _ctx(request, "reports", r=data))


@router.get("/chaos", response_class=HTMLResponse)
def chaos_page(request: Request):
    return templates.TemplateResponse(request, "chaos.html", _ctx(request, "chaos"))
