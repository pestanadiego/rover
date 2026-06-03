import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from rover.common import isoformat_utc, parse_utc, utc_now
from rover.db import ensure_database_schema, ensure_table_columns as ensure_db_table_columns
from rover.data_paths import default_db_path


DEFAULT_DB_PATH = default_db_path()

WINNER_DECISIONS = {"keep", "watchlist"}
REJECTION_DECISIONS = {"reject"}
COUNTABLE_RUN_STATUSES = {"completed", "no_results"}

def refresh_keyword_stats(
    db_path: Path | str = DEFAULT_DB_PATH,
    **_kwargs: Any,
) -> dict[str, Any]:
    """Recompute keyword stats from scrape runs, matches, products, and reviews."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = utc_now()

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_keyword_stats_schema(conn)

        keywords = fetch_keywords(conn)
        if not keywords:
            return {
                "db_path": str(path),
                "keywords_refreshed": 0,
                "retired_keywords": 0,
                "cooldown_keywords": 0,
            }

        product_stats = fetch_keyword_product_stats(conn)
        run_counts = fetch_run_counts(conn)
        summary = {
            "db_path": str(path),
            "keywords_refreshed": 0,
            "retired_keywords": 0,
            "cooldown_keywords": 0,
        }

        for keyword in keywords:
            keyword_id = int(keyword["id"])
            stats = product_stats.get(keyword_id, empty_product_stats())
            run_count = run_counts.get(keyword_id, 0)
            zero_streak, last_zero_run_at = compute_zero_result_streak(conn, keyword_id)
            lifecycle_state, cooldown_until, next_eligible = choose_schedule_state(
                current_state=keyword["lifecycle_state"],
                current_next_eligible=keyword["next_eligible_at_utc"],
                run_count=run_count,
                zero_result_streak=zero_streak,
                last_zero_run_at=last_zero_run_at,
                now=now,
            )

            total_candidates = int(stats["total_candidates_found"])
            total_winners = int(stats["total_winners"])
            winner_rate = total_winners / total_candidates if total_candidates else 0.0

            conn.execute(
                """
                UPDATE scrape_keywords
                SET run_count = ?,
                    total_candidates_found = ?,
                    total_winners = ?,
                    total_rejections = ?,
                    historical_winner_rate = ?,
                    avg_profit = ?,
                    avg_roi_percent = ?,
                    zero_result_streak = ?,
                    lifecycle_state = ?,
                    cooldown_until_utc = ?,
                    next_eligible_at_utc = ?,
                    updated_at_utc = ?
                WHERE id = ?
                """,
                (
                    run_count,
                    total_candidates,
                    total_winners,
                    int(stats["total_rejections"]),
                    round(winner_rate, 4),
                    stats["avg_profit"],
                    stats["avg_roi_percent"],
                    zero_streak,
                    lifecycle_state,
                    cooldown_until,
                    next_eligible,
                    isoformat_utc(now),
                    keyword_id,
                ),
            )

            summary["keywords_refreshed"] += 1
            if lifecycle_state == "retired":
                summary["retired_keywords"] += 1
            if lifecycle_state == "cooldown":
                summary["cooldown_keywords"] += 1

        conn.commit()
        return summary


def ensure_keyword_stats_schema(conn: sqlite3.Connection) -> None:
    """Create keyword tables and planned stats columns when they are absent."""
    ensure_database_schema(conn)


def add_missing_columns(
    conn: sqlite3.Connection,
    table_name: str,
    required_columns: dict[str, str],
) -> None:
    ensure_db_table_columns(conn, table_name, required_columns)


def backfill_keyword_extension_fields(conn: sqlite3.Connection) -> None:
    ensure_database_schema(conn)


def fetch_keywords(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT id, keyword, lifecycle_state, next_eligible_at_utc
        FROM scrape_keywords
        ORDER BY id
        """
    ).fetchall()


