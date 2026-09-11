from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

router = APIRouter()
_templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_review_store(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.review_store


def get_mygarage(request: Request):  # type: ignore[no-untyped-def]
    return request.app.state.mygarage


@router.get("/review", response_class=HTMLResponse)
def list_review_items(request: Request, review_store=Depends(get_review_store)) -> HTMLResponse:  # noqa: B008
    items = review_store.list_pending()
    return _templates.TemplateResponse(request, "review_list.html", {"items": items})


@router.get("/review/{item_id}/edit", response_class=HTMLResponse)
def edit_review_item(
    request: Request, item_id: int, review_store=Depends(get_review_store)  # noqa: B008
) -> HTMLResponse:
    item = review_store.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="not found")
    return _templates.TemplateResponse(request, "review_edit.html", {"item": item})


@router.post("/review/{item_id}/approve")
async def approve_review_item(
    request: Request,
    item_id: int,
    review_store=Depends(get_review_store),  # noqa: B008
    mygarage=Depends(get_mygarage),  # noqa: B008
) -> RedirectResponse:
    item = review_store.get(item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="not found")

    form = await request.form()
    payload: dict[str, Any] = dict(item.payload)
    for key in payload:
        if key in form:
            payload[key] = _coerce(form[key], type(item.payload[key]))
    review_store.update_payload(item_id, payload)

    result = await mygarage.create_record(item.vin or "", item.mygarage_entity, payload)
    review_store.mark_approved(item_id, str(result.get("id", "")))
    return RedirectResponse(url="/review", status_code=303)


@router.post("/review/{item_id}/reject")
def reject_review_item(item_id: int, review_store=Depends(get_review_store)) -> RedirectResponse:  # noqa: B008
    if review_store.get(item_id) is None:
        raise HTTPException(status_code=404, detail="not found")
    review_store.mark_rejected(item_id)
    return RedirectResponse(url="/review", status_code=303)


def _coerce(raw: Any, original_type: type) -> Any:
    if raw == "":
        return None
    if original_type is float:
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw
