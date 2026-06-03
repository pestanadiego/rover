import sqlite3

from rover.common import utc_now_iso


SCHEMA_PRAGMA = "PRAGMA journal_mode = WAL"

SCRAPE_KEYWORD_DEFAULT_ORIGIN = "manual_seed"
SCRAPE_KEYWORD_DEFAULT_LIFECYCLE = "new"
SCRAPE_KEYWORD_DEFAULT_CLUSTER = "dummy"

AGENT_REVIEW_RUN_PRODUCTS_COLUMNS = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "run_id": "INTEGER NOT NULL",
    "asin": "TEXT NOT NULL",
    "status": "TEXT NOT NULL",
    "decision": "TEXT",
    "notes": "TEXT",
    "analysis": "TEXT",
    "summary": "TEXT",
    "web_research_summary": "TEXT",
    "source_urls_json": "TEXT NOT NULL DEFAULT '[]'",
    "error_message": "TEXT",
    "reviewed_at_utc": "TEXT NOT NULL",
}


def ensure_database_schema(conn: sqlite3.Connection) -> None:
    """Create every application table and keep legacy databases compatible."""
    if not conn.in_transaction:
        conn.execute(SCHEMA_PRAGMA)
    ensure_product_tables(conn)
    ensure_keyword_tables(conn)
    ensure_review_tables(conn)
    ensure_agent_review_tables(conn)
    ensure_pipeline_runs_table(conn)
    ensure_embedding_tables(conn)


