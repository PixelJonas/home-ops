from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from vehicle_pipeline.cost_store import CostValidationError, ReviewItemNotPendingError
from vehicle_pipeline.legacy_payload import to_cost_payload
from vehicle_pipeline.review_store import ReviewItem
from vehicle_pipeline.taxonomy import COST_CATEGORIES, COST_ENTITY

router = APIRouter()
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_review_store(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.review_store


def get_mygarage(request: Request):  # type: ignore[no-untyped-def]
    return getattr(request.app.state, "mygarage", None)


def get_cost_store(request: Request):  # type: ignore[no-untyped-def]
    return getattr(request.app.state, "cost_store", None)


def get_cost_sink(request: Request) -> str:
    return getattr(request.app.state, "cost_sink", "local")  # type: ignore[no-any-return]


@router.get("/review", response_class=HTMLResponse)
def list_review_items(request: Request, review_store=Depends(get_review_store)) -> HTMLResponse:  # noqa: B008
    items = review_store.list_pending()
    return _templates.TemplateResponse(request, "review_list.html", {"items": items})


@router.get("/review/{item_id}/edit", response_class=HTMLResponse)
def edit_review_item(
    request: Request,
    item_id: int,
    review_store=Depends(get_review_store),  # noqa: B008
    cost_sink: str = Depends(get_cost_sink),  # noqa: B008
) -> HTMLResponse:
    item = review_store.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="not found")
    return _render_edit(request, item, _display_payload(item, cost_sink), cost_sink)


@router.post("/review/{item_id}/approve", response_model=None)
async def approve_review_item(
    request: Request,
    item_id: int,
    review_store=Depends(get_review_store),  # noqa: B008
    mygarage=Depends(get_mygarage),  # noqa: B008
    cost_store=Depends(get_cost_store),  # noqa: B008
    cost_sink: str = Depends(get_cost_sink),  # noqa: B008
) -> RedirectResponse | HTMLResponse:
    item = review_store.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="not found")
    form = await request.form()

    if cost_sink == "mygarage":
        return await _approve_to_mygarage(item, form, review_store, mygarage)

    # Local sink. Legacy (MyGarage-shaped) pending items are flattened on
    # the fly; nothing is written to review_items unless the cost row is
    # inserted too (cost_store does both in one transaction), so a 422
    # leaves the item exactly as it was -- still pending.
    base = to_cost_payload(
        item.mygarage_entity, item.payload, vin=item.vin, extracted_category=item.extracted_category
    )
    payload = dict(base)
    for key, original in base.items():
        if key != "extra" and key in form:
            payload[key] = _coerce(form[key], type(original))
    try:
        cost_store.insert_from_review(item, payload)
    except CostValidationError as exc:
        return _render_edit(request, item, payload, cost_sink, error=str(exc), status_code=422)
    except ReviewItemNotPendingError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RedirectResponse(url="/review", status_code=303)


async def _approve_to_mygarage(item: ReviewItem, form: Any, review_store: Any, mygarage: Any) -> RedirectResponse:
    """COST_SINK=mygarage: the original write-through-to-MyGarage path."""
    if item.mygarage_entity == COST_ENTITY or mygarage is None:
        raise HTTPException(
            status_code=422,
            detail="item is in local-cost shape (or MyGarage is not configured); approve it with COST_SINK=local",
        )
    payload: dict[str, Any] = dict(item.payload)
    for key in payload:
        if key in form:
            payload[key] = _coerce(form[key], type(item.payload[key]))
    review_store.update_payload(item.id, payload)

    # For entity types whose payload has no "vin" key (e.g. "documents"),
    # the edit form renders a standalone vin field (see review_edit.html) —
    # read it directly from the submitted form rather than only from the
    # merged payload, so a human-supplied VIN on a vin-less draft actually
    # reaches MyGarage instead of falling through to "".
    submitted_vin = form.get("vin")
    vin = (str(submitted_vin) if submitted_vin else None) or payload.get("vin") or item.vin or ""
    result = await mygarage.create_record(vin, item.mygarage_entity, payload)
    review_store.mark_approved(item.id, str(result.get("id", "")))
    return RedirectResponse(url="/review", status_code=303)


@router.post("/review/{item_id}/reject")
def reject_review_item(item_id: int, review_store=Depends(get_review_store)) -> RedirectResponse:  # noqa: B008
    if review_store.get(item_id) is None:
        raise HTTPException(status_code=404, detail="not found")
    review_store.mark_rejected(item_id)
    return RedirectResponse(url="/review", status_code=303)


def _display_payload(item: ReviewItem, cost_sink: str) -> dict[str, Any]:
    if cost_sink == "mygarage":
        return dict(item.payload)
    return to_cost_payload(
        item.mygarage_entity, item.payload, vin=item.vin, extracted_category=item.extracted_category
    )


def _render_edit(
    request: Request,
    item: ReviewItem,
    payload: dict[str, Any],
    cost_sink: str,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    return _templates.TemplateResponse(
        request,
        "review_edit.html",
        {
            "item": item,
            "payload": payload,
            "cost_sink": cost_sink,
            "categories": COST_CATEGORIES,
            "error": error,
        },
        status_code=status_code,
    )


def _coerce(raw: Any, original_type: type) -> Any:
    if raw == "":
        return None
    if original_type is float:
        try:
            return float(raw)
        except ValueError:
            return raw
    if original_type is int:
        try:
            return int(float(raw))
        except ValueError:
            return raw
    return raw
