import asyncio
import json

import httpx
import pytest

from app.whatsapp_client import GraphWhatsAppClient, WhatsAppClientError


def test_graph_client_sends_contextual_text_payload() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.OUTBOUND"}]})

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
            client = GraphWhatsAppClient(
                access_token="test-access-token",
                phone_number_id="1228458710361487",
                graph_api_version="v25.0",
                http_client=http_client,
            )
            await client.send_text(
                "61400000000",
                "Echo: test message",
                context_message_id="wamid.INBOUND",
            )

    asyncio.run(exercise())

    assert len(requests) == 1
    request = requests[0]
    assert request.url == "https://graph.facebook.com/v25.0/1228458710361487/messages"
    assert request.headers["authorization"] == "Bearer test-access-token"
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "61400000000",
        "type": "text",
        "text": {"preview_url": False, "body": "Echo: test message"},
        "context": {"message_id": "wamid.INBOUND"},
    }


def test_graph_client_error_excludes_response_and_credentials() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json={"error": {"message": "private message body test-access-token"}},
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
            client = GraphWhatsAppClient(
                access_token="test-access-token",
                phone_number_id="1228458710361487",
                graph_api_version="v25.0",
                http_client=http_client,
            )
            await client.send_text("61400000000", "private message body")

    with pytest.raises(WhatsAppClientError) as captured:
        asyncio.run(exercise())

    assert captured.value.status_code == 500
    assert "test-access-token" not in str(captured.value)
    assert "private message body" not in str(captured.value)
