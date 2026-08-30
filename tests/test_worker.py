import asyncio
import time
from pathlib import Path

import numpy as np

from app.db import Database, InboundEvent
from app.whatsapp_client import FakeWhatsAppClient, SentMessage, WhatsAppClientError
from app.worker import EventWorker


class FakeEmbeddingEncoder:
    model_name = "test-embedding-model"

    def embed_passage(self, text: str) -> np.ndarray:
        return self._embed(text)

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed(text)

    def _embed(self, text: str) -> np.ndarray:
        values = np.array(
            [
                len(text) + 1,
                text.count("a") + 1,
                text.count("e") + 1,
                text.count(" ") + 1,
            ],
            dtype=np.float32,
        )
        return values / np.linalg.norm(values)


class CommitInspectingClient(FakeWhatsAppClient):
    def __init__(self, database: Database) -> None:
        super().__init__()
        self._database = database

    async def send_text(
        self,
        recipient_wa_id: str,
        body: str,
        *,
        context_message_id: str | None = None,
    ) -> None:
        if body == "Saved.":
            assert self._database.count_notes() == 1
        await super().send_text(
            recipient_wa_id,
            body,
            context_message_id=context_message_id,
        )


class FailOnceClient(FakeWhatsAppClient):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    async def send_text(
        self,
        recipient_wa_id: str,
        body: str,
        *,
        context_message_id: str | None = None,
    ) -> None:
        if not self.failed:
            self.failed = True
            raise WhatsAppClientError(500)
        await super().send_text(
            recipient_wa_id,
            body,
            context_message_id=context_message_id,
        )


def make_database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    return database


def make_event(wamid: str, body: str, timestamp: int) -> InboundEvent:
    return InboundEvent(
        wamid=wamid,
        sender_wa_id="61400000000",
        message_type="text",
        body=body,
        whatsapp_timestamp=timestamp,
        received_at=timestamp,
    )


def make_worker(database: Database, client: FakeWhatsAppClient) -> EventWorker:
    return EventWorker(
        database=database,
        whatsapp_client=client,
        embedding_encoder=FakeEmbeddingEncoder(),
        user_timezone="Australia/Brisbane",
    )


def process_next(worker: EventWorker) -> bool:
    return asyncio.run(worker.process_next())


def test_normal_text_commits_note_before_saved_reply(tmp_path: Path) -> None:
    database = make_database(tmp_path / "agent.sqlite3")
    client = CommitInspectingClient(database)
    worker = make_worker(database, client)
    event = make_event("wamid.NOTE", "A durable note", 1_700_000_000)
    assert database.insert_inbound_event(event)

    assert process_next(worker)

    note = database.get_note(event.wamid)
    stored_event = database.get_inbound_event(event.wamid)
    assert note is not None
    assert note["body"] == "A durable note"
    assert note["embedding_dimensions"] == 4
    assert stored_event is not None
    assert stored_event["processing_state"] == "completed"
    assert client.sent_messages == [SentMessage("61400000000", "Saved.")]


def test_query_and_commands_create_no_notes(tmp_path: Path) -> None:
    database = make_database(tmp_path / "agent.sqlite3")
    client = FakeWhatsAppClient()
    worker = make_worker(database, client)
    events = [
        make_event("wamid.QUERY", "? database", 1),
        make_event("wamid.HELP", "/help", 2),
        make_event("wamid.UNKNOWN", "/unknown", 3),
    ]
    for event in events:
        assert database.insert_inbound_event(event)
        assert process_next(worker)

    assert database.count_notes() == 0
    assert [message.body for message in client.sent_messages] == [
        "You have no saved notes yet.",
        "Send text to save a note.\nStart a message with ? to search.\nUse /delete-last to delete your latest note.",
        "Unknown command. Use /help.",
    ]


