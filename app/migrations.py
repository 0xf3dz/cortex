import sqlite3


MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        CREATE TABLE inbound_events (
            wamid TEXT PRIMARY KEY,
            sender_wa_id TEXT NOT NULL,
            message_type TEXT NOT NULL,
            body TEXT,
            whatsapp_timestamp INTEGER NOT NULL,
            received_at INTEGER NOT NULL,
            processing_state TEXT NOT NULL
                CHECK (processing_state IN ('pending', 'processing', 'completed', 'failed')),
            failure_reason TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_attempt_at INTEGER
        );

        CREATE INDEX inbound_events_pending_idx
            ON inbound_events (processing_state, next_attempt_at, received_at);
        """,
    ),
    (
        2,
        """
        ALTER TABLE inbound_events ADD COLUMN processing_started_at INTEGER;
        ALTER TABLE inbound_events ADD COLUMN reply_body TEXT;
        ALTER TABLE inbound_events ADD COLUMN reply_context_wamid TEXT;
        ALTER TABLE inbound_events ADD COLUMN operation_applied_at INTEGER;

        CREATE TABLE notes (
            wamid TEXT PRIMARY KEY,
            sender_wa_id TEXT NOT NULL,
            body TEXT NOT NULL,
            searchable_text TEXT NOT NULL,
            urls_json TEXT NOT NULL,
            link_title TEXT,
            link_description TEXT,
            whatsapp_timestamp INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            embedding BLOB NOT NULL,
            embedding_dimensions INTEGER NOT NULL CHECK (embedding_dimensions > 0),
            embedding_model TEXT NOT NULL
        );

        CREATE INDEX notes_sender_timestamp_idx
            ON notes (sender_wa_id, whatsapp_timestamp DESC, created_at DESC);

        CREATE VIRTUAL TABLE notes_fts USING fts5(
            wamid UNINDEXED,
            body,
            url_text,
            link_title,
            link_description,
            tokenize = 'unicode61'
        );
        """,
    ),
    (
        3,
        """
        ALTER TABLE notes ADD COLUMN enrichment_state TEXT NOT NULL DEFAULT 'none'
            CHECK (enrichment_state IN ('none', 'pending', 'processing', 'completed', 'failed'));
        ALTER TABLE notes ADD COLUMN enrichment_started_at INTEGER;
        ALTER TABLE notes ADD COLUMN enrichment_attempt_count INTEGER NOT NULL DEFAULT 0
            CHECK (enrichment_attempt_count >= 0);
        ALTER TABLE notes ADD COLUMN enrichment_failure_reason TEXT;

        CREATE INDEX notes_enrichment_pending_idx
            ON notes (enrichment_state, created_at);
        """,
    ),
    (
        4,
        """
        ALTER TABLE inbound_events ADD COLUMN reply_reaction_emoji TEXT;

        UPDATE inbound_events
        SET reply_body = NULL,
            reply_reaction_emoji = '👍'
        WHERE reply_body = 'Saved.'
          AND processing_state IN ('pending', 'processing')
          AND EXISTS (
              SELECT 1
              FROM notes
              WHERE notes.wamid = inbound_events.wamid
          );
        """,
    ),
)


def apply_migrations(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at INTEGER NOT NULL DEFAULT (unixepoch())
        )
        """
    )
    applied = {
        row[0]
        for row in connection.execute("SELECT version FROM schema_migrations")
    }

    for version, sql in MIGRATIONS:
        if version in applied:
            continue
        try:
            connection.executescript(
                f"""
                BEGIN IMMEDIATE;
                {sql}
                INSERT INTO schema_migrations (version) VALUES ({version});
                COMMIT;
                """
            )
        except Exception:
            connection.rollback()
            raise
