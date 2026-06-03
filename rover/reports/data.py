import sqlite3
import time
from pathlib import Path
from typing import Any

from rover.agent.review_store import latest_agent_breakdown
from rover.common import elapsed_seconds, int_value, utc_now_iso
from rover.db import ensure_database_schema, ensure_table_columns as ensure_db_table_columns
from rover.data_paths import default_db_path
from rover.pipeline_logging import log_event


DEFAULT_DB_PATH = default_db_path()

DECISION_KEYS = ("keep", "watchlist", "reject", "needs_manual_review", "pending")


def build_latest_email_report(
    db_path: Path = DEFAULT_DB_PATH,
    max_products: int = 25,
    scheduler_run_id: int | None = None,
    scrape_summary: dict[str, Any] | None = None,
    normalization_completed: bool = True,
) -> dict[str, Any]:
    started_at = time.monotonic()
    db_path = Path(db_path)
    generated_at_utc = utc_now_iso()
    log_event(
        "email_report_data_started",
        stage="Send email report",
        db_path=db_path,
        max_products=max_products,
        scheduler_run_id=scheduler_run_id,
        normalization_completed=normalization_completed,
    )

    if not db_path.exists():
        log_event(
            "email_report_data_empty",
            stage="Send email report",
            level="WARNING",
            db_path=db_path,
            reason="database_missing",
        )
        return empty_report(generated_at_utc, scrape_summary, "Database file does not exist.")

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_report_tables(conn)

        scheduler_run = fetch_scheduler_run(conn, scheduler_run_id)
        normalization_run = fetch_latest_normalization_run(conn) if normalization_completed else None
        report_start_utc = choose_report_start(normalization_run, scheduler_run)

        products = []
        product_count = 0
        decision_counts = empty_decision_counts()
        top_keywords = []

        if normalization_run:
            products = fetch_products_for_run(conn, normalization_run, max_products)
            product_count = count_products_for_run(conn, normalization_run)
            decision_counts = count_decisions_for_run(conn, normalization_run)
            top_keywords = fetch_top_keywords_for_run(conn, normalization_run)

        generation_summary = fetch_generation_summary(conn, report_start_utc)
        cooldown_keywords = fetch_cooldown_keywords(conn)
        agent_review_run = latest_agent_breakdown(conn, report_start_utc)

    log_event(
        "email_report_data_completed",
        stage="Send email report",
        elapsed_seconds=elapsed_seconds(started_at),
        db_path=db_path,
        normalization_run_id=(normalization_run or {}).get("id"),
        scheduler_run_id=(scheduler_run or {}).get("id"),
        report_start_utc=report_start_utc,
        product_count=product_count,
        displayed_product_count=len(products),
        decision_counts=decision_counts,
        top_keyword_count=len(top_keywords),
        generation_summary_count=len(generation_summary),
        cooldown_keyword_count=len(cooldown_keywords),
        agent_review_run_id=(agent_review_run or {}).get("id"),
    )
    
    return {
        "generated_at_utc": generated_at_utc,
        "db_path": str(db_path),
        "normalization_completed": normalization_completed,
        "normalization_run": normalization_run,
        "scheduler_run": scheduler_run,
        "scrape_summary": clean_scrape_summary(scrape_summary),
        "report_start_utc": report_start_utc,
        "products": products,
        "product_count": product_count,
        "displayed_product_count": len(products),
        "decision_counts": decision_counts,
        "top_keywords": top_keywords,
        "generation_summary": generation_summary,
        "cooldown_keywords": cooldown_keywords,
        "agent_review_run": agent_review_run,
        "agent_breakdown": agent_review_run.get("breakdown") if agent_review_run else None,
        "warning": None,
    }


