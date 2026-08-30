import re
from dataclasses import dataclass

import numpy as np

from app.db import Database
from app.embedding import EmbeddingEncoder


_TOKEN_PATTERN = re.compile(r"\w+", re.UNICODE)
_RRF_K = 60


@dataclass(frozen=True, slots=True)
class SearchMatch:
    wamid: str
    whatsapp_timestamp: int


class SearchService:
    def __init__(self, database: Database, embedding_encoder: EmbeddingEncoder) -> None:
        self._database = database
        self._embedding_encoder = embedding_encoder

    def find_best(self, query: str) -> SearchMatch | None:
        notes = self._database.list_notes()
        if not notes:
            return None

        query_vector = self._embedding_encoder.embed_query(query)
        lexical_rows = self._lexical_candidates(query)
        lexical_rank = {
            row["wamid"]: rank
            for rank, row in enumerate(lexical_rows, start=1)
        }

        vector_scores: list[tuple[str, float]] = []
        for note in notes:
            if note["embedding_model"] != self._embedding_encoder.model_name:
                raise RuntimeError("Stored embedding model does not match the configured model")
            dimensions = int(note["embedding_dimensions"])
            embedding_bytes = note["embedding"]
            if len(embedding_bytes) != dimensions * 4:
                raise RuntimeError("Stored embedding byte length is invalid")
            note_vector = np.frombuffer(embedding_bytes, dtype=np.float32)
            if note_vector.size != query_vector.size:
                raise RuntimeError("Stored embedding dimensions do not match the query")
            vector_scores.append((note["wamid"], float(np.dot(note_vector, query_vector))))

        vector_scores.sort(key=lambda item: (-item[1], item[0]))
        vector_rank = {
            wamid: rank
            for rank, (wamid, _) in enumerate(vector_scores, start=1)
        }

        fused: list[tuple[str, float]] = []
        for note in notes:
            wamid = note["wamid"]
            score = 0.0
            if wamid in lexical_rank:
                score += 1.0 / (_RRF_K + lexical_rank[wamid])
            if wamid in vector_rank:
                score += 1.0 / (_RRF_K + vector_rank[wamid])
            fused.append((wamid, score))
        fused.sort(key=lambda item: (-item[1], item[0]))

        best_wamid = fused[0][0]
        best_note = next(note for note in notes if note["wamid"] == best_wamid)
        return SearchMatch(
            wamid=best_wamid,
            whatsapp_timestamp=int(best_note["whatsapp_timestamp"]),
        )

    def _lexical_candidates(self, query: str) -> list[object]:
        tokens = list(dict.fromkeys(token.casefold() for token in _TOKEN_PATTERN.findall(query)))
        if not tokens:
            return []
        fts_query = " OR ".join(f'"{token}"' for token in tokens)
        return list(self._database.search_notes_fts(fts_query))
