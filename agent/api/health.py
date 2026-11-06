"""Health and run-completion routes."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ..completion import handle_run_completion, verify_run_complete_token
from ..dashboard.oauth import require_session

router = APIRouter()
_SESSION_DEP = Depends(require_session)


@router.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "healthy"}


@router.post("/webhooks/run-complete")
async def run_complete_webhook(request: Request) -> dict[str, Any]:
    if not verify_run_complete_token(request.query_params.get("token")):
        raise HTTPException(status_code=401, detail="Invalid run-complete token")
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return {"status": "error", "message": "Invalid JSON"}
    if not isinstance(payload, dict):
        return {"status": "ignored", "reason": "payload not an object"}
    return await handle_run_completion(payload)


@router.get("/debug/queue-probe")
async def debug_queue_probe_snapshot(
    session: dict[str, Any] = _SESSION_DEP,
) -> dict[str, Any]:
    del session
    from ..utils.queue_debug import snapshot

    return snapshot()


@router.post("/debug/queue-probe")
async def debug_queue_probe_toggle(
    request: Request,
    session: dict[str, Any] = _SESSION_DEP,
) -> dict[str, Any]:
    del session
    from ..utils.queue_debug import is_enabled, set_enabled, snapshot

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="JSON body required") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
        raise HTTPException(status_code=400, detail='body must be {"enabled": bool}')
    set_enabled(payload["enabled"])
    return {"enabled": is_enabled(), **snapshot()}