def empty_report(
    generated_at_utc: str,
    scrape_summary: dict[str, Any] | None,
    warning: str,
) -> dict[str, Any]:
    return {
        "generated_at_utc": generated_at_utc,
        "db_path": None,
        "normalization_completed": False,
        "normalization_run": None,
        "scheduler_run": None,
        "scrape_summary": clean_scrape_summary(scrape_summary),
        "report_start_utc": None,
        "products": [],
        "product_count": 0,
        "displayed_product_count": 0,
        "decision_counts": empty_decision_counts(),
        "top_keywords": [],
        "generation_summary": [],
        "cooldown_keywords": [],
        "agent_review_run": None,
        "agent_breakdown": None,
        "warning": warning,
    }


def ensure_report_tables(conn: sqlite3.Connection) -> None:
    ensure_database_schema(conn)
    conn.commit()


def fetch_latest_normalization_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    if not table_exists(conn, "normalization_runs"):
        return None

    row = conn.execute(
        """
        SELECT *
        FROM normalization_runs
        ORDER BY imported_at_utc DESC, id DESC
        LIMIT 1
        """
    ).fetchone()

    return dict(row) if row else None


def fetch_scheduler_run(
    conn: sqlite3.Connection,
    scheduler_run_id: int | None,
) -> dict[str, Any] | None:
    if not table_exists(conn, "scheduler_runs"):
        return None

    if scheduler_run_id:
        row = conn.execute(
            """
            SELECT *
            FROM scheduler_runs
            WHERE id = ?
            LIMIT 1
            """,
            (scheduler_run_id,),
        ).fetchone()
        return dict(row) if row else None

    row = conn.execute(
        """
        SELECT *
        FROM scheduler_runs
        ORDER BY started_at_utc DESC, id DESC
        LIMIT 1
        """
    ).fetchone()

    return dict(row) if row else None


def choose_report_start(
    normalization_run: dict[str, Any] | None,
    scheduler_run: dict[str, Any] | None,
) -> str | None:
    if normalization_run:
        return normalization_run.get("imported_at_utc")

    if scheduler_run:
        return scheduler_run.get("started_at_utc")

    return None