def ensure_product_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            row_hash TEXT NOT NULL UNIQUE,
            asin TEXT NOT NULL,
            name TEXT,
            amazon_url TEXT,
            image_url TEXT,
            category TEXT,
            brand TEXT,
            manufacturer TEXT,
            upc TEXT,
            ean TEXT,
            weight_grams INTEGER,
            scrape_keyword TEXT,
            selleramp_search_term TEXT,
            quantity INTEGER,
            exported_at_utc TEXT,
            sales_marketplace TEXT,
            sales_currency TEXT,
            home_marketplace TEXT,
            home_currency TEXT,
            cost_price REAL,
            sale_price REAL,
            buy_box_current REAL,
            buy_box_average_180d REAL,
            breakeven REAL,
            max_cost REAL,
            sale_price_for_30_roi REAL,
            fba_fee REAL,
            referral_fee REAL,
            fbm_fulfilment_cost REAL,
            vat REAL,
            total_fees REAL,
            profit REAL,
            roi_percent REAL,
            profit_margin_percent REAL,
            sales_rank_current INTEGER,
            estimated_sales INTEGER,
            fba_seller_count INTEGER,
            fbm_seller_count INTEGER,
            total_seller_count INTEGER,
            spread_to_max_cost REAL,
            spread_to_breakeven REAL,
            buy_box_delta_percent REAL,
            data_quality TEXT NOT NULL,
            validation_warnings TEXT NOT NULL,
            record_json TEXT NOT NULL,
            raw_file TEXT NOT NULL,
            raw_row_number INTEGER NOT NULL,
            imported_at_utc TEXT NOT NULL
        )
        """
    )
    ensure_table_columns(
        conn,
        "products",
        {
            "scrape_keyword": "TEXT",
            "selleramp_search_term": "TEXT",
        },
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_asin ON products (asin)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_scrape_keyword ON products (scrape_keyword)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_products_roi ON products (roi_percent)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_sales_rank ON products (sales_rank_current)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_products_data_quality ON products (data_quality)"
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_products_raw_row
        ON products (raw_file, raw_row_number)
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rejected_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            raw_file TEXT NOT NULL,
            raw_row_number INTEGER NOT NULL,
            asin TEXT,
            errors_json TEXT NOT NULL,
            raw_row_json TEXT NOT NULL,
            imported_at_utc TEXT NOT NULL
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS normalization_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_file TEXT NOT NULL,
            imported_at_utc TEXT NOT NULL,
            total_rows INTEGER NOT NULL DEFAULT 0,
            saved_rows INTEGER NOT NULL DEFAULT 0,
            inserted_rows INTEGER NOT NULL DEFAULT 0,
            updated_rows INTEGER NOT NULL DEFAULT 0,
            unchanged_rows INTEGER NOT NULL DEFAULT 0,
            rejected_rows INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    ensure_table_columns(
        conn,
        "normalization_runs",
        {
            "inserted_rows": "INTEGER NOT NULL DEFAULT 0",
            "updated_rows": "INTEGER NOT NULL DEFAULT 0",
            "unchanged_rows": "INTEGER NOT NULL DEFAULT 0",
        },
    )


def ensure_keyword_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scrape_keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            first_seen_at_utc TEXT NOT NULL,
            last_scraped_at_utc TEXT,
            last_result_count INTEGER NOT NULL DEFAULT 0,
            total_products_found INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            lifecycle_state TEXT NOT NULL DEFAULT 'new',
            origin TEXT DEFAULT 'manual_seed',
            parent_keyword_id INTEGER,
            source_product_asin TEXT,
            source_engine_run_id INTEGER,
            cluster_key TEXT DEFAULT 'dummy',
            created_at_utc TEXT,
            updated_at_utc TEXT,
            next_eligible_at_utc TEXT,
            cooldown_until_utc TEXT,
            last_run_status TEXT,
            run_count INTEGER NOT NULL DEFAULT 0,
            zero_result_streak INTEGER NOT NULL DEFAULT 0,
            total_candidates_found INTEGER NOT NULL DEFAULT 0,
            total_winners INTEGER NOT NULL DEFAULT 0,
            total_rejections INTEGER NOT NULL DEFAULT 0,
            historical_winner_rate REAL NOT NULL DEFAULT 0,
            avg_profit REAL,
            avg_roi_percent REAL,
            priority_score REAL,
            retired_reason TEXT
        )
        """
    )
    ensure_table_columns(
        conn,
        "scrape_keywords",
        {
            "lifecycle_state": "TEXT NOT NULL DEFAULT 'new'",
            "origin": "TEXT DEFAULT 'manual_seed'",
            "parent_keyword_id": "INTEGER",
            "source_product_asin": "TEXT",
            "source_engine_run_id": "INTEGER",
            "cluster_key": "TEXT DEFAULT 'dummy'",
            "created_at_utc": "TEXT",
            "updated_at_utc": "TEXT",
            "next_eligible_at_utc": "TEXT",
            "cooldown_until_utc": "TEXT",
            "last_run_status": "TEXT",
            "run_count": "INTEGER NOT NULL DEFAULT 0",
            "zero_result_streak": "INTEGER NOT NULL DEFAULT 0",
            "total_candidates_found": "INTEGER NOT NULL DEFAULT 0",
            "total_winners": "INTEGER NOT NULL DEFAULT 0",
            "total_rejections": "INTEGER NOT NULL DEFAULT 0",
            "historical_winner_rate": "REAL NOT NULL DEFAULT 0",
            "avg_profit": "REAL",
            "avg_roi_percent": "REAL",
            "priority_score": "REAL",
            "retired_reason": "TEXT",
        },
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS keyword_scrape_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword_id INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT,
            status TEXT NOT NULL,
            products_exported INTEGER NOT NULL DEFAULT 0,
            sheet_rows_updated INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            scheduler_run_id INTEGER,
            scheduled_keyword_id INTEGER,
            priority_score REAL,
            selection_bucket TEXT,
            products_seen INTEGER NOT NULL DEFAULT 0,
            duplicate_asins_skipped INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    ensure_table_columns(
        conn,
        "keyword_scrape_runs",
        {
            "scheduler_run_id": "INTEGER",
            "scheduled_keyword_id": "INTEGER",
            "priority_score": "REAL",
            "selection_bucket": "TEXT",
            "products_seen": "INTEGER NOT NULL DEFAULT 0",
            "duplicate_asins_skipped": "INTEGER NOT NULL DEFAULT 0",
        },
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_keyword_scrape_runs_keyword_id
        ON keyword_scrape_runs (keyword_id)
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_keyword_matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asin TEXT NOT NULL,
            keyword_id INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            product_id INTEGER,
            normalization_run_id INTEGER,
            scrape_run_id INTEGER,
            raw_file TEXT,
            raw_row_number INTEGER,
            exported_at_utc TEXT,
            imported_at_utc TEXT NOT NULL,
            UNIQUE(asin, keyword_id, raw_file, raw_row_number)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_product_keyword_matches_asin
        ON product_keyword_matches (asin)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_product_keyword_matches_keyword_id
        ON product_keyword_matches (keyword_id)
        """
    )

    ensure_scheduler_tables(conn)
    ensure_keyword_generation_tables(conn)
    ensure_keyword_indexes(conn)
    backfill_keyword_defaults(conn)


def ensure_scheduler_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduler_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scheduled_for_utc TEXT NOT NULL,
            started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT,
            status TEXT NOT NULL,
            target_keyword_count INTEGER NOT NULL,
            selected_keyword_count INTEGER NOT NULL DEFAULT 0,
            notes TEXT
        )
        """
    )
    ensure_table_columns(
        conn,
        "scheduler_runs",
        {
            "scheduled_for_utc": "TEXT",
            "started_at_utc": "TEXT",
            "finished_at_utc": "TEXT",
            "status": "TEXT",
            "target_keyword_count": "INTEGER NOT NULL DEFAULT 0",
            "selected_keyword_count": "INTEGER NOT NULL DEFAULT 0",
            "notes": "TEXT",
        },
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduled_keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scheduler_run_id INTEGER NOT NULL,
            keyword_id INTEGER NOT NULL,
            keyword TEXT NOT NULL,
            selection_bucket TEXT NOT NULL,
            priority_score REAL NOT NULL,
            rank INTEGER NOT NULL,
            reason_json TEXT,
            status TEXT NOT NULL DEFAULT 'selected',
            created_at_utc TEXT NOT NULL
        )
        """
    )
    ensure_table_columns(
        conn,
        "scheduled_keywords",
        {
            "scheduler_run_id": "INTEGER",
            "keyword_id": "INTEGER",
            "keyword": "TEXT",
            "selection_bucket": "TEXT",
            "priority_score": "REAL NOT NULL DEFAULT 0",
            "rank": "INTEGER NOT NULL DEFAULT 0",
            "reason_json": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'selected'",
            "created_at_utc": "TEXT",
        },
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_scheduled_keywords_run
        ON scheduled_keywords (scheduler_run_id, rank)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_scheduled_keywords_keyword_id
        ON scheduled_keywords (keyword_id)
        """
    )


