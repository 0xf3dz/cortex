from dataclasses import dataclass
from typing import Any, Protocol

import httpx


@dataclass(frozen=True, slots=True)
class SentMessage:
    recipient_wa_id: str
    body: str
    context_message_id: str | None = None

@dataclass(frozen=True, slots=True)
class SentReaction:
    recipient_wa_id: str
    message_id: str
    emoji: str


class WhatsAppClientError(RuntimeError):
    def __init__(self, status_code: int | None) -> None:
        category = f"HTTP {status_code}" if status_code is not None else "transport"
        super().__init__(f"WhatsApp Graph API request failed: {category}")
        self.status_code = status_code


class WhatsAppClient(Protocol):
    async def send_text(
        self,
        recipient_wa_id: str,
        body: str,
        *,
        context_message_id: str | None = None,
    ) -> None: ...

    async def send_reaction(
        self,
        recipient_wa_id: str,
        message_id: str,
        emoji: str,
    ) -> None: ...


class GraphWhatsAppClient:
    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        graph_api_version: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._access_token = access_token
        self._messages_url = (
            f"https://graph.facebook.com/{graph_api_version}/{phone_number_id}/messages"
        )
        self._http_client = http_client or httpx.AsyncClient(timeout=10.0)
        self._owns_http_client = http_client is None

    async def send_text(
        self,
        recipient_wa_id: str,
        body: str,
        *,
        context_message_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient_wa_id,
            "type": "text",
            "text": {
                "preview_url": False,
                "body": body,
            },
        }
        if context_message_id is not None:
            payload["context"] = {"message_id": context_message_id}
        await self._send(payload)

    async def send_reaction(
        self,
        recipient_wa_id: str,
        message_id: str,
        emoji: str,
    ) -> None:
        await self._send(
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": recipient_wa_id,
                "type": "reaction",
                "reaction": {
                    "message_id": message_id,
                    "emoji": emoji,
                },
            }
        )

    async def _send(self, payload: dict[str, Any]) -> None:
        try:
            response = await self._http_client.post(
                self._messages_url,
                headers={"Authorization": f"Bearer {self._access_token}"},
                json=payload,
            )
        except httpx.HTTPError as error:
            raise WhatsAppClientError(status_code=None) from error

        if not response.is_success:
            raise WhatsAppClientError(status_code=response.status_code)

    async def aclose(self) -> None:
        if self._owns_http_client:
            await self._http_client.aclose()


class FakeWhatsAppClient:
    def __init__(self) -> None:
        self.sent_messages: list[SentMessage] = []
        self.sent_reactions: list[SentReaction] = []

    async def send_text(
        self,
        recipient_wa_id: str,
        body: str,
        *,
        context_message_id: str | None = None,
    ) -> None:
        self.sent_messages.append(
            SentMessage(
                recipient_wa_id=recipient_wa_id,
                body=body,
                context_message_id=context_message_id,
            )
        )

    async def send_reaction(
        self,
        recipient_wa_id: str,
        message_id: str,
        emoji: str,
    ) -> None:
        self.sent_reactions.append(
            SentReaction(
                recipient_wa_id=recipient_wa_id,
                message_id=message_id,
                emoji=emoji,
            )
        )
