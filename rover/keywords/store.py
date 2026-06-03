import sqlite3
from pathlib import Path
from typing import Any

import rover.db as schema_db
from rover.common import utc_now_iso
from rover.data_paths import default_db_path

DEFAULT_DB_PATH = default_db_path()

ORIGIN_MANUAL_SEED = "manual_seed"
LIFECYCLE_NEW = "new"
LIFECYCLE_TESTING = "testing"
LIFECYCLE_ACTIVE = "active"
LIFECYCLE_COOLDOWN = "cooldown"
LIFECYCLE_RETIRED = "retired"
DEFAULT_CLUSTER_KEY = "dummy"


class KeywordStore:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def should_skip_keyword(self, keyword: str) -> bool:
        with sqlite3.connect(self.db_path) as conn:
            create_keyword_tables(conn)
            row = conn.execute(
                """
                SELECT status, lifecycle_state
                FROM scrape_keywords
                WHERE keyword = ?
                """,
                (keyword,),
            ).fetchone()

        if not row:
            return False

        return row[1] == LIFECYCLE_RETIRED

    def asin_exists(self, asin: str | None) -> bool:
        if not asin:
            return False

        with sqlite3.connect(self.db_path) as conn:
            create_keyword_tables(conn)
            row = conn.execute(
                """
                SELECT 1
                FROM products
                WHERE asin = ?
                LIMIT 1
                """,
                (asin,),
            ).fetchone()

        return row is not None

    def start_run(
        self,
        keyword: str,
        scheduler_run_id: int | None = None,
        scheduled_keyword_id: int | None = None,
        priority_score: float | None = None,
        selection_bucket: str | None = None,
    ) -> int:
        now = utc_now_iso()

        with sqlite3.connect(self.db_path) as conn:
            create_keyword_tables(conn)
            keyword_id = ensure_keyword(conn, keyword, now)

            conn.execute(
                """
                UPDATE scrape_keywords
                SET status = ?,
                    lifecycle_state = ?,
                    last_error = NULL,
                    updated_at_utc = ?
                WHERE id = ?
                """,
                ("in_progress", LIFECYCLE_TESTING, now, keyword_id),
            )

            cursor = conn.execute(
                """
                INSERT INTO keyword_scrape_runs (
                    keyword_id,
                    keyword,
                    scheduler_run_id,
                    scheduled_keyword_id,
                    priority_score,
                    selection_bucket,
                    started_at_utc,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    keyword_id,
                    keyword,
                    scheduler_run_id,
                    scheduled_keyword_id,
                    priority_score,
                    selection_bucket,
                    now,
                    "in_progress",
                ),
            )

            conn.commit()
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        products_exported: int,
        sheet_rows_updated: int,
        products_seen: int = 0,
        duplicate_asins_skipped: int = 0,
        error_message: str | None = None,
    ) -> None:
        now = utc_now_iso()

        with sqlite3.connect(self.db_path) as conn:
            create_keyword_tables(conn)
            row = conn.execute(
                """
                SELECT keyword_id, scheduled_keyword_id
                FROM keyword_scrape_runs
                WHERE id = ?
                """,
                (run_id,),
            ).fetchone()

            if not row:
                return

            keyword_id = int(row[0])
            scheduled_keyword_id = row[1]

            conn.execute(
                """
                UPDATE keyword_scrape_runs
                SET finished_at_utc = ?,
                    status = ?,
                    products_seen = ?,
                    products_exported = ?,
                    duplicate_asins_skipped = ?,
                    sheet_rows_updated = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (
                    now,
                    status,
                    products_seen,
                    products_exported,
                    duplicate_asins_skipped,
                    sheet_rows_updated,
                    error_message,
                    run_id,
                ),
            )

            lifecycle_state = lifecycle_state_after_scrape(status, products_exported)
            update_keyword_after_scrape(
                conn,
                keyword_id,
                status,
                lifecycle_state,
                products_exported,
                error_message,
                now,
            )
            update_scheduled_keyword_after_scrape(
                conn,
                scheduled_keyword_id,
                status,
            )

            conn.commit()