def ensure_keyword_generation_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS keyword_generation_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            engine TEXT DEFAULT 'deterministic_pipeline',
            source_keyword_id INTEGER,
            source_keyword TEXT,
            source_product_asin TEXT,
            started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT,
            status TEXT NOT NULL,
            prompt TEXT,
            generated_count INTEGER NOT NULL DEFAULT 0,
            accepted_count INTEGER NOT NULL DEFAULT 0,
            rejected_count INTEGER NOT NULL DEFAULT 0,
            error_message TEXT,
            requested_limit INTEGER NOT NULL DEFAULT 0,
            engines_enabled TEXT NOT NULL DEFAULT '[]',
            engines_disabled TEXT NOT NULL DEFAULT '[]',
            source_keyword_count INTEGER NOT NULL DEFAULT 0,
            source_winner_count INTEGER NOT NULL DEFAULT 0,
            candidates_generated INTEGER NOT NULL DEFAULT 0,
            candidates_accepted INTEGER NOT NULL DEFAULT 0,
            candidates_inserted INTEGER NOT NULL DEFAULT 0,
            notes TEXT
        )
        """
    )
    ensure_table_columns(
        conn,
        "keyword_generation_runs",
        {
            "engine": "TEXT DEFAULT 'unknown'",
            "source_keyword_id": "INTEGER",
            "source_keyword": "TEXT",
            "source_product_asin": "TEXT",
            "prompt": "TEXT",
            "generated_count": "INTEGER NOT NULL DEFAULT 0",
            "accepted_count": "INTEGER NOT NULL DEFAULT 0",
            "rejected_count": "INTEGER NOT NULL DEFAULT 0",
            "error_message": "TEXT",
            "requested_limit": "INTEGER NOT NULL DEFAULT 0",
            "engines_enabled": "TEXT NOT NULL DEFAULT '[]'",
            "engines_disabled": "TEXT NOT NULL DEFAULT '[]'",
            "source_keyword_count": "INTEGER NOT NULL DEFAULT 0",
            "source_winner_count": "INTEGER NOT NULL DEFAULT 0",
            "candidates_generated": "INTEGER NOT NULL DEFAULT 0",
            "candidates_accepted": "INTEGER NOT NULL DEFAULT 0",
            "candidates_inserted": "INTEGER NOT NULL DEFAULT 0",
            "notes": "TEXT",
        },
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS keyword_generation_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            generation_run_id INTEGER NOT NULL,
            keyword TEXT,
            status TEXT,
            accepted_keyword_id INTEGER,
            reject_reason TEXT,
            candidate_keyword TEXT,
            normalized_keyword TEXT,
            engine TEXT,
            parent_keyword_id INTEGER,
            parent_keyword TEXT,
            source_product_asin TEXT,
            cluster_key TEXT,
            score REAL NOT NULL DEFAULT 0,
            accepted INTEGER NOT NULL DEFAULT 0,
            rejection_reason TEXT,
            inserted_keyword_id INTEGER,
            created_at_utc TEXT NOT NULL
        )
        """
    )
    ensure_table_columns(
        conn,
        "keyword_generation_candidates",
        {
            "keyword": "TEXT",
            "status": "TEXT",
            "accepted_keyword_id": "INTEGER",
            "reject_reason": "TEXT",
            "candidate_keyword": "TEXT",
            "normalized_keyword": "TEXT",
            "engine": "TEXT",
            "parent_keyword_id": "INTEGER",
            "parent_keyword": "TEXT",
            "source_product_asin": "TEXT",
            "cluster_key": "TEXT",
            "score": "REAL NOT NULL DEFAULT 0",
            "accepted": "INTEGER NOT NULL DEFAULT 0",
            "rejection_reason": "TEXT",
            "inserted_keyword_id": "INTEGER",
        },
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_keyword_generation_candidates_run
        ON keyword_generation_candidates (generation_run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_keyword_generation_candidates_keyword
        ON keyword_generation_candidates (keyword)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_keyword_generation_candidates_normalized
        ON keyword_generation_candidates (normalized_keyword)
        """
    )


def ensure_keyword_indexes(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_scrape_keywords_lifecycle
        ON scrape_keywords (lifecycle_state)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_scrape_keywords_lifecycle_eligible
        ON scrape_keywords (lifecycle_state, next_eligible_at_utc)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_scrape_keywords_origin
        ON scrape_keywords (origin)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_scrape_keywords_eligible
        ON scrape_keywords (next_eligible_at_utc, cooldown_until_utc)
        """
    )


def ensure_review_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_reviews (
            asin TEXT PRIMARY KEY,
            product_id INTEGER,
            status TEXT NOT NULL DEFAULT 'pending',
            decision TEXT,
            notes TEXT,
            analysis TEXT,
            summary TEXT,
            decided_at_utc TEXT,
            updated_at_utc TEXT NOT NULL
        )
        """
    )
    ensure_table_columns(
        conn,
        "product_reviews",
        {
            "analysis": "TEXT",
            "summary": "TEXT",
        },
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS product_review_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asin TEXT NOT NULL,
            product_id INTEGER,
            previous_status TEXT,
            new_status TEXT,
            decision TEXT,
            notes TEXT,
            analysis TEXT,
            summary TEXT,
            created_at_utc TEXT NOT NULL
        )
        """
    )
    ensure_table_columns(
        conn,
        "product_review_events",
        {
            "analysis": "TEXT",
            "summary": "TEXT",
        },
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_product_reviews_status
        ON product_reviews (status)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_product_reviews_decision
        ON product_reviews (decision)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_product_review_events_asin
        ON product_review_events (asin)
        """
    )


def ensure_agent_review_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_review_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT,
            status TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            model TEXT NOT NULL,
            scheduler_run_id INTEGER,
            normalization_run_id INTEGER,
            products_selected INTEGER NOT NULL DEFAULT 0,
            products_reviewed INTEGER NOT NULL DEFAULT 0,
            products_failed INTEGER NOT NULL DEFAULT 0,
            keep_count INTEGER NOT NULL DEFAULT 0,
            reject_count INTEGER NOT NULL DEFAULT 0,
            watchlist_count INTEGER NOT NULL DEFAULT 0,
            needs_manual_review_count INTEGER NOT NULL DEFAULT 0,
            breakdown TEXT,
            error_message TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_review_run_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            asin TEXT NOT NULL,
            status TEXT NOT NULL,
            decision TEXT,
            notes TEXT,
            analysis TEXT,
            summary TEXT,
            web_research_summary TEXT,
            source_urls_json TEXT NOT NULL DEFAULT '[]',
            error_message TEXT,
            reviewed_at_utc TEXT NOT NULL
        )
        """
    )
    ensure_table_columns(
        conn,
        "agent_review_run_products",
        {
            "analysis": "TEXT",
            "summary": "TEXT",
            "web_research_summary": "TEXT",
            "source_urls_json": "TEXT NOT NULL DEFAULT '[]'",
        },
    )
    drop_agent_review_confidence_column(conn)
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_review_runs_started
        ON agent_review_runs (started_at_utc)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_review_run_products_run
        ON agent_review_run_products (run_id)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_review_run_products_asin
        ON agent_review_run_products (asin)
        """
    )


def ensure_pipeline_runs_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lock_name TEXT NOT NULL,
            started_at_utc TEXT NOT NULL,
            finished_at_utc TEXT,
            status TEXT NOT NULL,
            heartbeat_at_utc TEXT,
            error_message TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pipeline_runs_active
        ON pipeline_runs (lock_name)
        WHERE status = 'in_progress'
        """
    )


