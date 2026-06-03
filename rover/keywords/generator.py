import hashlib
import json
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from rover.common import (
    bounded_float,
    clean_text,
    elapsed_seconds,
    isoformat_utc,
    parse_bool,
    parse_utc,
    positive_int,
    read_yaml_mapping,
    utc_now,
)
from rover.db import ensure_database_schema, ensure_table_columns as ensure_db_table_columns
from rover.data_paths import default_db_path
from rover.keywords.embeddings import (
    DEFAULT_EMBEDDING_MODEL,
    EmbeddingPhrase,
    KeywordEmbeddingIndex,
    SimilarityEdge,
    cosine_similarity,
    store_similarity_edges,
)
from rover.keywords.llm_client import OpenRouterKeywordClient
from rover.pipeline_logging import configure_pipeline_logging, log_event


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = default_db_path()
DEFAULT_POLICY_PATH = PROJECT_ROOT / "config" / "keyword_policy.yaml"

DEFAULT_LIMIT = 50
DEFAULT_RECENTLY_TESTED_DAYS = 14
NEXT_RUN_DELAY = timedelta(hours=1)
ASIN_TOKEN_RE = re.compile(r"^b[a-z0-9]{9}$")

WINNER_DECISIONS = {"keep", "watchlist"}
DETERMINISTIC_ENGINES = (
    "search_term_mutation",
    "modifier_expansion",
    "winner_title_expansion",
)
LLM_ENGINE = "llm_expansion"
EMBEDDING_ENGINE = "embedding_neighbor"

STOPWORDS = {
    "a",
    "an",
    "and",
    "by",
    "for",
    "from",
    "in",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}
TITLE_STOPWORDS = STOPWORDS | {
    "assorted",
    "bundle",
    "count",
    "ct",
    "each",
    "new",
    "pack",
    "packs",
    "piece",
    "pieces",
    "set",
    "size",
}
BANNED_TERMS = {
    "amazon",
    "apple",
    "asin",
    "cbd",
    "counterfeit",
    "cream",
    "disney",
    "fake",
    "free",
    "lego",
    "medicine",
    "nike",
    "ointment",
    "pokemon",
    "prime",
    "review",
    "reviews",
    "selleramp",
    "used",
}
BANNED_PHRASES = {
    "baby formula",
}
OVERLY_BROAD_TERMS = {
    "accessories",
    "baby",
    "beauty",
    "book",
    "books",
    "clothes",
    "electronics",
    "food",
    "gift",
    "gifts",
    "health",
    "home",
    "kitchen",
    "office",
    "parts",
    "shoes",
    "supplies",
    "tool",
    "tools",
    "toy",
    "toys",
}
SUFFIX_MODIFIERS = (
    "bulk",
    "case",
    "commercial",
    "multipack",
    "professional",
    "refill",
    "replacement",
    "travel size",
)
PREFIX_MODIFIERS = (
    "compatible",
    "replacement",
    "wholesale",
)

@dataclass(frozen=True)
class SourceKeyword:
    id: int | None
    keyword: str
    historical_winner_rate: float = 0.0
    total_winners: int = 0
    total_candidates_found: int = 0


@dataclass(frozen=True)
class WinnerProduct:
    asin: str
    name: str
    brand: str | None = None
    category: str | None = None
    profit: float | None = None
    roi_percent: float | None = None


@dataclass(frozen=True)
class KeywordCandidate:
    keyword: str
    engine: str
    parent_keyword_id: int | None = None
    parent_keyword: str | None = None
    source_product_asin: str | None = None
    cluster_key: str = "generated"
    score: float = 0.0


@dataclass(frozen=True)
class CandidateDecision:
    candidate: KeywordCandidate
    normalized_keyword: str
    accepted: bool
    rejection_reason: str | None


@dataclass(frozen=True)
class LLMExpansionSettings:
    enabled: bool = False
    keywords_per_source: int = 30
    max_source_keywords: int = 5
    max_winning_titles: int = 20
    max_bad_keywords: int = 30


@dataclass(frozen=True)
class EmbeddingNeighborSettings:
    enabled: bool = False
    model_name: str = DEFAULT_EMBEDDING_MODEL
    top_k_neighbors: int = 5
    min_similarity: float = 0.55
    max_similarity_for_duplicate: float = 0.95
    max_source_keywords: int = 25
    max_candidate_keywords: int = 500
    include_previous_candidates: bool = True


@dataclass(frozen=True)
class GenerationSettings:
    llm: LLMExpansionSettings
    embeddings: EmbeddingNeighborSettings

    @property
    def enabled_engines(self) -> list[str]:
        engines = list(DETERMINISTIC_ENGINES)
        if self.llm.enabled:
            engines.append(LLM_ENGINE)
        if self.embeddings.enabled:
            engines.append(EMBEDDING_ENGINE)
        return engines

    @property
    def disabled_engines(self) -> list[str]:
        engines = []
        if not self.llm.enabled:
            engines.append(LLM_ENGINE)
        if not self.embeddings.enabled:
            engines.append(EMBEDDING_ENGINE)
        return engines


