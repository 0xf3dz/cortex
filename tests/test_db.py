import sqlite3
from pathlib import Path

import pytest

from app.db import Database


def test_connection_context_closes_connection(tmp_path: Path) -> None:
    database = Database(tmp_path / "agent.sqlite3")

    with database.connect() as connection:
        connection.execute("SELECT 1")

    with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
        connection.execute("SELECT 1")
