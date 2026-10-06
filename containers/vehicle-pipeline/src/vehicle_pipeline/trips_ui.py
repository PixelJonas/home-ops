"""Trips page (business/private flag, purpose) and the Vollkostenrechnung
page. Every write goes through TripStore.annotate(via="ui") -- the same
single write path the notification handler uses."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from vehicle_pipeline.trips.store import TRIP_STATUSES, AnnotationError
from vehicle_pipeline.vollkosten import COST_CATEGORIES, PERIODS, load_vollkosten, report_tz

router = APIRouter()
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

BULK_ACTIONS = {"business": True, "private": False, "clear": None}


def _tz() -> ZoneInfo:
    return ZoneInfo(report_tz())


def _local(value: datetime | None, fmt: str = "%d.%m.%Y %H:%M") -> str:
    return value.astimezone(_tz()).strftime(fmt) if value is not None else "–"


_templates.env.filters["local"] = _local


def get_trip_store(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.trip_store


def get_vollkosten_loader(request: Request):  # type: ignore[no-untyped-def]
    pool = request.app.state.pool
    return lambda period, vehicle: load_vollkosten(pool, period, vehicle)


def month_bounds(month: str) -> tuple[datetime, datetime]:
    """'YYYY-MM' -> [first instant, first instant of next month) in the
    report timezone."""
    try:
        year, mon = (int(x) for x in month.split("-", 1))
        start = datetime(year, mon, 1, tzinfo=_tz())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="month must be YYYY-MM") from exc
    end = datetime(year + (mon == 12), mon % 12 + 1, 1, tzinfo=_tz())
    return start, end


def _safe_back(back: Any) -> str:
    """Redirect target after a POST: only our own pages (no open redirect)."""
    b = str(back or "")
    if b.startswith(("/trips", "/vollkosten")) and "//" not in b and "\\" not in b:
        return b
    return "/trips"


@router.get("/trips", response_class=HTMLResponse)
def list_trips(
    request: Request,
    vehicle: str = "",
    month: str = "",
    unflagged: str = "",
    status: str = "",
    trip: str = "",
    store=Depends(get_trip_store),  # noqa: B008
) -> HTMLResponse:
    if status and status not in TRIP_STATUSES:
        raise HTTPException(status_code=422, detail="unknown status")
    if trip:
        one = store.get_effective(trip)
        trips = [one] if one is not None else []
    else:
        start, end = month_bounds(month) if month else (None, None)
        trips = store.list_effective(
            vehicle=vehicle or None,
            month_start=start,
            month_end=end,
            unflagged_only=bool(unflagged),
            status=status or None,
        )
    query = {k: v for k, v in (("vehicle", vehicle), ("month", month), ("unflagged", unflagged), ("status", status), ("trip", trip)) if v}
    back = "/trips" + (f"?{urlencode(query)}" if query else "")
    return _templates.TemplateResponse(
        request,
        "trips_list.html",
        {
            "trips": trips,
            "vehicles": store.vehicle_ids(),
            "statuses": TRIP_STATUSES,
            "filters": {"vehicle": vehicle, "month": month, "unflagged": unflagged, "status": status, "trip": trip},
            "back": back,
        },
    )


@router.post("/trips/annotate", response_model=None)
async def bulk_annotate(request: Request, store=Depends(get_trip_store)) -> RedirectResponse:  # noqa: B008
    form = await request.form()
    keys = [str(k) for k in form.getlist("trip_key") if str(k)]
    action = str(form.get("action") or "")
    if action not in BULK_ACTIONS:
        raise HTTPException(status_code=422, detail="action must be business, private or clear")
    if keys:
        store.annotate(keys, business=BULK_ACTIONS[action], via="ui", actor="ui")
    return RedirectResponse(url=_safe_back(form.get("back")), status_code=303)


@router.post("/trips/purpose", response_model=None)
async def set_purpose(request: Request, store=Depends(get_trip_store)) -> RedirectResponse:  # noqa: B008
    form = await request.form()
    key = str(form.get("trip_key") or "")
    if not key:
        raise HTTPException(status_code=422, detail="trip_key missing")
    try:
        store.annotate(key, purpose=str(form.get("purpose") or "") or None, via="ui", actor="ui")
    except AnnotationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(url=_safe_back(form.get("back")), status_code=303)


@router.get("/vollkosten", response_class=HTMLResponse)
def vollkosten_page(
    request: Request,
    period: str = "yearly",
    vehicle: str = "",
    loader=Depends(get_vollkosten_loader),  # noqa: B008
) -> HTMLResponse:
    if period not in PERIODS:
        raise HTTPException(status_code=422, detail="period must be yearly or monthly")
    rows = loader(period, vehicle or None)
    return _templates.TemplateResponse(
        request,
        "vollkosten.html",
        {"rows": rows, "period": period, "vehicle": vehicle, "categories": COST_CATEGORIES},
    )
