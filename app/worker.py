import asyncio
import json
import logging
import time
from datetime import datetime
from time import monotonic
from zoneinfo import ZoneInfo

from app.db import Database, InboundEvent, Note
from app.embedding import EmbeddingEncoder
from app.link_enrichment import (
    LinkEnricher,
    LinkEnrichmentError,
    SecureLinkEnricher,
    extract_urls,
)
from app.search import SearchService
from app.whatsapp_client import WhatsAppClient, WhatsAppClientError


logger = logging.getLogger(__name__)
_HELP_TEXT = (
    "Send text to save a note.\n"
    "Start a message with ? to search.\n"
    "Use /delete-last to delete your latest note."
)


class EventWorker:
    def __init__(
        self,
        *,
        database: Database,
        whatsapp_client: WhatsAppClient,
        embedding_encoder: EmbeddingEncoder,
        user_timezone: str,
        link_enricher: LinkEnricher | None = None,
        poll_interval_seconds: float = 0.25,
        stale_after_seconds: int = 300,
    ) -> None:
        self._database = database
        self._whatsapp_client = whatsapp_client
        self._embedding_encoder = embedding_encoder
        self._search = SearchService(database, embedding_encoder)
        self._link_enricher = link_enricher or SecureLinkEnricher()
        self._user_timezone = ZoneInfo(user_timezone)
        self._poll_interval_seconds = poll_interval_seconds
        self._stale_after_seconds = stale_after_seconds
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        await asyncio.to_thread(
            self._database.recover_stale_events,
            int(time.time()) - self._stale_after_seconds,
        )
        await asyncio.to_thread(
            self._database.recover_stale_enrichments,
            int(time.time()) - self._stale_after_seconds,
        )
        while not self._stop_event.is_set():
            processed = await self.process_next()
            if not processed:
                processed = await self.process_next_enrichment()
            if not processed:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._poll_interval_seconds,
                    )
                except TimeoutError:
                    pass

    def stop(self) -> None:
        self._stop_event.set()

    async def process_next(self) -> bool:
        event = await asyncio.to_thread(self._database.claim_pending_event, int(time.time()))
        if event is None:
            return False

        started_at = monotonic()
        try:
            if event.reply_body is None:
                await self._prepare_operation(event)
            stored_event = await asyncio.to_thread(
                self._database.get_inbound_event,
                event.wamid,
            )
            if stored_event is None or stored_event["reply_body"] is None:
                raise RuntimeError("Worker operation did not prepare a reply")
            await self._whatsapp_client.send_text(
                event.sender_wa_id,
                stored_event["reply_body"],
                context_message_id=stored_event["reply_context_wamid"],
            )
            await asyncio.to_thread(self._database.complete_event, event.wamid)
            logger.info(
                "event=worker_complete wamid=%s state=completed elapsed_ms=%d",
                event.wamid,
                int((monotonic() - started_at) * 1000),
            )
        except WhatsAppClientError as error:
            await self._schedule_retry(event, "graph_api_error")
            logger.error(
                "event=worker_retry wamid=%s state=pending status=%s category=graph_api",
                event.wamid,
                error.status_code if error.status_code is not None else "transport",
            )
        except Exception:
            await self._schedule_retry(event, "processing_error")
            logger.exception(
                "event=worker_retry wamid=%s state=pending category=processing",
                event.wamid,
            )
        return True

    async def _prepare_operation(self, event: InboundEvent) -> None:
        body = event.body or ""
        now = int(time.time())
        if body.startswith("?"):
            await self._prepare_search(event, body[1:].strip(), now)
            return
        if body.startswith("/"):
            await self._prepare_command(event, body.strip(), now)
            return
        if not body.strip():
            await asyncio.to_thread(
                self._database.prepare_reply,
                event.wamid,
                "Send text to save a note, or use /help.",
                now,
            )
            return

        urls = extract_urls(body)
        vector = await asyncio.to_thread(self._embedding_encoder.embed_passage, body)
        note = Note(
            wamid=event.wamid,
            sender_wa_id=event.sender_wa_id,
            body=body,
            searchable_text=body,
            whatsapp_timestamp=event.whatsapp_timestamp,
            created_at=now,
            embedding=vector.tobytes(),
            embedding_dimensions=int(vector.size),
            embedding_model=self._embedding_encoder.model_name,
            urls_json=json.dumps(urls, ensure_ascii=False, separators=(",", ":")),
        )
        await asyncio.to_thread(self._database.prepare_note_reply, note, now)

    async def process_next_enrichment(self) -> bool:
        note = await asyncio.to_thread(
            self._database.claim_pending_enrichment,
            int(time.time()),
        )
        if note is None:
            return False
        try:
            urls = json.loads(note["urls_json"])
            metadata = None
            for url in urls:
                try:
                    metadata = await asyncio.to_thread(self._link_enricher.fetch, url)
                except LinkEnrichmentError:
                    continue
                break
            if metadata is None:
                raise LinkEnrichmentError("no URL could be enriched")
            enriched_urls = list(urls)
            if metadata.final_url not in enriched_urls:
                enriched_urls.append(metadata.final_url)
            searchable_parts = [note["body"], *enriched_urls]
            if metadata.title:
                searchable_parts.append(metadata.title)
            if metadata.description:
                searchable_parts.append(metadata.description)
            searchable_text = "\n".join(searchable_parts)
            vector = await asyncio.to_thread(
                self._embedding_encoder.embed_passage,
                searchable_text,
            )
            await asyncio.to_thread(
                self._database.complete_link_enrichment,
                note["wamid"],
                searchable_text=searchable_text,
                urls_json=json.dumps(
                    enriched_urls,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                link_title=metadata.title,
                link_description=metadata.description,
                embedding=vector.tobytes(),
                embedding_dimensions=int(vector.size),
                embedding_model=self._embedding_encoder.model_name,
            )
            logger.info(
                "event=link_enrichment_complete wamid=%s state=completed",
                note["wamid"],
            )
        except LinkEnrichmentError:
            await asyncio.to_thread(
                self._database.fail_link_enrichment,
                note["wamid"],
                "link_enrichment_failed",
            )
            logger.warning(
                "event=link_enrichment_failed wamid=%s state=failed",
                note["wamid"],
            )
        except Exception:
            await asyncio.to_thread(
                self._database.fail_link_enrichment,
                note["wamid"],
                "link_enrichment_processing_error",
            )
            logger.exception(
                "event=link_enrichment_failed wamid=%s state=failed category=processing",
                note["wamid"],
            )
        return True

    async def _prepare_search(
        self,
        event: InboundEvent,
        query: str,
        now: int,
    ) -> None:
        if not query:
            await asyncio.to_thread(
                self._database.prepare_reply,
                event.wamid,
                "Send a search query after ?.",
                now,
            )
            return
        match = await asyncio.to_thread(self._search.find_best, query)
        if match is None:
            reply_body = "You have no saved notes yet."
            context_wamid = None
        else:
            saved_at = datetime.fromtimestamp(
                match.whatsapp_timestamp,
                tz=self._user_timezone,
            )
            saved_date = f"{saved_at.day} {saved_at.strftime('%B %Y')}"
            reply_body = f"Best match · saved {saved_date}"
            context_wamid = match.wamid
        await asyncio.to_thread(
            self._database.prepare_reply,
            event.wamid,
            reply_body,
            now,
            context_wamid=context_wamid,
        )

    async def _prepare_command(
        self,
        event: InboundEvent,
        command: str,
        now: int,
    ) -> None:
        if command == "/help":
            await asyncio.to_thread(
                self._database.prepare_reply,
                event.wamid,
                _HELP_TEXT,
                now,
            )
            return
        if command == "/delete-last":
            await asyncio.to_thread(
                self._database.prepare_delete_last_reply,
                event.wamid,
                event.sender_wa_id,
                now,
            )
            return
        await asyncio.to_thread(
            self._database.prepare_reply,
            event.wamid,
            "Unknown command. Use /help.",
            now,
        )

    async def _schedule_retry(self, event: InboundEvent, reason: str) -> None:
        delay_seconds = min(2 ** min(event.attempt_count, 8), 300)
        await asyncio.to_thread(
            self._database.retry_event,
            event.wamid,
            next_attempt_at=int(time.time()) + delay_seconds,
            failure_reason=reason,
        )
