from typing import Protocol

import numpy as np
from sentence_transformers import SentenceTransformer


class EmbeddingEncoder(Protocol):
    @property
    def model_name(self) -> str: ...

    def embed_passage(self, text: str) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


class LocalEmbeddingEncoder:
    def __init__(self, model_name: str) -> None:
        self._model_name = model_name
        self._model = SentenceTransformer(
            model_name,
            device="cpu",
            trust_remote_code=False,
        )

    @property
    def model_name(self) -> str:
        return self._model_name

    def embed_passage(self, text: str) -> np.ndarray:
        return self._encode(f"passage: {text}")

    def embed_query(self, text: str) -> np.ndarray:
        return self._encode(f"query: {text}")

    def _encode(self, text: str) -> np.ndarray:
        encoded = self._model.encode(
            text,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        vector = np.asarray(encoded, dtype=np.float32)
        if vector.ndim != 1 or vector.size == 0:
            raise RuntimeError("Embedding model returned an invalid vector")
        return np.ascontiguousarray(vector)
