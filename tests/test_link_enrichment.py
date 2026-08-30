import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from app.db import Database, InboundEvent
from app.link_enrichment import (
    LinkEnrichmentError,
    LinkMetadata,
    SecureLinkEnricher,
    extract_urls,
)
from app.whatsapp_client import FakeWhatsAppClient
from app.worker import EventWorker


class FakeEmbeddingEncoder:
    model_name = "test-embedding-model"

    def embed_passage(self, text: str) -> np.ndarray:
        vector = np.array([len(text) + 1, text.count(" ") + 1], dtype=np.float32)
        return vector / np.linalg.norm(vector)

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_passage(text)


class SuccessfulLinkEnricher:
    def fetch(self, url: str) -> LinkMetadata:
        return LinkMetadata(
            final_url="https://example.com/final",
            title="SQLite Vector Search Guide",
            description="A compact database retrieval reference.",
        )


class FailedLinkEnricher:
    def fetch(self, url: str) -> LinkMetadata:
        raise LinkEnrichmentError("test failure")

def public_dns(host: str, port: int, *, type: int) -> list[tuple[object, ...]]:
    assert type == socket.SOCK_STREAM
    address = host if host == "127.0.0.1" else "93.184.216.34"
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]


def response(
    status_code: int,
    *,
    location: str | None = None,
    content_type: str | None = None,
    body: bytes = b"",
) -> SimpleNamespace:
    return SimpleNamespace(
        status_code=status_code,
        location=location,
        content_type=content_type,
        body=body,
    )


def make_worker(
    path: Path,
    link_enricher: SuccessfulLinkEnricher | FailedLinkEnricher,
) -> tuple[Database, EventWorker]:
    database = Database(path)
    database.initialize()
    worker = EventWorker(
        database=database,
        whatsapp_client=FakeWhatsAppClient(),
        embedding_encoder=FakeEmbeddingEncoder(),
        user_timezone="Australia/Brisbane",
        link_enricher=link_enricher,
    )
    return database, worker


def add_link_event(database: Database, body: str) -> InboundEvent:
    event = InboundEvent(
        wamid="wamid.LINK",
        sender_wa_id="61400000000",
        message_type="text",
        body=body,
        whatsapp_timestamp=1_700_000_000,
        received_at=1_700_000_000,
    )
    assert database.insert_inbound_event(event)
    return event


def test_extract_urls_preserves_order_and_trims_message_punctuation() -> None:
    text = (
        "Read https://example.com/guide, then http://example.org/path?q=one. "
        "Again https://example.com/guide"
    )

    assert extract_urls(text) == [
        "https://example.com/guide",
        "http://example.org/path?q=one",
    ]


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/file",
        "https://user:secret@example.com/",
        "http://127.0.0.1/admin",
        "http://10.20.30.40/internal",
        "http://169.254.169.254/latest/meta-data/",
        "http://224.0.0.1/stream",
        "http://192.0.2.1/reserved",
    ],
)
def test_rejects_disallowed_or_non_public_destinations(url: str) -> None:
    with pytest.raises(LinkEnrichmentError):
        SecureLinkEnricher().fetch(url)


def test_revalidates_redirect_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", public_dns)
    enricher = SecureLinkEnricher()
    calls: list[str] = []

    def redirect(url: str, connect_ip: str) -> SimpleNamespace:
        calls.append(url)
        return response(302, location="http://127.0.0.1/private")

    monkeypatch.setattr(enricher, "_request_once", redirect)

    with pytest.raises(LinkEnrichmentError, match="not public"):
        enricher.fetch("https://example.com/start")
    assert calls == ["https://example.com/start"]


def test_limits_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", public_dns)
    enricher = SecureLinkEnricher(max_redirects=2)
    monkeypatch.setattr(
        enricher,
        "_request_once",
        lambda url, connect_ip: response(302, location="/next"),
    )

    with pytest.raises(LinkEnrichmentError, match="redirect limit"):
        enricher.fetch("https://example.com/start")


def test_parses_only_html_title_and_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", public_dns)
    enricher = SecureLinkEnricher()
    monkeypatch.setattr(
        enricher,
        "_request_once",
        lambda url, connect_ip: response(
            200,
            content_type="text/html; charset=utf-8",
            body=(
                b"<html><head><title>  SQLite &amp; vectors  </title>"
                b'<meta name="description" content=" Local semantic search. ">'
                b"</head><body>Article text must not become metadata.</body></html>"
            ),
        ),
    )

    metadata = enricher.fetch("https://example.com/start#section")

    assert metadata == LinkMetadata(
        final_url="https://example.com/start",
        title="SQLite & vectors",
        description="Local semantic search.",
    )


def test_rejects_non_html_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", public_dns)
    enricher = SecureLinkEnricher()
    monkeypatch.setattr(
        enricher,
        "_request_once",
        lambda url, connect_ip: response(
            200,
            content_type="application/pdf",
            body=b"%PDF",
        ),
    )

    with pytest.raises(LinkEnrichmentError, match="not HTML"):
        enricher.fetch("https://example.com/file.pdf")


def test_stops_oversized_streamed_response() -> None:
    class OversizedResponse:
        remaining = 9

        def getheader(self, name: str) -> str | None:
            return None

        def read(self, size: int) -> bytes:
            if self.remaining == 0:
                return b""
            count = min(size, self.remaining)
            self.remaining -= count
            return b"x" * count

    with pytest.raises(LinkEnrichmentError, match="too large"):
        SecureLinkEnricher(max_response_bytes=8)._read_response_body(  # type: ignore[arg-type]
            OversizedResponse()
        )


def test_worker_enriches_note_and_updates_search_data(tmp_path: Path) -> None:
    database, worker = make_worker(
        tmp_path / "agent.sqlite3",
        SuccessfulLinkEnricher(),
    )
    event = add_link_event(
        database,
        "Useful database article https://example.com/original",
    )

    assert asyncio.run(worker.process_next())
    initial_note = database.get_note(event.wamid)
    assert initial_note is not None
    assert initial_note["enrichment_state"] == "pending"
    assert database.search_notes_fts('"Useful"')[0]["wamid"] == event.wamid

    assert asyncio.run(worker.process_next_enrichment())
    enriched_note = database.get_note(event.wamid)
    assert enriched_note is not None
    assert enriched_note["enrichment_state"] == "completed"
    assert enriched_note["link_title"] == "SQLite Vector Search Guide"
    assert enriched_note["link_description"] == "A compact database retrieval reference."
    assert "https://example.com/original" in enriched_note["searchable_text"]
    assert "https://example.com/final" in enriched_note["searchable_text"]
    assert database.search_notes_fts('"SQLite"')[0]["wamid"] == event.wamid
    assert database.search_notes_fts('"example"')[0]["wamid"] == event.wamid


def test_failed_enrichment_preserves_saved_note(tmp_path: Path) -> None:
    database, worker = make_worker(
        tmp_path / "agent.sqlite3",
        FailedLinkEnricher(),
    )
    event = add_link_event(database, "https://example.com/unavailable")

    assert asyncio.run(worker.process_next())
    assert asyncio.run(worker.process_next_enrichment())

    note = database.get_note(event.wamid)
    assert note is not None
    assert note["body"] == "https://example.com/unavailable"
    assert note["enrichment_state"] == "failed"
    assert note["enrichment_failure_reason"] == "link_enrichment_failed"
    assert database.search_notes_fts('"unavailable"')[0]["wamid"] == event.wamid