def fetch_keyword_product_stats(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    if not table_exists(conn, "products"):
        rows = fetch_keyword_review_counts_without_products(conn)
    else:
        rows = fetch_keyword_review_counts_with_products(conn)

    return {int(row["keyword_id"]): dict(row) for row in rows}


def fetch_keyword_review_counts_with_products(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    profit_expr = "p.profit" if column_exists(conn, "products", "profit") else "NULL"
    roi_expr = "p.roi_percent" if column_exists(conn, "products", "roi_percent") else "NULL"
    order_sql = product_order_sql(conn)

    return conn.execute(
        f"""
        WITH keyword_asins AS (
            SELECT DISTINCT keyword_id, asin
            FROM product_keyword_matches
        ),
        latest_products AS (
            SELECT asin, profit, roi_percent
            FROM (
                SELECT
                    p.asin,
                    {profit_expr} AS profit,
                    {roi_expr} AS roi_percent,
                    ROW_NUMBER() OVER (
                        PARTITION BY p.asin
                        ORDER BY {order_sql}
                    ) AS row_number
                FROM products p
            )
            WHERE row_number = 1
        )
        SELECT
            sk.id AS keyword_id,
            COUNT(ka.asin) AS total_candidates_found,
            SUM(CASE WHEN r.decision IN ('keep', 'watchlist') THEN 1 ELSE 0 END)
                AS total_winners,
            SUM(CASE WHEN r.decision = 'reject' THEN 1 ELSE 0 END)
                AS total_rejections,
            ROUND(AVG(lp.profit), 4) AS avg_profit,
            ROUND(AVG(lp.roi_percent), 4) AS avg_roi_percent
        FROM scrape_keywords sk
        LEFT JOIN keyword_asins ka ON ka.keyword_id = sk.id
        LEFT JOIN latest_products lp ON lp.asin = ka.asin
        LEFT JOIN product_reviews r ON r.asin = ka.asin
        GROUP BY sk.id
        """
    ).fetchall()


def fetch_keyword_review_counts_without_products(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    return conn.execute(
        """
        WITH keyword_asins AS (
            SELECT DISTINCT keyword_id, asin
            FROM product_keyword_matches
        )
        SELECT
            sk.id AS keyword_id,
            COUNT(ka.asin) AS total_candidates_found,
            SUM(CASE WHEN r.decision IN ('keep', 'watchlist') THEN 1 ELSE 0 END)
                AS total_winners,
            SUM(CASE WHEN r.decision = 'reject' THEN 1 ELSE 0 END)
                AS total_rejections,
            NULL AS avg_profit,
            NULL AS avg_roi_percent
        FROM scrape_keywords sk
        LEFT JOIN keyword_asins ka ON ka.keyword_id = sk.id
        LEFT JOIN product_reviews r ON r.asin = ka.asin
        GROUP BY sk.id
        """
    ).fetchall()


def product_order_sql(conn: sqlite3.Connection) -> str:
    if column_exists(conn, "products", "imported_at_utc"):
        return "p.imported_at_utc DESC, p.id DESC"
    return "p.id DESC"


def fetch_run_counts(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute(
        """
        SELECT keyword_id, COUNT(*) AS run_count
        FROM keyword_scrape_runs
        WHERE status != 'in_progress'
        GROUP BY keyword_id
        """
    ).fetchall()
    return {int(row["keyword_id"]): int(row["run_count"]) for row in rows}


def compute_zero_result_streak(
    conn: sqlite3.Connection,
    keyword_id: int,
) -> tuple[int, datetime | None]:
    rows = conn.execute(
        """
        SELECT
            status,
            COALESCE(products_exported, 0) AS products_exported,
            COALESCE(finished_at_utc, started_at_utc) AS run_at_utc
        FROM keyword_scrape_runs
        WHERE keyword_id = ?
          AND status != 'in_progress'
        ORDER BY run_at_utc DESC, id DESC
        """,
        (keyword_id,),
    ).fetchall()

    streak = 0
    last_zero_run_at = None

    for row in rows:
        if row["status"] not in COUNTABLE_RUN_STATUSES:
            continue

        run_at = parse_utc(row["run_at_utc"], default=utc_now())
        if int(row["products_exported"] or 0) > 0:
            break

        streak += 1
        if last_zero_run_at is None:
            last_zero_run_at = run_at

    return streak, last_zero_run_at


def choose_schedule_state(
    current_state: str | None,
    current_next_eligible: str | None,
    run_count: int,
    zero_result_streak: int,
    last_zero_run_at: datetime | None,
    now: datetime,
) -> tuple[str, str | None, str | None]:
    if run_count == 0:
        lifecycle_state = current_state or "new"
        next_eligible = current_next_eligible or isoformat_utc(now)
        return lifecycle_state, None, next_eligible

    cooldown_until = compute_cooldown_until(zero_result_streak, last_zero_run_at)

    if zero_result_streak >= 5:
        return "retired", cooldown_until, None

    if cooldown_until and parse_utc(cooldown_until, default=now) > now:
        return "cooldown", cooldown_until, cooldown_until

    return "active", cooldown_until, isoformat_utc(now)


def compute_cooldown_until(
    zero_result_streak: int,
    last_zero_run_at: datetime | None,
) -> str | None:
    cooldown_days = cooldown_days_for_zero_streak(zero_result_streak)
    if cooldown_days == 0:
        return None

    anchor = last_zero_run_at or utc_now()
    return isoformat_utc(anchor + timedelta(days=cooldown_days))


def cooldown_days_for_zero_streak(zero_result_streak: int) -> int:
    if zero_result_streak >= 6:
        return 30
    if zero_result_streak >= 3:
        return 7
    if zero_result_streak >= 1:
        return 2
    return 0


def empty_product_stats() -> dict[str, Any]:
    return {
        "total_candidates_found": 0,
        "total_winners": 0,
        "total_rejections": 0,
        "avg_profit": None,
        "avg_roi_percent": None,
    }


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


def main() -> int:
    summary = refresh_keyword_stats(DEFAULT_DB_PATH)
    print(
        "Refreshed {keywords_refreshed} keywords "
        "({retired_keywords} retired, {cooldown_keywords} cooldown) "
        "in {db_path}".format(**summary)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