def generate_keywords(
    db_path: Path | str = DEFAULT_DB_PATH,
    limit: int = DEFAULT_LIMIT,
    recently_tested_days: int = DEFAULT_RECENTLY_TESTED_DAYS,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Generate, validate, store, and enqueue deterministic scrape keywords."""
    started_at = time.monotonic()
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    safe_limit = max(1, int(limit))
    settings = load_generation_settings()
    log_event(
        "keyword_generation_started",
        stage="Generate new keywords",
        db_path=path,
        limit=safe_limit,
        recently_tested_days=recently_tested_days,
        enabled_engines=settings.enabled_engines,
        disabled_engines=settings.disabled_engines,
    )

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_keyword_generation_schema(conn)
        run_id = start_generation_run(conn, safe_limit, now, settings)
        log_event(
            "keyword_generation_run_created",
            stage="Generate new keywords",
            generation_run_id=run_id,
            db_path=path,
        )

        try:
            source_keywords = fetch_source_keywords(conn)
            winner_products = fetch_winner_products(conn)
            log_event(
                "keyword_generation_sources_loaded",
                stage="Generate new keywords",
                generation_run_id=run_id,
                source_keyword_count=len(source_keywords),
                source_winner_count=len(winner_products),
                source_keyword_sample=[source.keyword for source in source_keywords[:10]],
                winner_asin_sample=[product.asin for product in winner_products[:10]],
            )
            raw_candidates = collect_candidates(
                conn=conn,
                run_id=run_id,
                now=now,
                source_keywords=source_keywords,
                winner_products=winner_products,
                settings=settings,
            )
            decisions = validate_candidates(
                conn=conn,
                candidates=raw_candidates,
                limit=safe_limit,
                recently_tested_days=recently_tested_days,
                now=now,
            )
            accepted_count = sum(1 for decision in decisions if decision.accepted)
            log_event(
                "keyword_generation_candidates_validated",
                stage="Generate new keywords",
                generation_run_id=run_id,
                candidates_generated=len(raw_candidates),
                candidates_by_engine=candidate_counts_by_engine(raw_candidates),
                candidates_accepted=accepted_count,
                rejection_reason_counts=rejection_counts(decisions),
            )
            inserted_count = store_generation_results(conn, run_id, decisions, now)
            finish_generation_run(
                conn=conn,
                run_id=run_id,
                status="completed",
                source_keyword_count=len(source_keywords),
                source_winner_count=len(winner_products),
                candidates_generated=len(raw_candidates),
                candidates_accepted=accepted_count,
                candidates_inserted=inserted_count,
                notes=generation_note(settings),
                now=now,
            )
            conn.commit()
            log_event(
                "keyword_generation_completed",
                stage="Generate new keywords",
                generation_run_id=run_id,
                elapsed_seconds=elapsed_seconds(started_at),
                source_keyword_count=len(source_keywords),
                source_winner_count=len(winner_products),
                candidates_generated=len(raw_candidates),
                candidates_by_engine=candidate_counts_by_engine(raw_candidates),
                candidates_accepted=accepted_count,
                candidates_inserted=inserted_count,
                enabled_engines=settings.enabled_engines,
                disabled_engines=settings.disabled_engines,
            )

            return {
                "db_path": str(path),
                "generation_run_id": run_id,
                "source_keyword_count": len(source_keywords),
                "source_winner_count": len(winner_products),
                "candidates_generated": len(raw_candidates),
                "candidates_accepted": accepted_count,
                "candidates_inserted": inserted_count,
                "enabled_engines": settings.enabled_engines,
                "disabled_engines": settings.disabled_engines,
            }
        except Exception as exc:
            finish_generation_run(
                conn=conn,
                run_id=run_id,
                status="failed",
                source_keyword_count=0,
                source_winner_count=0,
                candidates_generated=0,
                candidates_accepted=0,
                candidates_inserted=0,
                notes=str(exc),
                now=now,
            )
            conn.commit()
            log_event(
                "keyword_generation_failed",
                stage="Generate new keywords",
                level="ERROR",
                generation_run_id=run_id,
                elapsed_seconds=elapsed_seconds(started_at),
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise


def collect_candidates(
    conn: sqlite3.Connection,
    run_id: int,
    now: datetime,
    source_keywords: list[SourceKeyword],
    winner_products: list[WinnerProduct],
    settings: GenerationSettings,
) -> list[KeywordCandidate]:
    started_at = time.monotonic()
    deterministic_candidates = []
    search_candidates = search_term_mutation(source_keywords)
    modifier_candidates = modifier_expansion(source_keywords)
    title_candidates = winner_title_expansion(winner_products)
    deterministic_candidates.extend(search_candidates)
    deterministic_candidates.extend(modifier_candidates)
    deterministic_candidates.extend(title_candidates)
    log_event(
        "keyword_generation_deterministic_candidates_collected",
        stage="Generate new keywords",
        generation_run_id=run_id,
        search_term_mutation_count=len(search_candidates),
        modifier_expansion_count=len(modifier_candidates),
        winner_title_expansion_count=len(title_candidates),
    )

    llm_candidates = llm_expansion(source_keywords, winner_products, settings)
    seed_candidates = [*deterministic_candidates, *llm_candidates]
    embedding_candidates = embedding_neighbor(
        conn=conn,
        run_id=run_id,
        now=now,
        source_keywords=source_keywords,
        winner_products=winner_products,
        seed_candidates=seed_candidates,
        settings=settings,
    )
    candidates = [*deterministic_candidates, *llm_candidates, *embedding_candidates]
    log_event(
        "keyword_generation_candidates_collected",
        stage="Generate new keywords",
        generation_run_id=run_id,
        elapsed_seconds=elapsed_seconds(started_at),
        candidates_generated=len(candidates),
        candidates_by_engine=candidate_counts_by_engine(candidates),
    )
    return sorted(
        candidates,
        key=lambda candidate: (
            -candidate.score,
            candidate.engine,
            normalize_keyword(candidate.keyword),
            candidate.parent_keyword_id or 0,
            candidate.source_product_asin or "",
        ),
    )


def search_term_mutation(
    source_keywords: Iterable[SourceKeyword],
    per_source_limit: int = 8,
) -> list[KeywordCandidate]:
    candidates = []

    for source in source_keywords:
        tokens = keyword_tokens(source.keyword)
        if not tokens:
            continue

        variants = unique_preserving_order(
            [
                singularize_phrase(tokens),
                " ".join(token for token in tokens if token not in STOPWORDS),
                reverse_two_word_phrase(tokens),
                *sliding_phrases(tokens, minimum=2, maximum=3),
            ]
        )

        for variant in variants[:per_source_limit]:
            if not variant or normalize_keyword(variant) == normalize_keyword(source.keyword):
                continue
            candidates.append(
                KeywordCandidate(
                    keyword=variant,
                    engine="search_term_mutation",
                    parent_keyword_id=source.id,
                    parent_keyword=source.keyword,
                    cluster_key=cluster_key("search_term_mutation", source.keyword),
                    score=source_score(source, base=1.0),
                )
            )

    return candidates


def modifier_expansion(
    source_keywords: Iterable[SourceKeyword],
    per_source_limit: int = 10,
) -> list[KeywordCandidate]:
    candidates = []

    for source in source_keywords:
        base_keyword = normalize_keyword(source.keyword)
        if not base_keyword:
            continue

        variants = []
        variants.extend(f"{base_keyword} {modifier}" for modifier in SUFFIX_MODIFIERS)
        variants.extend(f"{modifier} {base_keyword}" for modifier in PREFIX_MODIFIERS)
        variants = unique_preserving_order(variants)

        for variant in variants[:per_source_limit]:
            candidates.append(
                KeywordCandidate(
                    keyword=variant,
                    engine="modifier_expansion",
                    parent_keyword_id=source.id,
                    parent_keyword=source.keyword,
                    cluster_key=cluster_key("modifier_expansion", source.keyword),
                    score=source_score(source, base=0.8),
                )
            )

    return candidates


def winner_title_expansion(
    winner_products: Iterable[WinnerProduct],
    per_product_limit: int = 12,
) -> list[KeywordCandidate]:
    candidates = []

    for product in winner_products:
        tokens = title_tokens(product.name)
        if len(tokens) < 2:
            continue

        phrases = []
        phrases.extend(sliding_phrases(tokens, minimum=2, maximum=4))
        phrases.extend(brand_phrases(product, tokens))
        phrases = unique_preserving_order(phrases)

        for phrase in phrases[:per_product_limit]:
            candidates.append(
                KeywordCandidate(
                    keyword=phrase,
                    engine="winner_title_expansion",
                    source_product_asin=product.asin,
                    cluster_key=cluster_key("winner_title_expansion", product.asin),
                    score=winner_score(product),
                )
            )

    return candidates


def llm_expansion(
    source_keywords: list[SourceKeyword],
    winner_products: list[WinnerProduct],
    settings: GenerationSettings,
) -> list[KeywordCandidate]:
    if not settings.llm.enabled:
        log_event(
            "keyword_generation_llm_skipped",
            stage="Generate new keywords",
            reason="disabled",
        )
        return []

    client = OpenRouterKeywordClient.from_env(PROJECT_ROOT)
    if not client.is_configured():
        log_event(
            "keyword_generation_llm_skipped",
            stage="Generate new keywords",
            level="WARNING",
            reason="missing_openrouter_api_key",
            model=client.config.model,
        )
        return []

    prioritized_sources = prioritize_source_keywords(source_keywords)
    if not prioritized_sources:
        log_event(
            "keyword_generation_llm_skipped",
            stage="Generate new keywords",
            reason="no_source_keywords",
        )
        return []

    winning_titles = [
        product.name
        for product in winner_products[: settings.llm.max_winning_titles]
        if product.name
    ]
    bad_keywords = bad_source_keywords(
        source_keywords,
        limit=settings.llm.max_bad_keywords,
    )
    banned_terms = sorted([*BANNED_TERMS, *BANNED_PHRASES])
    candidates = []

    for source in prioritized_sources[: settings.llm.max_source_keywords]:
        started_at = time.monotonic()
        log_event(
            "keyword_generation_llm_source_started",
            stage="Generate new keywords",
            base_keyword=source.keyword,
            limit=settings.llm.keywords_per_source,
            model=client.config.model,
        )
        ideas = client.generate_keyword_ideas(
            base_keyword=source.keyword,
            winning_titles=winning_titles,
            bad_keywords=bad_keywords,
            banned_terms=banned_terms,
            limit=settings.llm.keywords_per_source,
        )
        log_event(
            "keyword_generation_llm_source_completed",
            stage="Generate new keywords",
            elapsed_seconds=elapsed_seconds(started_at),
            base_keyword=source.keyword,
            ideas_returned=len(ideas),
        )
        for idea in ideas:
            candidates.append(
                KeywordCandidate(
                    keyword=idea.keyword,
                    engine=LLM_ENGINE,
                    parent_keyword_id=source.id,
                    parent_keyword=source.keyword,
                    cluster_key=cluster_key(LLM_ENGINE, source.keyword),
                    score=source_score(source, base=1.15),
                )
            )

    log_event(
        "keyword_generation_llm_completed",
        stage="Generate new keywords",
        source_count=min(len(prioritized_sources), settings.llm.max_source_keywords),
        candidate_count=len(candidates),
    )
    return candidates


def embedding_neighbor(
    conn: sqlite3.Connection,
    run_id: int,
    now: datetime,
    source_keywords: list[SourceKeyword],
    winner_products: list[WinnerProduct],
    seed_candidates: list[KeywordCandidate],
    settings: GenerationSettings,
) -> list[KeywordCandidate]:
    if not settings.embeddings.enabled:
        log_event(
            "keyword_generation_embedding_skipped",
            stage="Generate new keywords",
            reason="disabled",
        )
        return []

    anchors = embedding_anchor_sources(source_keywords, winner_products, settings)
    candidate_keywords = embedding_candidate_universe(conn, seed_candidates, winner_products, settings)
    if not anchors or not candidate_keywords:
        log_event(
            "keyword_generation_embedding_skipped",
            stage="Generate new keywords",
            reason="missing_anchors_or_candidates",
            anchor_count=len(anchors),
            candidate_keyword_count=len(candidate_keywords),
        )
        return []

    index = KeywordEmbeddingIndex(settings.embeddings.model_name)
    if not index.is_available():
        log_event(
            "keyword_generation_embedding_skipped",
            stage="Generate new keywords",
            level="WARNING",
            reason="embedding_index_unavailable",
            model_name=settings.embeddings.model_name,
        )
        return []

    created_at_utc = isoformat_utc(now)
    phrases = [
        EmbeddingPhrase(keyword=anchor.keyword, keyword_id=anchor.id)
        for anchor in anchors
    ]
    phrases.extend(EmbeddingPhrase(keyword=keyword) for keyword in candidate_keywords)

    vectors = index.vectors_for_phrases(conn, phrases, created_at_utc)
    if not vectors:
        log_event(
            "keyword_generation_embedding_skipped",
            stage="Generate new keywords",
            level="WARNING",
            reason="no_vectors_returned",
            phrase_count=len(phrases),
            model_name=settings.embeddings.model_name,
        )
        return []

    candidates = []
    edges = []
    seen = set()

    for anchor in anchors:
        anchor_keyword = normalize_keyword(anchor.keyword)
        anchor_vector = vectors.get(anchor_keyword)
        if not anchor_vector:
            continue

        ranked_neighbors = rank_embedding_neighbors(
            anchor=anchor,
            anchor_vector=anchor_vector,
            candidate_keywords=candidate_keywords,
            vectors=vectors,
            settings=settings,
        )
        for candidate_keyword, similarity in ranked_neighbors:
            if candidate_keyword in seen:
                continue

            seen.add(candidate_keyword)
            edges.append(
                SimilarityEdge(
                    source_keyword_id=anchor.id,
                    source_keyword=anchor.keyword,
                    candidate_keyword=candidate_keyword,
                    similarity_score=similarity,
                )
            )
            candidates.append(
                KeywordCandidate(
                    keyword=candidate_keyword,
                    engine=EMBEDDING_ENGINE,
                    parent_keyword_id=anchor.id,
                    parent_keyword=anchor.keyword,
                    cluster_key=cluster_key(EMBEDDING_ENGINE, anchor.keyword),
                    score=1.0 + similarity + min(anchor.historical_winner_rate, 1.0),
                )
            )

    store_similarity_edges(
        conn=conn,
        edges=edges,
        model_name=settings.embeddings.model_name,
        generation_run_id=run_id,
        created_at_utc=created_at_utc,
    )
    log_event(
        "keyword_generation_embedding_completed",
        stage="Generate new keywords",
        generation_run_id=run_id,
        model_name=settings.embeddings.model_name,
        anchor_count=len(anchors),
        candidate_keyword_count=len(candidate_keywords),
        vector_count=len(vectors),
        edge_count=len(edges),
        candidate_count=len(candidates),
    )
    return candidates


def load_generation_settings(policy_path: Path = DEFAULT_POLICY_PATH) -> GenerationSettings:
    generation_config = read_generation_config(policy_path)
    return GenerationSettings(
        llm=load_llm_settings(generation_config.get("llm_expansion")),
        embeddings=load_embedding_settings(generation_config.get("embedding_neighbor")),
    )


def read_generation_config(policy_path: Path) -> dict[str, Any]:
    loaded = read_yaml_mapping(policy_path, description="Keyword policy generation config")
    generation = loaded.get("generation")
    return generation if isinstance(generation, dict) else {}


def load_llm_settings(raw: Any) -> LLMExpansionSettings:
    data = raw if isinstance(raw, dict) else {}
    return LLMExpansionSettings(
        enabled=parse_bool(data.get("enabled"), default=False),
        keywords_per_source=positive_int(data.get("keywords_per_source"), 30),
        max_source_keywords=positive_int(data.get("max_source_keywords"), 5),
        max_winning_titles=positive_int(data.get("max_winning_titles"), 20),
        max_bad_keywords=positive_int(data.get("max_bad_keywords"), 30),
    )


def load_embedding_settings(raw: Any) -> EmbeddingNeighborSettings:
    data = raw if isinstance(raw, dict) else {}
    return EmbeddingNeighborSettings(
        enabled=parse_bool(data.get("enabled"), default=False),
        model_name=clean_text(data.get("model_name"), DEFAULT_EMBEDDING_MODEL),
        top_k_neighbors=positive_int(data.get("top_k_neighbors"), 5),
        min_similarity=bounded_float(data.get("min_similarity"), 0.55, minimum=0.0, maximum=1.0),
        max_similarity_for_duplicate=bounded_float(
            data.get("max_similarity_for_duplicate"),
            0.95,
            minimum=0.0,
            maximum=1.0,
        ),
        max_source_keywords=positive_int(data.get("max_source_keywords"), 25),
        max_candidate_keywords=positive_int(data.get("max_candidate_keywords"), 500),
        include_previous_candidates=parse_bool(data.get("include_previous_candidates"), default=True),
    )


def prioritize_source_keywords(source_keywords: list[SourceKeyword]) -> list[SourceKeyword]:
    return sorted(
        source_keywords,
        key=lambda source: (
            -source.total_winners,
            -source.historical_winner_rate,
            -source.total_candidates_found,
            normalize_keyword(source.keyword),
        ),
    )


def bad_source_keywords(source_keywords: list[SourceKeyword], limit: int) -> list[str]:
    bad_sources = [
        source
        for source in source_keywords
        if source.total_candidates_found == 0 or source.historical_winner_rate == 0
    ]
    bad_sources.sort(
        key=lambda source: (
            source.total_winners,
            source.historical_winner_rate,
            normalize_keyword(source.keyword),
        )
    )
    return [source.keyword for source in bad_sources[:limit]]


def embedding_anchor_sources(
    source_keywords: list[SourceKeyword],
    winner_products: list[WinnerProduct],
    settings: GenerationSettings,
) -> list[SourceKeyword]:
    anchors = prioritize_source_keywords(source_keywords)
    if anchors:
        return anchors[: settings.embeddings.max_source_keywords]

    title_sources = [
        SourceKeyword(
            id=None,
            keyword=phrase,
            historical_winner_rate=1.0,
            total_winners=1,
            total_candidates_found=1,
        )
        for phrase in winner_title_phrases(winner_products)
    ]
    return title_sources[: settings.embeddings.max_source_keywords]


def embedding_candidate_universe(
    conn: sqlite3.Connection,
    seed_candidates: list[KeywordCandidate],
    winner_products: list[WinnerProduct],
    settings: GenerationSettings,
) -> list[str]:
    candidates = []
    candidates.extend(candidate.keyword for candidate in seed_candidates)
    candidates.extend(winner_title_phrases(winner_products))

    if settings.embeddings.include_previous_candidates:
        candidates.extend(fetch_previous_candidate_keywords(conn, settings.embeddings.max_candidate_keywords))

    return unique_preserving_order(candidates)[: settings.embeddings.max_candidate_keywords]


def winner_title_phrases(winner_products: Iterable[WinnerProduct]) -> list[str]:
    phrases = []
    for product in winner_products:
        tokens = title_tokens(product.name)
        phrases.extend(sliding_phrases(tokens, minimum=2, maximum=4))
    return unique_preserving_order(phrases)


def fetch_previous_candidate_keywords(conn: sqlite3.Connection, limit: int) -> list[str]:
    if not table_exists(conn, "keyword_generation_candidates"):
        return []

    columns = table_columns(conn, "keyword_generation_candidates")
    keyword_column = first_existing_column(columns, ("normalized_keyword", "keyword", "candidate_keyword"))
    if not keyword_column:
        return []

    rows = conn.execute(
        f"""
        SELECT {keyword_column} AS keyword
        FROM keyword_generation_candidates
        WHERE {keyword_column} IS NOT NULL
          AND TRIM({keyword_column}) != ''
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [row["keyword"] for row in rows]


def rank_embedding_neighbors(
    anchor: SourceKeyword,
    anchor_vector: list[float],
    candidate_keywords: list[str],
    vectors: dict[str, list[float]],
    settings: GenerationSettings,
) -> list[tuple[str, float]]:
    anchor_keyword = normalize_keyword(anchor.keyword)
    ranked = []

    for candidate_keyword in candidate_keywords:
        normalized = normalize_keyword(candidate_keyword)
        if not normalized or normalized == anchor_keyword:
            continue

        candidate_vector = vectors.get(normalized)
        if not candidate_vector:
            continue

        similarity = cosine_similarity(anchor_vector, candidate_vector)
        if similarity < settings.embeddings.min_similarity:
            continue

        if similarity > settings.embeddings.max_similarity_for_duplicate:
            continue

        ranked.append((normalized, similarity))

    ranked.sort(key=lambda item: (-item[1], item[0]))
    return ranked[: settings.embeddings.top_k_neighbors]


def validate_candidates(
    conn: sqlite3.Connection,
    candidates: list[KeywordCandidate],
    limit: int,
    recently_tested_days: int,
    now: datetime,
) -> list[CandidateDecision]:
    existing_keywords = load_existing_keywords(conn)
    recent_cutoff = now - timedelta(days=max(0, recently_tested_days))
    seen: set[str] = set()
    accepted_count = 0
    decisions = []

    for candidate in candidates:
        normalized = normalize_keyword(candidate.keyword)
        rejection_reason = rejection_reason_for_keyword(
            normalized=normalized,
            existing_keywords=existing_keywords,
            recent_cutoff=recent_cutoff,
            seen=seen,
        )

        if rejection_reason is None and accepted_count >= limit:
            rejection_reason = "run_limit"

        accepted = rejection_reason is None
        if accepted:
            accepted_count += 1

        if normalized:
            seen.add(normalized)

        decisions.append(
            CandidateDecision(
                candidate=candidate,
                normalized_keyword=normalized,
                accepted=accepted,
                rejection_reason=rejection_reason,
            )
        )

    return decisions


def candidate_counts_by_engine(candidates: Iterable[KeywordCandidate]) -> dict[str, int]:
    return dict(Counter(candidate.engine for candidate in candidates))


def rejection_counts(decisions: Iterable[CandidateDecision]) -> dict[str, int]:
    return dict(
        Counter(
            decision.rejection_reason or "accepted"
            for decision in decisions
        )
    )


def rejection_reason_for_keyword(
    normalized: str,
    existing_keywords: dict[str, dict[str, Any]],
    recent_cutoff: datetime,
    seen: set[str],
) -> str | None:
    if not normalized:
        return "empty"

    if normalized in seen:
        return "duplicate_in_run"

    tokens = keyword_tokens(normalized)
    if not tokens:
        return "empty"

    if len(normalized) > 80:
        return "too_long"

    if all(token.isdigit() for token in tokens):
        return "numbers_only"

    if any(is_asin_like_token(token) for token in tokens):
        return "asin_like"

    if any(token in BANNED_TERMS for token in tokens):
        return "banned_term"

    if any(phrase in normalized for phrase in BANNED_PHRASES):
        return "banned_term"

    if normalized in OVERLY_BROAD_TERMS:
        return "overly_broad"

    if len(tokens) == 1 and (len(tokens[0]) < 4 or tokens[0] in OVERLY_BROAD_TERMS):
        return "overly_broad"

    existing = existing_keywords.get(normalized)
    if not existing:
        return None

    last_seen = latest_timestamp(existing)
    if last_seen and last_seen >= recent_cutoff:
        return "tested_recently"

    return "already_exists"


def store_generation_results(
    conn: sqlite3.Connection,
    run_id: int,
    decisions: list[CandidateDecision],
    now: datetime,
) -> int:
    inserted_count = 0
    created_at = isoformat_utc(now)
    next_eligible_at = isoformat_utc(now + NEXT_RUN_DELAY)

    for decision in decisions:
        inserted_keyword_id = None

        if decision.accepted:
            inserted_keyword_id = insert_generated_keyword(
                conn=conn,
                decision=decision,
                created_at=created_at,
                next_eligible_at=next_eligible_at,
            )
            if inserted_keyword_id is not None:
                inserted_count += 1

        store_candidate_decision(
            conn=conn,
            run_id=run_id,
            decision=decision,
            inserted_keyword_id=inserted_keyword_id,
            created_at=created_at,
        )

    return inserted_count


def insert_generated_keyword(
    conn: sqlite3.Connection,
    decision: CandidateDecision,
    created_at: str,
    next_eligible_at: str,
) -> int | None:
    candidate = decision.candidate
    cursor = conn.execute(
        """
        INSERT INTO scrape_keywords (
            keyword,
            status,
            first_seen_at_utc,
            origin,
            parent_keyword_id,
            source_product_asin,
            cluster_key,
            lifecycle_state,
            created_at_utc,
            updated_at_utc,
            next_eligible_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(keyword) DO NOTHING
        """,
        (
            decision.normalized_keyword,
            "pending",
            created_at,
            candidate.engine,
            candidate.parent_keyword_id,
            candidate.source_product_asin,
            candidate.cluster_key,
            "new",
            created_at,
            created_at,
            next_eligible_at,
        ),
    )

    if cursor.rowcount == 0:
        return None

    row = conn.execute(
        """
        SELECT id
        FROM scrape_keywords
        WHERE keyword = ?
        """,
        (decision.normalized_keyword,),
    ).fetchone()
    return int(row["id"]) if row else None


def store_candidate_decision(
    conn: sqlite3.Connection,
    run_id: int,
    decision: CandidateDecision,
    inserted_keyword_id: int | None,
    created_at: str,
) -> None:
    candidate = decision.candidate
    values = {
        "generation_run_id": run_id,
        "candidate_keyword": candidate.keyword,
        "normalized_keyword": decision.normalized_keyword,
        "engine": candidate.engine,
        "parent_keyword_id": candidate.parent_keyword_id,
        "parent_keyword": candidate.parent_keyword,
        "source_product_asin": candidate.source_product_asin,
        "cluster_key": candidate.cluster_key,
        "score": round(candidate.score, 4),
        "accepted": 1 if decision.accepted else 0,
        "rejection_reason": decision.rejection_reason,
        "inserted_keyword_id": inserted_keyword_id,
        "created_at_utc": created_at,
        "keyword": decision.normalized_keyword,
        "status": "accepted" if decision.accepted else "rejected",
        "accepted_keyword_id": inserted_keyword_id,
        "reject_reason": decision.rejection_reason,
    }
    insert_dynamic(conn, "keyword_generation_candidates", values)


def ensure_keyword_generation_schema(conn: sqlite3.Connection) -> None:
    ensure_database_schema(conn)


def ensure_scrape_keyword_schema(conn: sqlite3.Connection) -> None:
    ensure_database_schema(conn)


def ensure_keyword_source_tables(conn: sqlite3.Connection) -> None:
    ensure_database_schema(conn)


def ensure_generation_tables(conn: sqlite3.Connection) -> None:
    ensure_database_schema(conn)


def backfill_keyword_extension_fields(conn: sqlite3.Connection) -> None:
    ensure_database_schema(conn)


def start_generation_run(
    conn: sqlite3.Connection,
    requested_limit: int,
    now: datetime,
    settings: GenerationSettings,
) -> int:
    values = {
        "engine": "deterministic_pipeline",
        "source_keyword_id": None,
        "source_keyword": None,
        "source_product_asin": None,
        "started_at_utc": isoformat_utc(now),
        "status": "in_progress",
        "prompt": generation_note(settings),
        "requested_limit": requested_limit,
        "engines_enabled": json.dumps(settings.enabled_engines),
        "engines_disabled": json.dumps(settings.disabled_engines),
    }
    cursor = insert_dynamic(conn, "keyword_generation_runs", values)
    return int(cursor.lastrowid)


def finish_generation_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    source_keyword_count: int,
    source_winner_count: int,
    candidates_generated: int,
    candidates_accepted: int,
    candidates_inserted: int,
    notes: str,
    now: datetime,
) -> None:
    rejected_count = max(candidates_generated - candidates_accepted, 0)
    values = {
        "finished_at_utc": isoformat_utc(now),
        "status": status,
        "source_keyword_count": source_keyword_count,
        "source_winner_count": source_winner_count,
        "candidates_generated": candidates_generated,
        "candidates_accepted": candidates_accepted,
        "candidates_inserted": candidates_inserted,
        "generated_count": candidates_generated,
        "accepted_count": candidates_accepted,
        "rejected_count": rejected_count,
        "notes": notes,
        "error_message": notes if status == "failed" else None,
    }
    update_dynamic(conn, "keyword_generation_runs", values, "id = ?", (run_id,))


