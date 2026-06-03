import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rover.common import (
    clean_text,
    ensure_utc,
    float_value as safe_float,
    int_value as safe_int,
    isoformat_utc as format_utc,
    parse_utc,
    utc_now,
)
from rover.data_paths import default_db_path
from rover.keywords.policy import BUCKET_ORDER, KeywordPolicy, load_keyword_policy
from rover.keywords.store import (
    DEFAULT_CLUSTER_KEY,
    LIFECYCLE_ACTIVE,
    ORIGIN_MANUAL_SEED,
    create_keyword_tables,
    ensure_keyword,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = default_db_path()
BOOTSTRAP_SEEN_AT_UTC = "1970-01-01T00:00:00Z"
BOOTSTRAP_SEED_KEYWORDS = (
    "water bottle",
    "sensory toys",
    "baby toys",
    "toy cars",
    "zoo animal toys",
    "baby doll accessories",
    "soccer ball kids",
    "beach toys",
    "bath toys",
)


@dataclass
class KeywordCandidate:
    keyword_id: int
    keyword: str
    status: str
    created_at_utc: str
    origin: str
    cluster: str
    parent_keyword: str | None
    last_scraped_at_utc: str | None
    cooldown_until_utc: str | None
    next_eligible_at_utc: str | None
    historical_winner_rate: float
    avg_profit: float
    avg_profit_score: float
    days_since_last_run: float
    zero_result_streak: int
    priority_score: float
    buckets: tuple[str, ...]

    def to_dict(self, scheduler_run_id: int, bucket: str, rank_in_bucket: int) -> dict[str, Any]:
        return {
            "scheduler_run_id": scheduler_run_id,
            "keyword_id": self.keyword_id,
            "keyword": self.keyword,
            "selection_bucket": bucket,
            "bucket": bucket,
            "origin": self.origin,
            "cluster": self.cluster,
            "parent_keyword": self.parent_keyword,
            "priority_score": round(self.priority_score, 4),
            "historical_winner_rate": round(self.historical_winner_rate, 4),
            "avg_profit": round(self.avg_profit, 4),
            "avg_profit_score": round(self.avg_profit_score, 4),
            "days_since_last_run": round(self.days_since_last_run, 2),
            "zero_result_streak": self.zero_result_streak,
            "status": self.status,
            "created_at_utc": self.created_at_utc,
            "last_scraped_at_utc": self.last_scraped_at_utc,
            "rank": rank_in_bucket,
            "rank_in_bucket": rank_in_bucket,
        }


def select_keywords(
    db_path: str | Path = DEFAULT_DB_PATH,
    policy_path: str | Path | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    policy = load_keyword_policy(policy_path)
    run_started = ensure_utc(now or utc_now())
    run_started_at_utc = format_utc(run_started)

    with connect(db_path) as conn:
        ensure_scheduler_tables(conn)
        bootstrap_seed_keywords(conn, policy)
        scheduler_run_id = create_scheduler_run(conn, policy, run_started_at_utc)

        candidates = load_candidates(conn, policy, run_started)
        selected = pick_candidates(candidates, policy, scheduler_run_id)
        insert_scheduled_keywords(conn, selected, run_started_at_utc)
        finish_scheduler_run(conn, scheduler_run_id, len(selected), run_started_at_utc)
        conn.commit()

    return [row for row, _candidate in selected]


def select_keywords_for_run(
    keyword_store: Any | None = None,
    project_root: Path | None = None,
    global_winner_limit: int | None = None,
) -> dict[str, Any]:
    db_path = keyword_store.db_path if keyword_store else DEFAULT_DB_PATH
    selected = select_keywords(db_path=db_path)

    scheduler_run_id = None
    if selected:
        scheduler_run_id = selected[0].get("scheduler_run_id")
    else:
        scheduler_run_id = latest_scheduler_run_id(db_path)

    return {
        "scheduler_run_id": scheduler_run_id,
        "keywords": selected,
    }


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def latest_scheduler_run_id(db_path: str | Path) -> int | None:
    with connect(db_path) as conn:
        if not table_exists(conn, "scheduler_runs"):
            return None

        row = conn.execute(
            """
            SELECT id
            FROM scheduler_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    return int(row["id"]) if row else None


def ensure_scheduler_tables(conn: sqlite3.Connection) -> None:
    create_keyword_tables(conn)


def create_scheduler_run(
    conn: sqlite3.Connection,
    policy: KeywordPolicy,
    run_started_at_utc: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO scheduler_runs (
            scheduled_for_utc,
            started_at_utc,
            status,
            target_keyword_count,
            notes
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            run_started_at_utc,
            run_started_at_utc,
            "in_progress",
            policy.target_keyword_count,
            json.dumps(
                {
                    "policy_path": str(policy.path),
                    "policy": policy.as_dict(),
                },
                sort_keys=True,
            ),
        ),
    )
    return int(cursor.lastrowid)


def finish_scheduler_run(
    conn: sqlite3.Connection,
    scheduler_run_id: int,
    selected_count: int,
    finished_at_utc: str,
) -> None:
    conn.execute(
        """
        UPDATE scheduler_runs
        SET finished_at_utc = ?,
            status = ?,
            selected_keyword_count = ?
        WHERE id = ?
        """,
        (finished_at_utc, "completed", selected_count, scheduler_run_id),
    )


def load_candidates(
    conn: sqlite3.Connection,
    policy: KeywordPolicy,
    run_started: datetime,
) -> list[KeywordCandidate]:
    if not table_exists(conn, "scrape_keywords"):
        return []

    keyword_rows = conn.execute("SELECT * FROM scrape_keywords").fetchall()
    if not keyword_rows:
        return []

    scrape_columns = table_columns(conn, "scrape_keywords")
    run_stats = load_run_stats(conn)
    avg_profit_by_keyword = load_avg_profit_by_keyword(conn)
    candidates = []

    for row in keyword_rows:
        candidate = build_candidate(
            row=row,
            columns=scrape_columns,
            policy=policy,
            run_started=run_started,
            stats=run_stats.get(int(row["id"])),
            avg_profit=avg_profit_by_keyword.get(clean_keyword(row["keyword"]), 0.0),
        )
        if candidate is None:
            continue
        candidates.append(candidate)

    return candidates


def build_candidate(
    row: sqlite3.Row,
    columns: set[str],
    policy: KeywordPolicy,
    run_started: datetime,
    stats: dict[str, Any] | None,
    avg_profit: float,
) -> KeywordCandidate | None:
    keyword = clean_keyword(row["keyword"])
    if not keyword:
        return None

    if contains_banned_term(keyword, policy.banned_terms):
        return None

    created_at_utc = first_text(
        column_value(row, columns, "created_at_utc"),
        column_value(row, columns, "created_at"),
        column_value(row, columns, "first_seen_at_utc"),
    )
    created_at = parse_utc(created_at_utc)
    if created_at and created_at >= run_started:
        return None

    status = clean_text(column_value(row, columns, "status"), default="pending").lower()
    lifecycle_state = clean_text(
        column_value(row, columns, "lifecycle_state"),
        default="new",
    ).lower()
    if lifecycle_state == "retired":
        return None

    last_scraped_at_utc = first_text(
        column_value(row, columns, "last_scraped_at_utc"),
        column_value(row, columns, "last_run_at_utc"),
        stats.get("last_run_at_utc") if stats else None,
    )
    cooldown_until_utc = first_text(column_value(row, columns, "cooldown_until_utc"))
    next_eligible_at_utc = first_text(column_value(row, columns, "next_eligible_at_utc"))

    historical_winner_rate = historical_rate(row, columns, stats)
    zero_result_streak = zero_streak(row, columns, stats, status)
    if zero_result_streak >= policy.retire_after_zero_winner_runs:
        return None

    if not cooldown_has_elapsed(cooldown_until_utc, next_eligible_at_utc, run_started):
        return None

    if not policy_cooldown_has_elapsed(
        status=status,
        zero_result_streak=zero_result_streak,
        last_scraped_at_utc=last_scraped_at_utc,
        policy=policy,
        run_started=run_started,
    ):
        return None

    origin = candidate_origin(row, columns)
    cluster = candidate_cluster(row, columns, policy.default_cluster)
    parent_keyword = first_text(
        column_value(row, columns, "parent_keyword"),
        column_value(row, columns, "parent"),
        column_value(row, columns, "seed_keyword"),
    )
    explicit_avg_profit = first_text(
        column_value(row, columns, "avg_profit"),
        column_value(row, columns, "average_profit"),
    )
    if explicit_avg_profit is not None:
        avg_profit = safe_float(explicit_avg_profit, avg_profit)

    avg_profit_score = min(avg_profit / 10.0, 3.0) if avg_profit else 0.0
    days_since_last_run = days_since(last_scraped_at_utc, run_started, policy.cooldowns["stale_days"])
    priority_score = (
        (5.0 * historical_winner_rate)
        + (2.0 * avg_profit_score)
        + days_since_last_run
        - (3.0 * zero_result_streak)
    )
    buckets = candidate_buckets(
        row=row,
        columns=columns,
        status=status,
        origin=origin,
        parent_keyword=parent_keyword,
        historical_winner_rate=historical_winner_rate,
        total_products_found=safe_int(column_value(row, columns, "total_products_found"), 0),
        zero_result_streak=zero_result_streak,
    )
    if not buckets:
        return None

    return KeywordCandidate(
        keyword_id=int(row["id"]),
        keyword=keyword,
        status=status,
        created_at_utc=created_at_utc or "1970-01-01T00:00:00Z",
        origin=origin,
        cluster=cluster,
        parent_keyword=parent_keyword,
        last_scraped_at_utc=last_scraped_at_utc,
        cooldown_until_utc=cooldown_until_utc,
        next_eligible_at_utc=next_eligible_at_utc,
        historical_winner_rate=historical_winner_rate,
        avg_profit=avg_profit,
        avg_profit_score=avg_profit_score,
        days_since_last_run=days_since_last_run,
        zero_result_streak=zero_result_streak,
        priority_score=priority_score,
        buckets=buckets,
    )


def pick_candidates(
    candidates: list[KeywordCandidate],
    policy: KeywordPolicy,
    scheduler_run_id: int,
) -> list[tuple[dict[str, Any], KeywordCandidate]]:
    if not candidates:
        return []

    targets = bucket_targets(policy)
    by_bucket = candidates_by_bucket(candidates)
    selected: list[tuple[dict[str, Any], KeywordCandidate]] = []
    selected_keywords: set[str] = set()
    diversity = DiversityTracker(policy)

    for bucket in BUCKET_ORDER:
        selected.extend(
            take_from_bucket(
                bucket=bucket,
                limit=targets[bucket],
                candidates=by_bucket[bucket],
                scheduler_run_id=scheduler_run_id,
                selected_keywords=selected_keywords,
                diversity=diversity,
            )
        )

    remaining_slots = policy.target_keyword_count - len(selected)
    if remaining_slots <= 0:
        return selected

    backfill = sorted(candidates, key=sort_key)
    selected.extend(
        take_from_bucket(
            bucket="backfill",
            limit=remaining_slots,
            candidates=backfill,
            scheduler_run_id=scheduler_run_id,
            selected_keywords=selected_keywords,
            diversity=diversity,
        )
    )
    return selected


def bootstrap_seed_keywords(conn: sqlite3.Connection, policy: KeywordPolicy) -> None:
    if scrape_keyword_count(conn) > 0:
        return

    cluster_key = policy.default_cluster or DEFAULT_CLUSTER_KEY
    for keyword in BOOTSTRAP_SEED_KEYWORDS:
        ensure_keyword(
            conn=conn,
            keyword=keyword,
            seen_at_utc=BOOTSTRAP_SEEN_AT_UTC,
            origin=ORIGIN_MANUAL_SEED,
            lifecycle_state=LIFECYCLE_ACTIVE,
            cluster_key=cluster_key,
            next_eligible_at_utc=BOOTSTRAP_SEEN_AT_UTC,
        )


def scrape_keyword_count(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "scrape_keywords"):
        return 0

    row = conn.execute("SELECT COUNT(*) AS keyword_count FROM scrape_keywords").fetchone()
    if not row:
        return 0

    return int(row["keyword_count"])


def take_from_bucket(
    bucket: str,
    limit: int,
    candidates: list[KeywordCandidate],
    scheduler_run_id: int,
    selected_keywords: set[str],
    diversity: "DiversityTracker",
    allow_manual: bool = True,
) -> list[tuple[dict[str, Any], KeywordCandidate]]:
    if limit <= 0:
        return []

    taken: list[tuple[dict[str, Any], KeywordCandidate]] = []
    rank = 1
    for candidate in candidates:
        if len(taken) >= limit:
            break

        if candidate.keyword in selected_keywords:
            continue

        if "manual" in candidate.buckets and bucket != "manual" and not allow_manual:
            continue

        if bucket not in candidate.buckets and bucket != "backfill":
            continue

        bypass_diversity = bucket == "manual"
        if not bypass_diversity and not diversity.can_add(candidate):
            continue

        selected_keywords.add(candidate.keyword)
        if not bypass_diversity:
            diversity.add(candidate)

        row = candidate.to_dict(
            scheduler_run_id=scheduler_run_id,
            bucket=bucket,
            rank_in_bucket=rank,
        )
        taken.append((row, candidate))
        rank += 1

    return taken


class DiversityTracker:
    def __init__(self, policy: KeywordPolicy):
        target = max(policy.target_keyword_count, 1)
        diversity = policy.diversity
        self.max_per_origin = max(1, math.floor(target * float(diversity["max_share_per_origin"])))
        self.max_per_cluster = max(1, math.floor(target * float(diversity["max_share_per_cluster"])))
        self.max_per_parent = int(diversity["max_per_parent_keyword"])
        self.origin_counts: Counter[str] = Counter()
        self.cluster_counts: Counter[str] = Counter()
        self.parent_counts: Counter[str] = Counter()

    def can_add(self, candidate: KeywordCandidate) -> bool:
        if self.origin_counts[candidate.origin] >= self.max_per_origin:
            return False

        if self.should_cap_cluster(candidate.cluster):
            if self.cluster_counts[candidate.cluster] >= self.max_per_cluster:
                return False

        if candidate.parent_keyword:
            if self.parent_counts[candidate.parent_keyword] >= self.max_per_parent:
                return False

        return True

    def should_cap_cluster(self, cluster: str) -> bool:
        if cluster == DEFAULT_CLUSTER_KEY:
            return False

        return True

    def add(self, candidate: KeywordCandidate) -> None:
        self.origin_counts[candidate.origin] += 1
        if self.should_cap_cluster(candidate.cluster):
            self.cluster_counts[candidate.cluster] += 1
        if candidate.parent_keyword:
            self.parent_counts[candidate.parent_keyword] += 1


def bucket_targets(policy: KeywordPolicy) -> dict[str, int]:
    target = policy.target_keyword_count
    exact = {bucket: target * policy.selection_mix[bucket] for bucket in BUCKET_ORDER}
    base = {bucket: math.floor(value) for bucket, value in exact.items()}
    remaining = target - sum(base.values())

    by_remainder = sorted(
        BUCKET_ORDER,
        key=lambda bucket: (exact[bucket] - base[bucket], policy.selection_mix[bucket]),
        reverse=True,
    )
    for bucket in by_remainder[:remaining]:
        base[bucket] += 1

    return base


def candidates_by_bucket(
    candidates: list[KeywordCandidate],
) -> dict[str, list[KeywordCandidate]]:
    by_bucket: dict[str, list[KeywordCandidate]] = defaultdict(list)
    for candidate in candidates:
        for bucket in candidate.buckets:
            by_bucket[bucket].append(candidate)

    for bucket in BUCKET_ORDER:
        by_bucket[bucket].sort(key=sort_key)

    return by_bucket


def sort_key(candidate: KeywordCandidate) -> tuple[float, float, str]:
    return (-candidate.priority_score, -candidate.historical_winner_rate, candidate.keyword)


def insert_scheduled_keywords(
    conn: sqlite3.Connection,
    selected: list[tuple[dict[str, Any], KeywordCandidate]],
    selected_at_utc: str,
) -> None:
    for row, candidate in selected:
        values = {
            "scheduler_run_id": row["scheduler_run_id"],
            "keyword_id": candidate.keyword_id,
            "keyword": candidate.keyword,
            "selection_bucket": row["selection_bucket"],
            "bucket": row["selection_bucket"],
            "origin": candidate.origin,
            "cluster": candidate.cluster,
            "parent_keyword": candidate.parent_keyword,
            "priority_score": candidate.priority_score,
            "rank": row["rank"],
            "rank_in_bucket": row["rank"],
            "reason_json": json.dumps(row, sort_keys=True),
            "status": "selected",
            "created_at_utc": selected_at_utc,
            "selected_at_utc": selected_at_utc,
        }
        cursor = insert_dynamic(conn, "scheduled_keywords", values)
        row["scheduled_keyword_id"] = int(cursor.lastrowid)


def insert_dynamic(
    conn: sqlite3.Connection,
    table_name: str,
    values: dict[str, Any],
) -> sqlite3.Cursor:
    columns = [column for column in values if column in table_columns(conn, table_name)]
    placeholders = ", ".join("?" for _column in columns)
    column_sql = ", ".join(columns)
    return conn.execute(
        f"INSERT INTO {table_name} ({column_sql}) VALUES ({placeholders})",
        [values[column] for column in columns],
    )


def load_run_stats(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    if not table_exists(conn, "keyword_scrape_runs"):
        return {}

    rows = conn.execute(
        """
        SELECT keyword_id, started_at_utc, products_exported, status
        FROM keyword_scrape_runs
        WHERE status != 'in_progress'
        ORDER BY keyword_id, started_at_utc DESC, id DESC
        """
    ).fetchall()
    stats: dict[int, dict[str, Any]] = {}
    for row in rows:
        keyword_id = int(row["keyword_id"])
        current = stats.setdefault(
            keyword_id,
            {
                "total_runs": 0,
                "winner_runs": 0,
                "zero_result_streak": 0,
                "counting_zero_streak": True,
                "last_run_at_utc": row["started_at_utc"],
            },
        )
        products_exported = safe_int(row["products_exported"], 0)
        current["total_runs"] += 1
        if products_exported > 0:
            current["winner_runs"] += 1
            current["counting_zero_streak"] = False
            continue

        if current["counting_zero_streak"]:
            current["zero_result_streak"] += 1

    for current in stats.values():
        total_runs = current["total_runs"]
        current["historical_winner_rate"] = (
            current["winner_runs"] / total_runs if total_runs else 0.0
        )
        current.pop("counting_zero_streak", None)

    return stats


def load_avg_profit_by_keyword(conn: sqlite3.Connection) -> dict[str, float]:
    if not table_exists(conn, "products"):
        return {}

    columns = table_columns(conn, "products")
    keyword_columns = [
        column for column in ("scrape_keyword", "selleramp_search_term", "search_term") if column in columns
    ]
    if not keyword_columns or "profit" not in columns:
        return {}

    profit_by_keyword: dict[str, list[float]] = defaultdict(list)
    for column in keyword_columns:
        rows = conn.execute(
            f"""
            SELECT {column} AS keyword, AVG(profit) AS avg_profit
            FROM products
            WHERE {column} IS NOT NULL AND TRIM({column}) != ''
            GROUP BY {column}
            """
        ).fetchall()
        for row in rows:
            keyword = clean_keyword(row["keyword"])
            if not keyword:
                continue
            profit_by_keyword[keyword].append(float(row["avg_profit"] or 0.0))

    return {
        keyword: sum(values) / len(values)
        for keyword, values in profit_by_keyword.items()
        if values
    }


def candidate_buckets(
    row: sqlite3.Row,
    columns: set[str],
    status: str,
    origin: str,
    parent_keyword: str | None,
    historical_winner_rate: float,
    total_products_found: int,
    zero_result_streak: int,
) -> tuple[str, ...]:
    if is_manual_candidate(row, columns, origin):
        return ("manual",)

    if is_cooldown_retry(status, zero_result_streak):
        return ("cooldown_retry",)

    if origin in {"winner_title_expansion", "search_term_mutation"} or parent_keyword:
        return ("winner_mutation",)

    if origin in {
        "category_expansion",
        "modifier_expansion",
        "llm_expansion",
        "embedding_neighbor",
        "random_exploration",
        "new_generated",
        "generated",
        "ai_generated",
    }:
        return ("new_generated",)

    if origin in {"winner_mutation", "mutation", "winner_mutated"}:
        return ("winner_mutation",)

    if historical_winner_rate > 0 or total_products_found > 0:
        return ("active_proven",)

    return ()


def is_manual_candidate(row: sqlite3.Row, columns: set[str], origin: str) -> bool:
    if origin in {"manual", "manual_seed"}:
        return True

    if clean_text(column_value(row, columns, "source"), default="") == "manual":
        return True

    if safe_int(column_value(row, columns, "is_manual"), 0) == 1:
        return True

    return False


def is_cooldown_retry(status: str, zero_result_streak: int) -> bool:
    if status in {"no_results", "error", "failed"}:
        return True

    return zero_result_streak > 0


def candidate_origin(row: sqlite3.Row, columns: set[str]) -> str:
    origin = first_text(
        column_value(row, columns, "origin"),
        column_value(row, columns, "source"),
        column_value(row, columns, "keyword_origin"),
    )
    if not origin:
        return "legacy"

    return origin.lower().replace(" ", "_")


def candidate_cluster(row: sqlite3.Row, columns: set[str], default_cluster: str) -> str:
    cluster = first_text(
        column_value(row, columns, "cluster_key"),
        column_value(row, columns, "cluster"),
        column_value(row, columns, "keyword_cluster"),
        column_value(row, columns, "cluster_id"),
    )
    if not cluster:
        return default_cluster

    return cluster.lower().replace(" ", "_")


def historical_rate(
    row: sqlite3.Row,
    columns: set[str],
    stats: dict[str, Any] | None,
) -> float:
    explicit = column_value(row, columns, "historical_winner_rate")
    if explicit is not None:
        return safe_float(explicit, 0.0)

    if stats:
        return safe_float(stats.get("historical_winner_rate"), 0.0)

    total_products_found = safe_int(column_value(row, columns, "total_products_found"), 0)
    return 1.0 if total_products_found > 0 else 0.0


def zero_streak(
    row: sqlite3.Row,
    columns: set[str],
    stats: dict[str, Any] | None,
    status: str,
) -> int:
    explicit = column_value(row, columns, "zero_result_streak")
    if explicit is not None:
        return safe_int(explicit, 0)

    if stats:
        return safe_int(stats.get("zero_result_streak"), 0)

    if status == "no_results":
        return 1

    return 0


def cooldown_has_elapsed(
    cooldown_until_utc: str | None,
    next_eligible_at_utc: str | None,
    run_started: datetime,
) -> bool:
    cooldown_until = parse_utc(cooldown_until_utc)
    next_eligible = parse_utc(next_eligible_at_utc)

    if cooldown_until and cooldown_until > run_started:
        return False

    if next_eligible and next_eligible > run_started:
        return False

    return True


def policy_cooldown_has_elapsed(
    status: str,
    zero_result_streak: int,
    last_scraped_at_utc: str | None,
    policy: KeywordPolicy,
    run_started: datetime,
) -> bool:
    if status in {"error", "failed"}:
        return last_run_is_old_enough(
            last_scraped_at_utc,
            policy.cooldowns["error_days"],
            run_started,
        )

    if status == "no_results" or zero_result_streak > 0:
        return last_run_is_old_enough(
            last_scraped_at_utc,
            policy.cooldowns["no_results_days"],
            run_started,
        )

    return True


def last_run_is_old_enough(
    last_scraped_at_utc: str | None,
    cooldown_days: int,
    run_started: datetime,
) -> bool:
    last_scraped_at = parse_utc(last_scraped_at_utc)
    if not last_scraped_at:
        return True

    elapsed_seconds = (run_started - last_scraped_at).total_seconds()
    return elapsed_seconds >= cooldown_days * 86400


def days_since(
    timestamp_utc: str | None,
    run_started: datetime,
    default_days: int,
) -> float:
    timestamp = parse_utc(timestamp_utc)
    if not timestamp:
        return float(default_days)

    delta = run_started - timestamp
    if delta.total_seconds() < 0:
        return 0.0

    return delta.total_seconds() / 86400.0


def contains_banned_term(keyword: str, banned_terms: tuple[str, ...]) -> bool:
    normalized = keyword.lower()
    return any(term in normalized for term in banned_terms)


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def column_value(row: sqlite3.Row, columns: set[str], column_name: str) -> Any:
    if column_name not in columns:
        return None

    return row[column_name]


def first_text(*values: Any) -> str | None:
    for value in values:
        text = clean_text(value, default="")
        if text:
            return text

    return None


def clean_keyword(value: Any) -> str:
    return " ".join(clean_text(value, default="").lower().split())


def main() -> None:
    selected = select_keywords()
    print(json.dumps(selected, indent=2))


if __name__ == "__main__":
    main()