def fetch_products_for_run(
    conn: sqlite3.Connection,
    normalization_run: dict[str, Any],
    max_products: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        {product_select_sql()}
        {product_run_where_sql()}
        {product_order_sql()}
        LIMIT ?
        """,
        (
            normalization_run["imported_at_utc"],
            normalization_run["imported_at_utc"],
            safe_limit(max_products),
        ),
    ).fetchall()

    return [dict(row) for row in rows]


def count_products_for_run(
    conn: sqlite3.Connection,
    normalization_run: dict[str, Any],
) -> int:
    row = conn.execute(
        f"""
        SELECT COUNT(*)
        FROM products p
        LEFT JOIN product_reviews r ON r.asin = p.asin
        {product_run_where_sql()}
        """,
        (
            normalization_run["imported_at_utc"],
            normalization_run["imported_at_utc"],
        ),
    ).fetchone()

    return int(row[0]) if row else 0


def count_decisions_for_run(
    conn: sqlite3.Connection,
    normalization_run: dict[str, Any],
) -> dict[str, int]:
    counts = empty_decision_counts()
    rows = conn.execute(
        f"""
        SELECT
            COALESCE(NULLIF(r.decision, ''), 'pending') AS decision,
            COUNT(*) AS count
        FROM products p
        LEFT JOIN product_reviews r ON r.asin = p.asin
        {product_run_where_sql()}
        GROUP BY COALESCE(NULLIF(r.decision, ''), 'pending')
        """,
        (
            normalization_run["imported_at_utc"],
            normalization_run["imported_at_utc"],
        ),
    ).fetchall()

    for row in rows:
        decision = clean_decision(row["decision"])
        counts[decision] = int(row["count"])

    return counts


def fetch_top_keywords_for_run(
    conn: sqlite3.Connection,
    normalization_run: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = conn.execute(
        f"""
        SELECT
            COALESCE(NULLIF(p.scrape_keyword, ''), 'Unknown') AS keyword,
            COUNT(*) AS product_count
        FROM products p
        LEFT JOIN product_reviews r ON r.asin = p.asin
        {product_run_where_sql()}
        GROUP BY COALESCE(NULLIF(p.scrape_keyword, ''), 'Unknown')
        ORDER BY product_count DESC, keyword ASC
        LIMIT 5
        """,
        (
            normalization_run["imported_at_utc"],
            normalization_run["imported_at_utc"],
        ),
    ).fetchall()

    return [dict(row) for row in rows]


def fetch_generation_summary(
    conn: sqlite3.Connection,
    report_start_utc: str | None,
) -> list[dict[str, Any]]:
    if not report_start_utc:
        return []

    if not table_exists(conn, "keyword_generation_runs"):
        return []

    rows = conn.execute(
        """
        SELECT
            engine,
            COUNT(*) AS run_count,
            COALESCE(SUM(generated_count), 0) AS generated_count,
            COALESCE(SUM(accepted_count), 0) AS accepted_count,
            COALESCE(SUM(rejected_count), 0) AS rejected_count
        FROM keyword_generation_runs
        WHERE started_at_utc >= ?
        GROUP BY engine
        ORDER BY accepted_count DESC, generated_count DESC, engine ASC
        """,
        (report_start_utc,),
    ).fetchall()

    return [dict(row) for row in rows]


def fetch_cooldown_keywords(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not table_exists(conn, "scrape_keywords"):
        return []

    rows = conn.execute(
        """
        SELECT
            keyword,
            cooldown_until_utc,
            next_eligible_at_utc,
            zero_result_streak,
            priority_score
        FROM scrape_keywords
        WHERE lifecycle_state = 'cooldown'
        ORDER BY
            priority_score DESC,
            COALESCE(next_eligible_at_utc, cooldown_until_utc, updated_at_utc) ASC,
            keyword ASC
        LIMIT 5
        """
    ).fetchall()

    return [dict(row) for row in rows]


def product_select_sql() -> str:
    return """
        SELECT
            p.*,
            COALESCE(r.status, 'pending') AS agent_status,
            r.decision AS agent_decision,
            r.notes AS agent_notes,
            r.analysis AS agent_analysis,
            r.summary AS agent_summary,
            r.decided_at_utc AS agent_decided_at_utc,
            r.updated_at_utc AS agent_review_updated_at_utc
        FROM products p
        LEFT JOIN product_reviews r ON r.asin = p.asin
    """


def product_run_where_sql() -> str:
    return """
        WHERE p.imported_at_utc = ?
           OR (r.updated_at_utc IS NOT NULL AND r.updated_at_utc >= ?)
    """


def product_order_sql() -> str:
    return """
        ORDER BY
            CASE COALESCE(NULLIF(r.decision, ''), 'pending')
                WHEN 'keep' THEN 1
                WHEN 'watchlist' THEN 2
                WHEN 'needs_manual_review' THEN 3
                WHEN 'pending' THEN 4
                WHEN 'reject' THEN 5
                ELSE 6
            END,
            p.roi_percent DESC,
            p.profit DESC,
            p.sales_rank_current ASC,
            p.id DESC
    """


def clean_scrape_summary(scrape_summary: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(scrape_summary, dict):
        return {}

    return {
        "keywords_attempted": int_value(scrape_summary.get("keywords_attempted")),
        "products_exported": int_value(scrape_summary.get("products_exported")),
        "sheet_rows_updated": int_value(scrape_summary.get("sheet_rows_updated")),
        "errors": list(scrape_summary.get("errors") or []),
    }


def empty_decision_counts() -> dict[str, int]:
    return {decision: 0 for decision in DECISION_KEYS}


def clean_decision(value: Any) -> str:
    text = str(value or "pending").strip().lower()
    if text in DECISION_KEYS:
        return text

    return "pending"


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        LIMIT 1
        """,
        (table_name,),
    ).fetchone()

    return row is not None


def add_missing_columns(
    conn: sqlite3.Connection,
    table_name: str,
    required_columns: dict[str, str],
) -> None:
    ensure_db_table_columns(conn, table_name, required_columns)


def safe_limit(value: int, maximum: int = 100) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 25

    if parsed < 1:
        return 1

    if parsed > maximum:
        return maximum

    return parsed
