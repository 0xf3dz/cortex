import json
import time
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status

from app.config import Settings
from app.db import Database, InboundEvent
from app.webhook_security import has_valid_signature

router = APIRouter(prefix="/webhooks/whatsapp", tags=["whatsapp"])


@router.get("")
def verify_webhook(
    request: Request,
    mode: str | None = Query(default=None, alias="hub.mode"),
    verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
) -> Response:
    settings: Settings = request.app.state.settings
    if mode != "subscribe" or verify_token != settings.meta_verify_token or challenge is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Webhook verification failed")
    return Response(content=challenge, media_type="text/plain")


@router.post("")
async def receive_webhook(request: Request) -> Response:
    settings: Settings = request.app.state.settings
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > settings.webhook_max_body_bytes:
                raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Request body too large")
        except ValueError as error:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Content-Length") from error

    raw_body = await request.body()
    if len(raw_body) > settings.webhook_max_body_bytes:
        raise HTTPException(status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="Request body too large")
    if not has_valid_signature(
        raw_body,
        request.headers.get("x-hub-signature-256"),
        settings.meta_app_secret,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON") from error
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid webhook payload")

    database: Database = request.app.state.database
    received_at = int(time.time())
    for event in _iter_text_events(payload, received_at):
        if event.sender_wa_id == settings.allowed_whatsapp_wa_id:
            database.insert_inbound_event(event)

    return Response(status_code=status.HTTP_200_OK)



def _iter_text_events(payload: dict[str, Any], received_at: int) -> Iterator[InboundEvent]:
    entries = payload.get("entry")
    if not isinstance(entries, list):
        return

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            messages = value.get("messages")
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict) or message.get("type") != "text":
                    continue
                text = message.get("text")
                if not isinstance(text, dict) or not isinstance(text.get("body"), str):
                    continue
                wamid = message.get("id")
                sender_wa_id = message.get("from")
                timestamp_value = message.get("timestamp")
                if not isinstance(wamid, str) or not isinstance(sender_wa_id, str):
                    continue
                try:
                    whatsapp_timestamp = int(timestamp_value)
                except (TypeError, ValueError):
                    continue
                yield InboundEvent(
                    wamid=wamid,
                    sender_wa_id=sender_wa_id,
                    message_type="text",
                    body=text["body"],
                    whatsapp_timestamp=whatsapp_timestamp,
                    received_at=received_at,
                )