def test_search_reply_uses_source_context_and_exact_saved_date(tmp_path: Path) -> None:
    database = make_database(tmp_path / "agent.sqlite3")
    client = FakeWhatsAppClient()
    worker = make_worker(database, client)
    note = make_event(
        "wamid.SOURCE",
        "SQLite vector extension benchmark",
        1_700_000_000,
    )
    query = make_event("wamid.SEARCH", "? embedded database vectors", 1_700_000_100)
    for event in (note, query):
        assert database.insert_inbound_event(event)
        assert process_next(worker)

    assert client.sent_messages[-1] == SentMessage(
        recipient_wa_id="61400000000",
        body="Best match · saved 15 November 2023",
        context_message_id="wamid.SOURCE",
    )


def test_webhook_retry_creates_one_note_and_reply(tmp_path: Path) -> None:
    database = make_database(tmp_path / "agent.sqlite3")
    client = FakeWhatsAppClient()
    worker = make_worker(database, client)
    event = make_event("wamid.DUPLICATE", "Save once", 1)

    assert database.insert_inbound_event(event)
    assert not database.insert_inbound_event(event)
    assert process_next(worker)
    assert not process_next(worker)

    assert database.count_notes() == 1
    assert client.sent_messages == [SentMessage("61400000000", "Saved.")]


def test_delete_last_removes_latest_note_and_fts_row(tmp_path: Path) -> None:
    database = make_database(tmp_path / "agent.sqlite3")
    client = FakeWhatsAppClient()
    worker = make_worker(database, client)
    first = make_event("wamid.FIRST", "first alpha note", 100)
    second = make_event("wamid.SECOND", "second beta note", 200)
    delete = make_event("wamid.DELETE", "/delete-last", 300)

    for event in (first, second, delete):
        assert database.insert_inbound_event(event)
        assert process_next(worker)

    assert database.count_notes() == 1
    assert database.get_note(first.wamid) is not None
    assert database.get_note(second.wamid) is None
    assert database.search_notes_fts('"beta"') == []
    assert client.sent_messages[-1].body == "Deleted."


def test_failed_reply_retries_without_duplicate_note(tmp_path: Path) -> None:
    database = make_database(tmp_path / "agent.sqlite3")
    client = FailOnceClient()
    worker = make_worker(database, client)
    event = make_event("wamid.RETRY", "Retry this reply", 1)
    assert database.insert_inbound_event(event)

    assert process_next(worker)
    failed_event = database.get_inbound_event(event.wamid)
    assert failed_event is not None
    assert failed_event["processing_state"] == "pending"
    assert failed_event["reply_body"] == "Saved."
    assert database.count_notes() == 1

    with database.connect() as connection:
        connection.execute(
            "UPDATE inbound_events SET next_attempt_at = 0 WHERE wamid = ?",
            (event.wamid,),
        )
    assert process_next(worker)

    completed_event = database.get_inbound_event(event.wamid)
    assert completed_event is not None
    assert completed_event["processing_state"] == "completed"
    assert database.count_notes() == 1
    assert client.sent_messages == [SentMessage("61400000000", "Saved.")]


def test_stale_processing_event_returns_to_pending(tmp_path: Path) -> None:
    database = make_database(tmp_path / "agent.sqlite3")
    event = make_event("wamid.STALE", "Recover me", 1)
    assert database.insert_inbound_event(event)
    claimed = database.claim_pending_event(100)
    assert claimed is not None

    assert database.recover_stale_events(100) == 1

    recovered = database.get_inbound_event(event.wamid)
    assert recovered is not None
    assert recovered["processing_state"] == "pending"


def test_completed_note_survives_database_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "agent.sqlite3"
    database = make_database(database_path)
    client = FakeWhatsAppClient()
    worker = make_worker(database, client)
    event = make_event("wamid.RESTART", "Keep after restart", int(time.time()))
    assert database.insert_inbound_event(event)
    assert process_next(worker)

    restarted_database = make_database(database_path)

    note = restarted_database.get_note(event.wamid)
    stored_event = restarted_database.get_inbound_event(event.wamid)
    assert note is not None
    assert stored_event is not None
    assert stored_event["processing_state"] == "completed"
