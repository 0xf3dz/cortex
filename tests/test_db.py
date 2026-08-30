import sqlite3
from pathlib import Path

import pytest

from app.db import Database
from app.migrations import MIGRATIONS


def test_connection_context_closes_connection(tmp_path: Path) -> None:
    database = Database(tmp_path / "agent.sqlite3")

    with database.connect() as connection:
        connection.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")


def test_migration_converts_pending_saved_reply_to_reaction(tmp_path: Path) -> None:
    database_path = tmp_path / "agent.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at INTEGER)"
        )
        for version, sql in MIGRATIONS[:3]:
            connection.executescript(sql)
            connection.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, 0)",
                (version,),
            )
        connection.execute(
            """
            INSERT INTO inbound_events (
                wamid,
                sender_wa_id,
                message_type,
                body,
                whatsapp_timestamp,
                received_at,
                processing_state,
                reply_body,
                operation_applied_at
            ) VALUES ('wamid.NOTE', '61400000000', 'text', 'note', 1, 1, 'pending', 'Saved.', 1)
            """
        )
        connection.execute(
            """
            INSERT INTO notes (
                wamid,
                sender_wa_id,
                body,
                searchable_text,
                urls_json,
                whatsapp_timestamp,
                created_at,
                embedding,
                embedding_dimensions,
                embedding_model
            ) VALUES ('wamid.NOTE', '61400000000', 'note', 'note', '[]', 1, 1, ?, 1, 'test')
            """,
            (b"\x00\x00\x00\x00",),
        )

    database = Database(database_path)
    database.initialize()

    event = database.get_inbound_event("wamid.NOTE")
    assert event is not None
    assert event["reply_body"] is None
    assert event["reply_reaction_emoji"] == "👍"
