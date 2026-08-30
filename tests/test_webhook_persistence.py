import json
from collections.abc import Callable

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.whatsapp_client import FakeWhatsAppClient


def test_non_allowlisted_sender_creates_no_event(
    client: TestClient,
    fixture_body: bytes,
    sign: Callable[[bytes], dict[str, str]],
) -> None:
    payload = json.loads(fixture_body)
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["from"] = "61499999999"
    blocked_body = json.dumps(payload, separators=(",", ":")).encode()

    response = client.post(
        "/webhooks/whatsapp",
        content=blocked_body,
        headers=sign(blocked_body),
    )

    assert response.status_code == 200
    assert client.app.state.database.count_inbound_events() == 0


def test_process_restart_retains_pending_event(
    settings: Settings,
    fixture_body: bytes,
    sign: Callable[[bytes], dict[str, str]],
) -> None:
    with TestClient(create_app(settings, FakeWhatsAppClient())) as first_client:
        response = first_client.post(
            "/webhooks/whatsapp",
            content=fixture_body,
            headers=sign(fixture_body),
        )
        assert response.status_code == 200

    with TestClient(create_app(settings, FakeWhatsAppClient())) as restarted_client:
        event = restarted_client.app.state.database.get_inbound_event("wamid.TEST_MESSAGE_001")

    assert event is not None
    assert event["body"] == "A durable test note"
    assert event["processing_state"] == "pending"
