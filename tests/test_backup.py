import sqlite3
from pathlib import Path

from app.backup import create_backup
from app.db import Database, InboundEvent


def test_create_backup_copies_consistent_database(tmp_path: Path) -> None:
    source_path = tmp_path / "agent.sqlite3"
    destination_path = tmp_path / "backups" / "agent.sqlite3"
    database = Database(source_path)
    database.initialize()
    assert database.insert_inbound_event(
        InboundEvent(
            wamid="wamid.BACKUP",
            sender_wa_id="61400000000",
            message_type="text",
            body="backup note",
            whatsapp_timestamp=1,
            received_at=1,
        )
    )

    create_backup(source_path, destination_path)

    with sqlite3.connect(destination_path) as backup:
        assert backup.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert backup.execute("SELECT count(*) FROM inbound_events").fetchone() == (1,)
    assert destination_path.stat().st_mode & 0o777 == 0o600
