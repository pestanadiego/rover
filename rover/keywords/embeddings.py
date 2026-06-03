import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from sentence_transformers import SentenceTransformer

from rover.common import utc_now_iso
from rover.db import ensure_database_schema

DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


@dataclass(frozen=True)
class EmbeddingPhrase:
    keyword: str
    keyword_id: int | None = None


@dataclass(frozen=True)
class SimilarityEdge:
    source_keyword_id: int | None
    source_keyword: str
    candidate_keyword: str
    similarity_score: float


class KeywordEmbeddingIndex:
    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL):
        self.model_name = model_name
        self._model: Any | None = None

    def is_available(self) -> bool:
        return self._load_model() is not None

    def vectors_for_phrases(
        self,
        conn: sqlite3.Connection,
        phrases: Iterable[EmbeddingPhrase],
        created_at_utc: str,
    ) -> dict[str, list[float]]:
        ensure_embedding_tables(conn)
        normalized_phrases = unique_phrases(phrases)
        if not normalized_phrases:
            return {}

        vectors = load_cached_vectors(conn, self.model_name, normalized_phrases)
        missing_phrases = [
            phrase
            for phrase in normalized_phrases
            if phrase.keyword not in vectors
        ]
        if not missing_phrases:
            return vectors

        encoded_vectors = self._encode([phrase.keyword for phrase in missing_phrases])
        if not encoded_vectors:
            return vectors

        for phrase, vector in zip(missing_phrases, encoded_vectors):
            clean_vector = normalize_vector(vector)
            if not clean_vector:
                continue

            store_embedding(
                conn=conn,
                phrase=phrase,
                model_name=self.model_name,
                vector=clean_vector,
                created_at_utc=created_at_utc,
            )
            vectors[phrase.keyword] = clean_vector

        return vectors

    def _encode(self, keywords: list[str]) -> list[list[float]]:
        model = self._load_model()
        if model is None:
            return []

        vectors = model.encode(
            keywords,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [vector_to_list(vector) for vector in vectors]

    def _load_model(self) -> Any | None:
        if self._model is not None:
            return self._model

        if SentenceTransformer is None:
            return None

        try:
            self._model = SentenceTransformer(self.model_name)
        except Exception:
            return None

        return self._model


def ensure_embedding_tables(conn: sqlite3.Connection) -> None:
    ensure_database_schema(conn)


def load_cached_vectors(
    conn: sqlite3.Connection,
    model_name: str,
    phrases: list[EmbeddingPhrase],
) -> dict[str, list[float]]:
    if not phrases:
        return {}

    keywords = [phrase.keyword for phrase in phrases]
    placeholders = ", ".join("?" for _keyword in keywords)
    rows = conn.execute(
        f"""
        SELECT keyword, embedding_json
        FROM keyword_embeddings
        WHERE model_name = ?
          AND keyword IN ({placeholders})
        """,
        [model_name, *keywords],
    ).fetchall()

    vectors = {}
    for row in rows:
        vector = parse_vector_json(row["embedding_json"])
        if not vector:
            continue
        vectors[row["keyword"]] = vector

    return vectors


def store_embedding(
    conn: sqlite3.Connection,
    phrase: EmbeddingPhrase,
    model_name: str,
    vector: list[float],
    created_at_utc: str,
) -> None:
    conn.execute(
        """
        INSERT INTO keyword_embeddings (
            keyword_id,
            keyword,
            model_name,
            embedding_json,
            dimension,
            created_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(keyword, model_name) DO UPDATE SET
            keyword_id = COALESCE(excluded.keyword_id, keyword_embeddings.keyword_id),
            embedding_json = excluded.embedding_json,
            dimension = excluded.dimension
        """,
        (
            phrase.keyword_id,
            phrase.keyword,
            model_name,
            json.dumps(vector),
            len(vector),
            created_at_utc,
        ),
    )


def store_similarity_edges(
    conn: sqlite3.Connection,
    edges: Iterable[SimilarityEdge],
    model_name: str,
    generation_run_id: int,
    created_at_utc: str,
) -> None:
    ensure_embedding_tables(conn)
    for edge in edges:
        conn.execute(
            """
            INSERT INTO keyword_similarity_edges (
                source_keyword_id,
                source_keyword,
                candidate_keyword,
                model_name,
                similarity_score,
                generation_run_id,
                created_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                edge.source_keyword_id,
                edge.source_keyword,
                edge.candidate_keyword,
                model_name,
                round(edge.similarity_score, 6),
                generation_run_id,
                created_at_utc,
            ),
        )


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0

    left_vector = np.asarray(left, dtype=float)
    right_vector = np.asarray(right, dtype=float)
    left_norm = np.linalg.norm(left_vector)
    right_norm = np.linalg.norm(right_vector)
    if left_norm == 0 or right_norm == 0:
        return 0.0

    return float(np.dot(left_vector, right_vector) / (left_norm * right_norm))


def unique_phrases(phrases: Iterable[EmbeddingPhrase]) -> list[EmbeddingPhrase]:
    seen = set()
    unique = []

    for phrase in phrases:
        keyword = clean_keyword(phrase.keyword)
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        unique.append(EmbeddingPhrase(keyword=keyword, keyword_id=phrase.keyword_id))

    return unique


def clean_keyword(value: str | None) -> str:
    if not value:
        return ""

    text = str(value).lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def vector_to_list(vector: Any) -> list[float]:
    if hasattr(vector, "tolist"):
        vector = vector.tolist()

    if not isinstance(vector, list):
        return []

    return normalize_vector(vector)


def normalize_vector(vector: list[Any]) -> list[float]:
    values = []
    for value in vector:
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            return []

    return values


def parse_vector_json(raw_json: str) -> list[float]:
    try:
        loaded = json.loads(raw_json)
    except json.JSONDecodeError:
        return []

    if not isinstance(loaded, list):
        return []

    return normalize_vector(loaded)
