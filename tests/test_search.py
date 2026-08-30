from pathlib import Path

import numpy as np

from app.db import Database, InboundEvent, Note
from app.search import SearchService


class ControlledEmbeddingEncoder:
    model_name = "test-embedding-model"

    def __init__(self) -> None:
        self.query_calls = 0

    def embed_passage(self, text: str) -> np.ndarray:
        if "database" in text.casefold():
            return np.array([1.0, 0.0], dtype=np.float32)
        return np.array([0.0, 1.0], dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        self.query_calls += 1
        if text in {"embedded storage troubleshooting", "ZXQ-441", "192.168.50.1"}:
            return np.array([1.0, 0.0], dtype=np.float32)
        return np.array([0.0, 1.0], dtype=np.float32)


class FailingEmbeddingEncoder(ControlledEmbeddingEncoder):
    def embed_query(self, text: str) -> np.ndarray:
        raise AssertionError("An empty search must not embed a query")


def make_database(path: Path) -> Database:
    database = Database(path)
    database.initialize()
    return database


def add_note(
    database: Database,
    encoder: ControlledEmbeddingEncoder,
    wamid: str,
    body: str,
    timestamp: int,
) -> None:
    event = InboundEvent(
        wamid=wamid,
        sender_wa_id="61400000000",
        message_type="text",
        body=body,
        whatsapp_timestamp=timestamp,
        received_at=timestamp,
    )
    assert database.insert_inbound_event(event)
    vector = encoder.embed_passage(body)
    database.prepare_note_reply(
        Note(
            wamid=wamid,
            sender_wa_id=event.sender_wa_id,
            body=body,
            searchable_text=body,
            whatsapp_timestamp=timestamp,
            created_at=timestamp,
            embedding=vector.tobytes(),
            embedding_dimensions=vector.size,
            embedding_model=encoder.model_name,
        ),
        applied_at=timestamp,
    )


def test_vector_search_returns_one_semantic_match(tmp_path: Path) -> None:
    database = make_database(tmp_path / "agent.sqlite3")
    encoder = ControlledEmbeddingEncoder()
    add_note(database, encoder, "wamid.DATABASE", "SQLite database index notes", 1)
    add_note(database, encoder, "wamid.RECIPE", "Chocolate dessert recipe", 2)

    match = SearchService(database, encoder).find_best("embedded storage troubleshooting")

    assert match is not None
    assert match.wamid == "wamid.DATABASE"
    assert match.whatsapp_timestamp == 1
    assert encoder.query_calls == 1


def test_lexical_rank_preserves_exact_technical_name_and_url(tmp_path: Path) -> None:
    database = make_database(tmp_path / "agent.sqlite3")
    encoder = ControlledEmbeddingEncoder()
    add_note(database, encoder, "wamid.SERIAL", "Laptop serial ZXQ-441 expires in March", 1)
    add_note(database, encoder, "wamid.URL", "Router address 192.168.50.1", 2)
    add_note(database, encoder, "wamid.DATABASE", "Unrelated database note", 3)
    search = SearchService(database, encoder)

    serial_match = search.find_best("ZXQ-441")
    url_match = search.find_best("192.168.50.1")

    assert serial_match is not None
    assert serial_match.wamid == "wamid.SERIAL"
    assert url_match is not None
    assert url_match.wamid == "wamid.URL"


def test_empty_collection_returns_no_match_without_embedding(tmp_path: Path) -> None:
    database = make_database(tmp_path / "agent.sqlite3")

    match = SearchService(database, FailingEmbeddingEncoder()).find_best("anything")

    assert match is None


def test_invalid_stored_embedding_dimensions_fail_search(tmp_path: Path) -> None:
    database = make_database(tmp_path / "agent.sqlite3")
    encoder = ControlledEmbeddingEncoder()
    add_note(database, encoder, "wamid.DATABASE", "SQLite database note", 1)
    with database.connect() as connection:
        connection.execute(
            "UPDATE notes SET embedding_dimensions = 3 WHERE wamid = ?",
            ("wamid.DATABASE",),
        )

    try:
        SearchService(database, encoder).find_best("embedded storage troubleshooting")
    except RuntimeError as error:
        assert str(error) == "Stored embedding byte length is invalid"
    else:
        raise AssertionError("Search accepted invalid stored embedding dimensions")