def create_keyword_tables(conn: sqlite3.Connection) -> None:
    schema_db.ensure_database_schema(conn)


def create_scheduler_tables(conn: sqlite3.Connection) -> None:
    schema_db.ensure_scheduler_tables(conn)


def create_generation_tables(conn: sqlite3.Connection) -> None:
    schema_db.ensure_keyword_generation_tables(conn)


def create_keyword_indexes(conn: sqlite3.Connection) -> None:
    schema_db.ensure_keyword_indexes(conn)


def ensure_table_columns(
    conn: sqlite3.Connection,
    table_name: str,
    columns: dict[str, str],
) -> None:
    schema_db.ensure_table_columns(conn, table_name, columns)


def backfill_keyword_defaults(conn: sqlite3.Connection) -> None:
    schema_db.backfill_keyword_defaults(conn)


def ensure_keyword(
    conn: sqlite3.Connection,
    keyword: str,
    seen_at_utc: str,
    origin: str = ORIGIN_MANUAL_SEED,
    lifecycle_state: str = LIFECYCLE_NEW,
    parent_keyword_id: int | None = None,
    source_product_asin: str | None = None,
    source_engine_run_id: int | None = None,
    cluster_key: str = DEFAULT_CLUSTER_KEY,
    next_eligible_at_utc: str | None = None,
) -> int:
    create_keyword_tables(conn)
    conn.execute(
        """
        INSERT INTO scrape_keywords (
            keyword,
            status,
            first_seen_at_utc,
            lifecycle_state,
            origin,
            parent_keyword_id,
            source_product_asin,
            source_engine_run_id,
            cluster_key,
            created_at_utc,
            updated_at_utc,
            next_eligible_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(keyword) DO NOTHING
        """,
        (
            keyword,
            "pending",
            seen_at_utc,
            lifecycle_state,
            origin,
            parent_keyword_id,
            source_product_asin,
            source_engine_run_id,
            cluster_key,
            seen_at_utc,
            seen_at_utc,
            next_eligible_at_utc,
        ),
    )

    return int(conn.execute(
        """
        SELECT id
        FROM scrape_keywords
        WHERE keyword = ?
        """,
        (keyword,),
    ).fetchone()[0])


def update_keyword_after_scrape(
    conn: sqlite3.Connection,
    keyword_id: int,
    status: str,
    lifecycle_state: str,
    products_exported: int,
    error_message: str | None,
    updated_at_utc: str,
) -> None:
    conn.execute(
        """
        UPDATE scrape_keywords
        SET status = ?,
            lifecycle_state = ?,
            last_run_status = ?,
            last_scraped_at_utc = ?,
            last_result_count = ?,
            total_products_found = total_products_found + ?,
            total_candidates_found = total_candidates_found + ?,
            run_count = run_count + 1,
            zero_result_streak = CASE
                WHEN ? = 0 THEN zero_result_streak + 1
                ELSE 0
            END,
            last_error = ?,
            updated_at_utc = ?
        WHERE id = ?
        """,
        (
            status,
            lifecycle_state,
            status,
            updated_at_utc,
            products_exported,
            products_exported,
            products_exported,
            products_exported,
            error_message,
            updated_at_utc,
            keyword_id,
        ),
    )


def update_scheduled_keyword_after_scrape(
    conn: sqlite3.Connection,
    scheduled_keyword_id: int | None,
    status: str,
) -> None:
    if not scheduled_keyword_id:
        return

    conn.execute(
        """
        UPDATE scheduled_keywords
        SET status = ?
        WHERE id = ?
        """,
        (status, scheduled_keyword_id),
    )


def lifecycle_state_after_scrape(status: str, products_exported: int) -> str:
    if status == "error":
        return LIFECYCLE_COOLDOWN

    if products_exported > 0:
        return "awaiting_review"

    return LIFECYCLE_COOLDOWN


def row_to_dict(row: sqlite3.Row | tuple[Any, ...] | None) -> dict[str, Any] | None:
    if row is None:
        return None

    if isinstance(row, sqlite3.Row):
        return dict(row)

    return dict(row)
