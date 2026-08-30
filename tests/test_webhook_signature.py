from collections.abc import Callable

from fastapi.testclient import TestClient


def test_correctly_signed_fixture_is_accepted(
    client: TestClient,
    fixture_body: bytes,
    sign: Callable[[bytes], dict[str, str]],
) -> None:
    response = client.post(
        "/webhooks/whatsapp",
        content=fixture_body,
        headers=sign(fixture_body),
    )

    assert response.status_code == 200
    assert client.app.state.database.count_inbound_events() == 1


def test_invalid_signature_is_rejected_before_json_parsing(client: TestClient) -> None:
    response = client.post(
        "/webhooks/whatsapp",
        content=b"not-json",
        headers={"x-hub-signature-256": "sha256=" + ("0" * 64)},
    )

    assert response.status_code == 401
    assert client.app.state.database.count_inbound_events() == 0
