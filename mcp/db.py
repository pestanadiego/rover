import sqlite3
from pathlib import Path
from typing import Any


from rover.common import utc_now_iso
from rover.db import ensure_database_schema, ensure_table_columns as ensure_db_table_columns
from rover.data_paths import default_db_path


DEFAULT_DB_PATH = default_db_path()

ALLOWED_DECISIONS = {"keep", "reject", "needs_manual_review", "watchlist"}
REVIEWED_DECISIONS = {"keep", "reject", "watchlist"}


class ProductDB:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ensure_review_tables()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]

    def ensure_review_tables(self) -> None:
        with self._connect() as conn:
            ensure_database_schema(conn)
            conn.commit()

    def get_product_by_asin(self, asin: str) -> dict[str, Any] | None:
        asin = normalize_asin(asin)
        if not asin:
            return None

        with self._connect() as conn:
            row = conn.execute(
                product_select_sql(
                    """
                    WHERE p.asin = ?
                    ORDER BY p.imported_at_utc DESC, p.id DESC
                    LIMIT 1
                    """
                ),
                (asin,),
            ).fetchone()

        return dict(row) if row else None

    def get_latest_products(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = safe_limit(limit)

        with self._connect() as conn:
            rows = conn.execute(
                product_select_sql(
                    """
                    ORDER BY p.imported_at_utc DESC, p.id DESC
                    LIMIT ?
                    """
                ),
                (limit,),
            ).fetchall()

        return self._rows_to_dicts(rows)

    def get_products_needing_review(self, limit: int = 10) -> list[dict[str, Any]]:
        limit = safe_limit(limit)

        with self._connect() as conn:
            rows = conn.execute(
                product_select_sql(
                    """
                    WHERE r.status IS NULL
                       OR r.status = 'pending'
                       OR r.status = 'summary_pending'
                    ORDER BY p.imported_at_utc DESC, p.id DESC
                    LIMIT ?
                    """
                ),
                (limit,),
            ).fetchall()

        return self._rows_to_dicts(rows)

    def write_decision(
        self,
        asin: str,
        decision: str,
        notes: str = "",
        analysis: str | None = None,
        summary: str | None = None,
        status_override: str | None = None,
    ) -> dict[str, Any]:
        asin = normalize_asin(asin)
        decision = normalize_decision(decision)
        notes = clean_review_text(notes)
        analysis = clean_review_text(analysis) or notes
        summary = clean_review_text(summary) or notes
        notes = summary or notes

        if not asin:
            return {"saved": False, "message": "ASIN is required."}

        if decision not in ALLOWED_DECISIONS:
            allowed = ", ".join(sorted(ALLOWED_DECISIONS))
            return {"saved": False, "message": f"Decision must be one of: {allowed}."}

        product = self.get_product_by_asin(asin)
        if not product:
            return {"saved": False, "message": f"No product found with ASIN {asin}."}

        now = utc_now_iso()
        status = clean_review_text(status_override) or status_for_decision(decision)

        with self._connect() as conn:
            previous = conn.execute(
                """
                SELECT status
                FROM product_reviews
                WHERE asin = ?
                """,
                (asin,),
            ).fetchone()
            previous_status = previous["status"] if previous else None

            conn.execute(
                """
                INSERT INTO product_reviews (
                    asin,
                    product_id,
                    status,
                    decision,
                    notes,
                    analysis,
                    summary,
                    decided_at_utc,
                    updated_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(asin) DO UPDATE SET
                    product_id = excluded.product_id,
                    status = excluded.status,
                    decision = excluded.decision,
                    notes = excluded.notes,
                    analysis = excluded.analysis,
                    summary = excluded.summary,
                    decided_at_utc = excluded.decided_at_utc,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    asin,
                    product["id"],
                    status,
                    decision,
                    notes,
                    analysis,
                    summary,
                    now,
                    now,
                ),
            )

            conn.execute(
                """
                INSERT INTO product_review_events (
                    asin,
                    product_id,
                    previous_status,
                    new_status,
                    decision,
                    notes,
                    analysis,
                    summary,
                    created_at_utc
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asin,
                    product["id"],
                    previous_status,
                    status,
                    decision,
                    notes,
                    analysis,
                    summary,
                    now,
                ),
            )
            conn.commit()

        updated_product = self.get_product_by_asin(asin)
        return {
            "saved": True,
            "message": f"Saved {decision} decision for {asin}.",
            "product": updated_product,
        }

    def get_reviewed_products(
        self,
        decision: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        limit = safe_limit(limit)
        params: list[Any] = []
        where = "WHERE r.decision IS NOT NULL"

        if decision:
            where += " AND r.decision = ?"
            params.append(normalize_decision(decision))

        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(
                product_select_sql(
                    f"""
                    {where}
                    ORDER BY r.updated_at_utc DESC, p.imported_at_utc DESC, p.id DESC
                    LIMIT ?
                    """
                ),
                params,
            ).fetchall()

        return self._rows_to_dicts(rows)

    def search_products(
        self,
        keyword: str | None = None,
        min_roi: float | None = None,
        min_profit: float | None = None,
        max_sales_rank: int | None = None,
        min_estimated_sales: int | None = None,
        max_sellers: int | None = None,
        data_quality: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []

        if keyword:
            clauses.append(
                """
                (
                    p.scrape_keyword = ?
                    OR p.selleramp_search_term = ?
                    OR p.name LIKE ?
                    OR p.brand LIKE ?
                    OR p.category LIKE ?
                )
                """
            )
            keyword_like = f"%{keyword.strip()}%"
            params.extend([keyword.strip(), keyword.strip(), keyword_like, keyword_like, keyword_like])

        add_min_clause(clauses, params, "p.roi_percent", min_roi)
        add_min_clause(clauses, params, "p.profit", min_profit)
        add_max_clause(clauses, params, "p.sales_rank_current", max_sales_rank)
        add_min_clause(clauses, params, "p.estimated_sales", min_estimated_sales)
        add_max_clause(clauses, params, "p.total_seller_count", max_sellers)

        if data_quality:
            clauses.append("p.data_quality = ?")
            params.append(data_quality.strip())

        where = ""
        if clauses:
            where = "WHERE " + " AND ".join(clauses)

        params.append(safe_limit(limit))

        with self._connect() as conn:
            rows = conn.execute(
                product_select_sql(
                    f"""
                    {where}
                    ORDER BY p.imported_at_utc DESC, p.id DESC
                    LIMIT ?
                    """
                ),
                params,
            ).fetchall()

        return self._rows_to_dicts(rows)

    def get_products_by_scrape_keyword(
        self,
        keyword: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        keyword = keyword.strip()
        if not keyword:
            return []

        with self._connect() as conn:
            rows = conn.execute(
                product_select_sql(
                    """
                    WHERE p.scrape_keyword = ?
                    ORDER BY p.imported_at_utc DESC, p.id DESC
                    LIMIT ?
                    """
                ),
                (keyword, safe_limit(limit)),
            ).fetchall()

        return self._rows_to_dicts(rows)

    def get_product_keywords(self, asin: str) -> list[dict[str, Any]]:
        asin = normalize_asin(asin)
        if not asin:
            return []

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    keyword,
                    scrape_run_id,
                    raw_file,
                    raw_row_number,
                    exported_at_utc,
                    imported_at_utc
                FROM product_keyword_matches
                WHERE asin = ?
                ORDER BY imported_at_utc DESC, id DESC
                """,
                (asin,),
            ).fetchall()

        return self._rows_to_dicts(rows)

    def get_keyword_status(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    keyword,
                    status,
                    first_seen_at_utc,
                    last_scraped_at_utc,
                    last_result_count,
                    total_products_found,
                    last_error
                FROM scrape_keywords
                ORDER BY last_scraped_at_utc DESC, first_seen_at_utc DESC
                LIMIT ?
                """,
                (safe_limit(limit, maximum=500),),
            ).fetchall()

        return self._rows_to_dicts(rows)

    def get_pipeline_summary(self) -> dict[str, Any]:
        with self._connect() as conn:
            latest_run = conn.execute(
                """
                SELECT *
                FROM normalization_runs
                ORDER BY imported_at_utc DESC, id DESC
                LIMIT 1
                """
            ).fetchone()

            return {
                "total_products": scalar_count(conn, "products"),
                "pending_reviews": self._pending_review_count(conn),
                "reviewed_products": scalar_count(conn, "product_reviews"),
                "kept_products": review_decision_count(conn, "keep"),
                "rejected_products": review_decision_count(conn, "reject"),
                "watchlist_products": review_decision_count(conn, "watchlist"),
                "manual_review_products": review_decision_count(conn, "needs_manual_review"),
                "partial_data_products": product_quality_count(conn, "partial"),
                "complete_data_products": product_quality_count(conn, "complete"),
                "rejected_import_rows": scalar_count(conn, "rejected_products"),
                "keywords_total": scalar_count(conn, "scrape_keywords"),
                "keywords_no_results": keyword_status_count(conn, "no_results"),
                "keywords_error": keyword_status_count(conn, "error"),
                "latest_normalization_run": dict(latest_run) if latest_run else None,
            }

    def _pending_review_count(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            """
            SELECT COUNT(*)
            FROM products p
            LEFT JOIN product_reviews r ON r.asin = p.asin
            WHERE r.status IS NULL
               OR r.status = 'pending'
               OR r.status = 'summary_pending'
            """
        ).fetchone()
        return int(row[0])


def product_select_sql(where_order_limit: str) -> str:
    return f"""
        SELECT
            p.*,
            r.status AS agent_status,
            r.decision AS agent_decision,
            r.notes AS agent_notes,
            r.analysis AS agent_analysis,
            r.summary AS agent_summary,
            r.decided_at_utc AS agent_decided_at_utc,
            r.updated_at_utc AS agent_review_updated_at_utc
        FROM products p
        LEFT JOIN product_reviews r ON r.asin = p.asin
        {where_order_limit}
    """


def add_missing_columns(
    conn: sqlite3.Connection,
    table_name: str,
    required_columns: dict[str, str],
) -> None:
    ensure_db_table_columns(conn, table_name, required_columns)


def add_min_clause(
    clauses: list[str],
    params: list[Any],
    column_name: str,
    value: float | int | None,
) -> None:
    if value is None:
        return

    clauses.append(f"{column_name} >= ?")
    params.append(value)


def add_max_clause(
    clauses: list[str],
    params: list[Any],
    column_name: str,
    value: float | int | None,
) -> None:
    if value is None:
        return

    clauses.append(f"{column_name} <= ?")
    params.append(value)


def normalize_asin(asin: str) -> str:
    return asin.strip().upper()


def normalize_decision(decision: str) -> str:
    return decision.strip().lower()


def clean_review_text(value: Any) -> str:
    return str(value or "").strip()


def status_for_decision(decision: str) -> str:
    if decision in REVIEWED_DECISIONS:
        return "reviewed"

    return "needs_manual_review"


def safe_limit(limit: int, maximum: int = 100) -> int:
    try:
        parsed_limit = int(limit)
    except (TypeError, ValueError):
        return 10

    if parsed_limit < 1:
        return 1

    if parsed_limit > maximum:
        return maximum

    return parsed_limit


def scalar_count(conn: sqlite3.Connection, table_name: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    return int(row[0])


def review_decision_count(conn: sqlite3.Connection, decision: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM product_reviews
        WHERE decision = ?
        """,
        (decision,),
    ).fetchone()
    return int(row[0])


def product_quality_count(conn: sqlite3.Connection, data_quality: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM products
        WHERE data_quality = ?
        """,
        (data_quality,),
    ).fetchone()
    return int(row[0])


def keyword_status_count(conn: sqlite3.Connection, status: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM scrape_keywords
        WHERE status = ?
        """,
        (status,),
    ).fetchone()
    return int(row[0])
