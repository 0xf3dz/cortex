import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.migrations import apply_migrations


@dataclass(frozen=True, slots=True)
class InboundEvent:
    wamid: str
    sender_wa_id: str
    message_type: str
    body: str | None
    whatsapp_timestamp: int
    received_at: int
    processing_state: str = "pending"
    attempt_count: int = 0
    reply_body: str | None = None
    reply_context_wamid: str | None = None
    operation_applied_at: int | None = None


@dataclass(frozen=True, slots=True)
class Note:
    wamid: str
    sender_wa_id: str
    body: str
    searchable_text: str
    whatsapp_timestamp: int
    created_at: int
    embedding: bytes
    embedding_dimensions: int
    embedding_model: str
    urls_json: str = "[]"
    link_title: str | None = None
    link_description: str | None = None


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(journal_mode).lower() != "wal":
                raise RuntimeError("SQLite WAL mode is unavailable")
            apply_migrations(connection)

    def insert_inbound_event(self, event: InboundEvent) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO inbound_events (
                    wamid,
                    sender_wa_id,
                    message_type,
                    body,
                    whatsapp_timestamp,
                    received_at,
                    processing_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.wamid,
                    event.sender_wa_id,
                    event.message_type,
                    event.body,
                    event.whatsapp_timestamp,
                    event.received_at,
                    event.processing_state,
                ),
            )
            return cursor.rowcount == 1

    def count_inbound_events(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT count(*) FROM inbound_events").fetchone()
            return int(row[0])

    def get_inbound_event(self, wamid: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM inbound_events WHERE wamid = ?",
                (wamid,),
            ).fetchone()

    def recover_stale_events(self, stale_before: int) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE inbound_events
                SET processing_state = 'pending',
                    processing_started_at = NULL,
                    next_attempt_at = NULL,
                    failure_reason = 'stale_processing_recovered'
                WHERE processing_state = 'processing'
                  AND processing_started_at <= ?
                """,
                (stale_before,),
            )
            return cursor.rowcount

    def claim_pending_event(self, now: int) -> InboundEvent | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM inbound_events
                WHERE processing_state = 'pending'
                  AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                ORDER BY received_at, wamid
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE inbound_events
                SET processing_state = 'processing',
                    processing_started_at = ?,
                    attempt_count = attempt_count + 1,
                    next_attempt_at = NULL
                WHERE wamid = ?
                """,
                (now, row["wamid"]),
            )
            connection.commit()
            return InboundEvent(
                wamid=row["wamid"],
                sender_wa_id=row["sender_wa_id"],
                message_type=row["message_type"],
                body=row["body"],
                whatsapp_timestamp=row["whatsapp_timestamp"],
                received_at=row["received_at"],
                processing_state="processing",
                attempt_count=row["attempt_count"] + 1,
                reply_body=row["reply_body"],
                reply_context_wamid=row["reply_context_wamid"],
                operation_applied_at=row["operation_applied_at"],
            )

    def prepare_note_reply(self, note: Note, applied_at: int) -> None:
        if len(note.embedding) != note.embedding_dimensions * 4:
            raise ValueError("Embedding byte length does not match its dimensions")
        try:
            urls = json.loads(note.urls_json)
        except json.JSONDecodeError as error:
            raise ValueError("Note URLs must be valid JSON") from error
        if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
            raise ValueError("Note URLs must be a JSON string array")
        enrichment_state = "pending" if urls else "none"
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute(
                "SELECT operation_applied_at FROM inbound_events WHERE wamid = ?",
                (note.wamid,),
            ).fetchone()
            if event is None:
                connection.rollback()
                raise RuntimeError("Inbound event does not exist")
            if event["operation_applied_at"] is not None:
                connection.commit()
                return
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO notes (
                    wamid,
                    sender_wa_id,
                    body,
                    searchable_text,
                    urls_json,
                    link_title,
                    link_description,
                    whatsapp_timestamp,
                    created_at,
                    embedding,
                    embedding_dimensions,
                    embedding_model,
                    enrichment_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    note.wamid,
                    note.sender_wa_id,
                    note.body,
                    note.searchable_text,
                    note.urls_json,
                    note.link_title,
                    note.link_description,
                    note.whatsapp_timestamp,
                    note.created_at,
                    note.embedding,
                    note.embedding_dimensions,
                    note.embedding_model,
                    enrichment_state,
                ),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    INSERT INTO notes_fts (
                        wamid,
                        body,
                        url_text,
                        link_title,
                        link_description
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        note.wamid,
                        note.body,
                        " ".join(urls),
                        note.link_title or "",
                        note.link_description or "",
                    ),
                )
            connection.execute(
                """
                UPDATE inbound_events
                SET reply_body = 'Saved.',
                    reply_context_wamid = NULL,
                    operation_applied_at = ?
                WHERE wamid = ?
                """,
                (applied_at, note.wamid),
            )
            connection.commit()

    def recover_stale_enrichments(self, stale_before: int) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE notes
                SET enrichment_state = 'pending',
                    enrichment_started_at = NULL,
                    enrichment_failure_reason = 'stale_processing_recovered'
                WHERE enrichment_state = 'processing'
                  AND enrichment_started_at <= ?
                """,
                (stale_before,),
            )
            return cursor.rowcount

    def claim_pending_enrichment(self, now: int) -> sqlite3.Row | None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM notes
                WHERE enrichment_state = 'pending'
                ORDER BY created_at, wamid
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE notes
                SET enrichment_state = 'processing',
                    enrichment_started_at = ?,
                    enrichment_attempt_count = enrichment_attempt_count + 1
                WHERE wamid = ?
                """,
                (now, row["wamid"]),
            )
            connection.commit()
            return row

    def complete_link_enrichment(
        self,
        wamid: str,
        *,
        searchable_text: str,
        urls_json: str,
        link_title: str | None,
        link_description: str | None,
        embedding: bytes,
        embedding_dimensions: int,
        embedding_model: str,
    ) -> None:
        if len(embedding) != embedding_dimensions * 4:
            raise ValueError("Embedding byte length does not match its dimensions")
        try:
            urls = json.loads(urls_json)
        except json.JSONDecodeError as error:
            raise ValueError("Note URLs must be valid JSON") from error
        if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
            raise ValueError("Note URLs must be a JSON string array")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            note = connection.execute(
                "SELECT body FROM notes WHERE wamid = ?",
                (wamid,),
            ).fetchone()
            if note is None:
                connection.rollback()
                raise RuntimeError("Note does not exist")
            cursor = connection.execute(
                """
                UPDATE notes
                SET searchable_text = ?,
                    urls_json = ?,
                    link_title = ?,
                    link_description = ?,
                    embedding = ?,
                    embedding_dimensions = ?,
                    embedding_model = ?,
                    enrichment_state = 'completed',
                    enrichment_started_at = NULL,
                    enrichment_failure_reason = NULL
                WHERE wamid = ?
                  AND enrichment_state = 'processing'
                """,
                (
                    searchable_text,
                    urls_json,
                    link_title,
                    link_description,
                    embedding,
                    embedding_dimensions,
                    embedding_model,
                    wamid,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                raise RuntimeError("Note enrichment is not in progress")
            connection.execute("DELETE FROM notes_fts WHERE wamid = ?", (wamid,))
            connection.execute(
                """
                INSERT INTO notes_fts (
                    wamid,
                    body,
                    url_text,
                    link_title,
                    link_description
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    wamid,
                    note["body"],
                    " ".join(urls),
                    link_title or "",
                    link_description or "",
                ),
            )
            connection.commit()

    def fail_link_enrichment(self, wamid: str, failure_reason: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE notes
                SET enrichment_state = 'failed',
                    enrichment_started_at = NULL,
                    enrichment_failure_reason = ?
                WHERE wamid = ?
                  AND enrichment_state = 'processing'
                """,
                (failure_reason, wamid),
            )

    def prepare_reply(
        self,
        wamid: str,
        body: str,
        applied_at: int,
        *,
        context_wamid: str | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE inbound_events
                SET reply_body = COALESCE(reply_body, ?),
                    reply_context_wamid = COALESCE(reply_context_wamid, ?),
                    operation_applied_at = COALESCE(operation_applied_at, ?)
                WHERE wamid = ?
                """,
                (body, context_wamid, applied_at, wamid),
            )

    def prepare_delete_last_reply(
        self,
        event_wamid: str,
        sender_wa_id: str,
        applied_at: int,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            event = connection.execute(
                "SELECT operation_applied_at FROM inbound_events WHERE wamid = ?",
                (event_wamid,),
            ).fetchone()
            if event is None:
                connection.rollback()
                raise RuntimeError("Inbound event does not exist")
            if event["operation_applied_at"] is not None:
                connection.commit()
                return
            note = connection.execute(
                """
                SELECT wamid
                FROM notes
                WHERE sender_wa_id = ?
                ORDER BY whatsapp_timestamp DESC, created_at DESC, wamid DESC
                LIMIT 1
                """,
                (sender_wa_id,),
            ).fetchone()
            if note is None:
                reply_body = "You have no saved notes yet."
            else:
                connection.execute("DELETE FROM notes_fts WHERE wamid = ?", (note["wamid"],))
                connection.execute("DELETE FROM notes WHERE wamid = ?", (note["wamid"],))
                reply_body = "Deleted."
            connection.execute(
                """
                UPDATE inbound_events
                SET reply_body = ?,
                    reply_context_wamid = NULL,
                    operation_applied_at = ?
                WHERE wamid = ?
                """,
                (reply_body, applied_at, event_wamid),
            )
            connection.commit()

    def complete_event(self, wamid: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE inbound_events
                SET processing_state = 'completed',
                    processing_started_at = NULL,
                    next_attempt_at = NULL,
                    failure_reason = NULL
                WHERE wamid = ?
                """,
                (wamid,),
            )

    def retry_event(
        self,
        wamid: str,
        *,
        next_attempt_at: int,
        failure_reason: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE inbound_events
                SET processing_state = 'pending',
                    processing_started_at = NULL,
                    next_attempt_at = ?,
                    failure_reason = ?
                WHERE wamid = ?
                """,
                (next_attempt_at, failure_reason, wamid),
            )

    def count_notes(self) -> int:
        with self.connect() as connection:
            row = connection.execute("SELECT count(*) FROM notes").fetchone()
            return int(row[0])

    def get_note(self, wamid: str) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM notes WHERE wamid = ?",
                (wamid,),
            ).fetchone()

    def list_notes(self) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                "SELECT * FROM notes ORDER BY whatsapp_timestamp, wamid"
            ).fetchall()

    def search_notes_fts(self, fts_query: str, limit: int = 20) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT notes.*, bm25(notes_fts) AS lexical_score
                FROM notes_fts
                JOIN notes ON notes.wamid = notes_fts.wamid
                WHERE notes_fts MATCH ?
                ORDER BY lexical_score
                LIMIT ?
                """,
                (fts_query, limit),
            ).fetchall()