def fetch_source_keywords(conn: sqlite3.Connection, limit: int = 200) -> list[SourceKeyword]:
    sources = []

    for row in conn.execute(
        """
        SELECT
            id,
            keyword,
            COALESCE(historical_winner_rate, 0) AS historical_winner_rate,
            COALESCE(total_winners, 0) AS total_winners,
            COALESCE(total_candidates_found, 0) AS total_candidates_found
        FROM scrape_keywords
        WHERE keyword IS NOT NULL
          AND TRIM(keyword) != ''
          AND COALESCE(lifecycle_state, 'active') != 'retired'
        ORDER BY
            COALESCE(total_winners, 0) DESC,
            COALESCE(historical_winner_rate, 0) DESC,
            COALESCE(total_candidates_found, 0) DESC,
            id ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall():
        sources.append(
            SourceKeyword(
                id=int(row["id"]),
                keyword=row["keyword"],
                historical_winner_rate=float(row["historical_winner_rate"] or 0),
                total_winners=int(row["total_winners"] or 0),
                total_candidates_found=int(row["total_candidates_found"] or 0),
            )
        )

    sources.extend(fetch_product_search_terms(conn, limit=limit))
    return dedupe_source_keywords(sources)[:limit]


def fetch_product_search_terms(conn: sqlite3.Connection, limit: int = 200) -> list[SourceKeyword]:
    if not table_exists(conn, "products"):
        return []

    available_columns = table_columns(conn, "products")
    term_columns = [
        column
        for column in ("scrape_keyword", "selleramp_search_term", "search_term")
        if column in available_columns
    ]
    if not term_columns:
        return []

    select_parts = [
        f"""
        SELECT {column} AS keyword, COUNT(*) AS product_count
        FROM products
        WHERE {column} IS NOT NULL
          AND TRIM({column}) != ''
        GROUP BY {column}
        """
        for column in term_columns
    ]
    rows = conn.execute(
        f"""
        SELECT keyword, SUM(product_count) AS product_count
        FROM (
            {" UNION ALL ".join(select_parts)}
        )
        GROUP BY keyword
        ORDER BY product_count DESC, keyword ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    sources = []
    for row in rows:
        keyword = normalize_keyword(row["keyword"])
        if has_asin_like_token(keyword):
            continue
        sources.append(
            SourceKeyword(
                id=None,
                keyword=keyword,
                total_candidates_found=int(row["product_count"] or 0),
            )
        )

    return sources


def fetch_winner_products(conn: sqlite3.Connection, limit: int = 100) -> list[WinnerProduct]:
    if not table_exists(conn, "products") or not table_exists(conn, "product_reviews"):
        return []

    if "name" not in table_columns(conn, "products"):
        return []

    order_sql = product_order_sql(conn)
    rows = conn.execute(
        f"""
        WITH latest_products AS (
            SELECT *
            FROM (
                SELECT
                    p.*,
                    ROW_NUMBER() OVER (
                        PARTITION BY p.asin
                        ORDER BY {order_sql}
                    ) AS row_number
                FROM products p
            )
            WHERE row_number = 1
        )
        SELECT
            p.asin,
            p.name,
            {optional_product_column(conn, "brand")} AS brand,
            {optional_product_column(conn, "category")} AS category,
            {optional_product_column(conn, "profit")} AS profit,
            {optional_product_column(conn, "roi_percent")} AS roi_percent
        FROM product_reviews r
        JOIN latest_products p ON p.asin = r.asin
        WHERE r.decision IN ('keep', 'watchlist')
          AND p.name IS NOT NULL
          AND TRIM(p.name) != ''
        ORDER BY
            COALESCE(profit, 0) DESC,
            COALESCE(roi_percent, 0) DESC,
            p.asin ASC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    return [
        WinnerProduct(
            asin=row["asin"],
            name=row["name"],
            brand=row["brand"],
            category=row["category"],
            profit=row["profit"],
            roi_percent=row["roi_percent"],
        )
        for row in rows
    ]


def load_existing_keywords(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            keyword,
            first_seen_at_utc,
            last_scraped_at_utc,
            created_at_utc,
            updated_at_utc
        FROM scrape_keywords
        """
    ).fetchall()

    existing = {}
    for row in rows:
        normalized = normalize_keyword(row["keyword"])
        if not normalized:
            continue
        existing[normalized] = dict(row)

    return existing


def latest_timestamp(row: dict[str, Any]) -> datetime | None:
    values = [
        parse_utc(row.get("last_scraped_at_utc")),
        parse_utc(row.get("updated_at_utc")),
        parse_utc(row.get("created_at_utc")),
        parse_utc(row.get("first_seen_at_utc")),
    ]
    parsed_values = [value for value in values if value is not None]
    if not parsed_values:
        return None
    return max(parsed_values)


def keyword_tokens(keyword: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", normalize_keyword(keyword))


def has_asin_like_token(keyword: str) -> bool:
    return any(is_asin_like_token(token) for token in keyword_tokens(keyword))


def is_asin_like_token(token: str) -> bool:
    return ASIN_TOKEN_RE.fullmatch(token) is not None


def title_tokens(title: str) -> list[str]:
    return [
        token
        for token in keyword_tokens(title)
        if token not in TITLE_STOPWORDS and len(token) >= 3
    ]


def normalize_keyword(keyword: str | None) -> str:
    if not keyword:
        return ""

    text = keyword.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def singularize_phrase(tokens: list[str]) -> str:
    return " ".join(singularize_token(token) for token in tokens)


def singularize_token(token: str) -> str:
    if len(token) <= 4:
        return token
    if token.endswith("ies"):
        return token[:-3] + "y"
    if token.endswith("ses"):
        return token[:-2]
    if token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def reverse_two_word_phrase(tokens: list[str]) -> str:
    if len(tokens) != 2:
        return ""
    return " ".join(reversed(tokens))


def sliding_phrases(tokens: list[str], minimum: int, maximum: int) -> list[str]:
    phrases = []

    for size in range(minimum, maximum + 1):
        if len(tokens) < size:
            continue
        for start in range(0, len(tokens) - size + 1):
            phrase = " ".join(tokens[start : start + size])
            phrases.append(phrase)

    return phrases


def brand_phrases(product: WinnerProduct, tokens: list[str]) -> list[str]:
    brand = normalize_keyword(product.brand)
    if not brand:
        return []

    brand_tokens = set(keyword_tokens(brand))
    descriptors = [token for token in tokens if token not in brand_tokens]
    if not descriptors:
        return []

    phrases = [f"{brand} {' '.join(descriptors[:2])}"]
    if product.category:
        category_tokens = [
            token
            for token in keyword_tokens(product.category)
            if token not in STOPWORDS and token not in OVERLY_BROAD_TERMS
        ]
        if category_tokens:
            phrases.append(f"{brand} {' '.join(category_tokens[:2])}")

    return phrases


def unique_preserving_order(values: Iterable[str]) -> list[str]:
    seen = set()
    unique_values = []

    for value in values:
        normalized = normalize_keyword(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_values.append(normalized)

    return unique_values


def source_score(source: SourceKeyword, base: float) -> float:
    return (
        base
        + min(source.historical_winner_rate, 1.0)
        + min(source.total_winners, 10) * 0.1
        + min(source.total_candidates_found, 50) * 0.005
    )


def winner_score(product: WinnerProduct) -> float:
    profit_score = max(float(product.profit or 0), 0.0) * 0.02
    roi_score = max(float(product.roi_percent or 0), 0.0) * 0.005
    return 1.2 + min(profit_score, 1.0) + min(roi_score, 1.0)


def cluster_key(engine: str, seed: str) -> str:
    digest = hashlib.sha1(f"{engine}:{normalize_keyword(seed)}".encode("utf-8")).hexdigest()
    return f"{engine}:{digest[:12]}"


def dedupe_source_keywords(sources: Iterable[SourceKeyword]) -> list[SourceKeyword]:
    seen = set()
    unique_sources = []

    for source in sources:
        normalized = normalize_keyword(source.keyword)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique_sources.append(source)

    return unique_sources


def product_order_sql(conn: sqlite3.Connection) -> str:
    if column_exists(conn, "products", "imported_at_utc"):
        return "p.imported_at_utc DESC, p.id DESC"
    return "p.id DESC"


def optional_product_column(conn: sqlite3.Connection, column_name: str) -> str:
    if column_exists(conn, "products", column_name):
        return f"p.{column_name}"
    return "NULL"


def add_missing_columns(
    conn: sqlite3.Connection,
    table_name: str,
    required_columns: dict[str, str],
) -> None:
    ensure_db_table_columns(conn, table_name, required_columns)


def insert_dynamic(
    conn: sqlite3.Connection,
    table_name: str,
    values: dict[str, Any],
) -> sqlite3.Cursor:
    columns = [column for column in values if column_exists(conn, table_name, column)]
    placeholders = ", ".join("?" for _ in columns)
    column_sql = ", ".join(columns)
    return conn.execute(
        f"INSERT INTO {table_name} ({column_sql}) VALUES ({placeholders})",
        [values[column] for column in columns],
    )


def update_dynamic(
    conn: sqlite3.Connection,
    table_name: str,
    values: dict[str, Any],
    where_sql: str,
    where_params: tuple[Any, ...],
) -> None:
    columns = [column for column in values if column_exists(conn, table_name, column)]
    if not columns:
        return

    set_sql = ", ".join(f"{column} = ?" for column in columns)
    params = [values[column] for column in columns]
    params.extend(where_params)
    conn.execute(f"UPDATE {table_name} SET {set_sql} WHERE {where_sql}", params)


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})")}


def column_exists(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    return column_name in table_columns(conn, table_name)


def first_existing_column(columns: set[str], names: tuple[str, ...]) -> str | None:
    for name in names:
        if name in columns:
            return name

    return None


def generation_note(settings: GenerationSettings) -> str:
    return json.dumps(
        {
            "enabled_engines": settings.enabled_engines,
            "disabled_engines": settings.disabled_engines,
            "llm_expansion": {
                "enabled": settings.llm.enabled,
                "keywords_per_source": settings.llm.keywords_per_source,
                "max_source_keywords": settings.llm.max_source_keywords,
            },
            "embedding_neighbor": {
                "enabled": settings.embeddings.enabled,
                "model_name": settings.embeddings.model_name,
                "top_k_neighbors": settings.embeddings.top_k_neighbors,
                "min_similarity": settings.embeddings.min_similarity,
                "max_similarity_for_duplicate": settings.embeddings.max_similarity_for_duplicate,
            },
        },
        sort_keys=True,
    )


def main() -> int:
    configure_pipeline_logging(PROJECT_ROOT)
    summary = generate_keywords(
        db_path=DEFAULT_DB_PATH,
        limit=DEFAULT_LIMIT,
        recently_tested_days=DEFAULT_RECENTLY_TESTED_DAYS,
    )
    print(
        "Generated {candidates_inserted} new keywords from "
        "{candidates_generated} candidates in run {generation_run_id}.".format(**summary)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