def ensure_embedding_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS keyword_embeddings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword_id INTEGER,
            keyword TEXT NOT NULL,
            model_name TEXT NOT NULL,
            embedding_json TEXT NOT NULL,
            dimension INTEGER NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(keyword, model_name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS keyword_similarity_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_keyword_id INTEGER,
            source_keyword TEXT NOT NULL,
            candidate_keyword TEXT NOT NULL,
            model_name TEXT NOT NULL,
            similarity_score REAL NOT NULL,
            generation_run_id INTEGER,
            created_at_utc TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_keyword_embeddings_keyword
        ON keyword_embeddings (keyword, model_name)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_keyword_similarity_edges_run
        ON keyword_similarity_edges (generation_run_id)
        """
    )


def ensure_table_columns(
    conn: sqlite3.Connection,
    table_name: str,
    columns: dict[str, str],
) -> None:
    existing_columns = table_columns(conn, table_name)

    for column_name, column_type in columns.items():
        if column_name in existing_columns:
            continue
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")


def backfill_keyword_defaults(conn: sqlite3.Connection) -> None:
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE scrape_keywords
        SET created_at_utc = COALESCE(created_at_utc, first_seen_at_utc, ?),
            updated_at_utc = COALESCE(updated_at_utc, first_seen_at_utc, ?),
            lifecycle_state = COALESCE(NULLIF(lifecycle_state, ''), ?),
            origin = COALESCE(origin, ?),
            cluster_key = COALESCE(cluster_key, ?),
            next_eligible_at_utc = COALESCE(next_eligible_at_utc, first_seen_at_utc, ?),
            total_candidates_found = COALESCE(total_candidates_found, total_products_found, 0),
            last_run_status = COALESCE(last_run_status, status)
        """,
        (
            now,
            now,
            SCRAPE_KEYWORD_DEFAULT_LIFECYCLE,
            SCRAPE_KEYWORD_DEFAULT_ORIGIN,
            SCRAPE_KEYWORD_DEFAULT_CLUSTER,
            now,
        ),
    )


def drop_agent_review_confidence_column(conn: sqlite3.Connection) -> None:
    existing_columns = table_columns(conn, "agent_review_run_products")
    if "confidence" not in existing_columns:
        return

    temp_table = "agent_review_run_products_without_confidence"
    desired_columns = list(AGENT_REVIEW_RUN_PRODUCTS_COLUMNS)
    column_definitions = ", ".join(
        f"{name} {column_type}"
        for name, column_type in AGENT_REVIEW_RUN_PRODUCTS_COLUMNS.items()
    )
    column_names = ", ".join(desired_columns)

    conn.execute(f"DROP TABLE IF EXISTS {temp_table}")
    conn.execute(f"CREATE TABLE {temp_table} ({column_definitions})")
    conn.execute(
        f"""
        INSERT INTO {temp_table} ({column_names})
        SELECT {column_names}
        FROM agent_review_run_products
        """
    )
    conn.execute("DROP TABLE agent_review_run_products")
    conn.execute(f"ALTER TABLE {temp_table} RENAME TO agent_review_run_products")


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
