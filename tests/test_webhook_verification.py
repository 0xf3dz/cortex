from fastapi.testclient import TestClient

from tests.conftest import VERIFY_TOKEN


def test_correct_verification_token_returns_raw_challenge(client: TestClient) -> None:
    response = client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "74628364",
        },
    )

    assert response.status_code == 200
    assert response.text == "74628364"
    assert response.headers["content-type"].startswith("text/plain")


def test_incorrect_verification_token_is_rejected(client: TestClient) -> None:
    response = client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "incorrect-token-value",
            "hub.challenge": "74628364",
        },
    )

    assert response.status_code == 403
