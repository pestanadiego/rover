import json
import sqlite3
from pathlib import Path
from typing import Any

import rover.db as schema_db
from rover.common import utc_now_iso
from rover.data_paths import default_db_path

DEFAULT_DB_PATH = default_db_path()


def ensure_agent_review_tables(conn: sqlite3.Connection) -> None:
    schema_db.ensure_database_schema(conn)


def start_agent_review_run(
    db_path: Path,
    agent_name: str,
    model: str,
    scheduler_run_id: int | None,
    normalization_run_id: int | None,
    products_selected: int,
) -> int:
    now = utc_now_iso()

    with sqlite3.connect(db_path) as conn:
        ensure_agent_review_tables(conn)
        cursor = conn.execute(
            """
            INSERT INTO agent_review_runs (
                started_at_utc,
                status,
                agent_name,
                model,
                scheduler_run_id,
                normalization_run_id,
                products_selected
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                now,
                "in_progress",
                agent_name,
                model,
                scheduler_run_id,
                normalization_run_id,
                products_selected,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def record_product_review(
    db_path: Path,
    run_id: int,
    asin: str,
    status: str,
    decision: str | None = None,
    notes: str | None = None,
    analysis: str | None = None,
    summary: str | None = None,
    web_research_summary: str | None = None,
    source_urls: list[str] | None = None,
    error_message: str | None = None,
) -> None:
    with sqlite3.connect(db_path) as conn:
        ensure_agent_review_tables(conn)
        conn.execute(
            """
            INSERT INTO agent_review_run_products (
                run_id,
                asin,
                status,
                decision,
                notes,
                analysis,
                summary,
                web_research_summary,
                source_urls_json,
                error_message,
                reviewed_at_utc
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                asin,
                status,
                decision,
                notes,
                analysis,
                summary,
                web_research_summary,
                json.dumps(source_urls or [], sort_keys=True),
                error_message,
                utc_now_iso(),
            ),
        )
        conn.commit()


def drop_confidence_column(conn: sqlite3.Connection) -> None:
    schema_db.drop_agent_review_confidence_column(conn)


def add_missing_columns(
    conn: sqlite3.Connection,
    table_name: str,
    required_columns: dict[str, str],
) -> None:
    schema_db.ensure_table_columns(conn, table_name, required_columns)


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return schema_db.table_columns(conn, table_name)


def finish_agent_review_run(
    db_path: Path,
    run_id: int,
    status: str,
    breakdown: str | None = None,
    error_message: str | None = None,
) -> None:
    with sqlite3.connect(db_path) as conn:
        ensure_agent_review_tables(conn)
        counts = product_review_counts(conn, run_id)
        conn.execute(
            """
            UPDATE agent_review_runs
            SET finished_at_utc = ?,
                status = ?,
                products_reviewed = ?,
                products_failed = ?,
                keep_count = ?,
                reject_count = ?,
                watchlist_count = ?,
                needs_manual_review_count = ?,
                breakdown = ?,
                error_message = ?
            WHERE id = ?
            """,
            (
                utc_now_iso(),
                status,
                counts["products_reviewed"],
                counts["products_failed"],
                counts["keep"],
                counts["reject"],
                counts["watchlist"],
                counts["needs_manual_review"],
                breakdown,
                error_message,
                run_id,
            ),
        )
        conn.commit()


def latest_agent_breakdown(
    conn: sqlite3.Connection,
    report_start_utc: str | None,
) -> dict[str, Any] | None:
    ensure_agent_review_tables(conn)

    if report_start_utc:
        row = conn.execute(
            """
            SELECT *
            FROM agent_review_runs
            WHERE finished_at_utc >= ?
              AND breakdown IS NOT NULL
              AND TRIM(breakdown) != ''
            ORDER BY finished_at_utc DESC, id DESC
            LIMIT 1
            """,
            (report_start_utc,),
        ).fetchone()
        if row:
            return dict(row)

    row = conn.execute(
        """
        SELECT *
        FROM agent_review_runs
        WHERE breakdown IS NOT NULL
          AND TRIM(breakdown) != ''
        ORDER BY finished_at_utc DESC, id DESC
        LIMIT 1
        """
    ).fetchone()

    return dict(row) if row else None


def product_review_counts(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    counts = {
        "products_reviewed": 0,
        "products_failed": 0,
        "keep": 0,
        "reject": 0,
        "watchlist": 0,
        "needs_manual_review": 0,
    }

    rows = conn.execute(
        """
        SELECT status, decision, COUNT(*) AS count
        FROM agent_review_run_products
        WHERE run_id = ?
        GROUP BY status, decision
        """,
        (run_id,),
    ).fetchall()

    for row in rows:
        status = row[0]
        decision = row[1]
        count = int(row[2])

        if status == "failed":
            counts["products_failed"] += count
        else:
            counts["products_reviewed"] += count

        if decision in {"keep", "reject", "watchlist", "needs_manual_review"}:
            counts[decision] += count

    return counts
