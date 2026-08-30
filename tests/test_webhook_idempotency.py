from collections.abc import Callable

from fastapi.testclient import TestClient


def test_repeated_wamid_creates_one_pending_event(
    client: TestClient,
    fixture_body: bytes,
    sign: Callable[[bytes], dict[str, str]],
) -> None:
    headers = sign(fixture_body)

    first_response = client.post("/webhooks/whatsapp", content=fixture_body, headers=headers)
    second_response = client.post("/webhooks/whatsapp", content=fixture_body, headers=headers)

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert client.app.state.database.count_inbound_events() == 1
    event = client.app.state.database.get_inbound_event("wamid.TEST_MESSAGE_001")
    assert event is not None
    assert event["processing_state"] == "pending"
