import os
import sqlite3
from pathlib import Path


def create_backup(source_path: Path, destination_path: Path) -> None:
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    destination_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary_path = destination_path.with_suffix(f"{destination_path.suffix}.tmp")
    temporary_path.unlink(missing_ok=True)
    try:
        with (
            sqlite3.connect(f"file:{source_path}?mode=ro", uri=True) as source,
            sqlite3.connect(temporary_path) as destination,
        ):
            source.backup(destination)
        with sqlite3.connect(temporary_path) as backup:
            integrity = backup.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError("SQLite backup integrity check failed")
        temporary_path.chmod(0o600)
        temporary_path.replace(destination_path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> None:
    source_path = Path(os.environ.get("DATABASE_PATH", "/data/agent.sqlite3"))
    destination_path = Path(
        os.environ.get("BACKUP_PATH", "/data/backups/agent-latest.sqlite3")
    )
    create_backup(source_path, destination_path)
    print(f"backup={destination_path} state=completed")


if __name__ == "__main__":
    main()
