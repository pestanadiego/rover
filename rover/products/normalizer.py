import csv
import hashlib
import json
import math
import os
import re
import sqlite3
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from rover.common import elapsed_seconds, utc_now_iso
from rover.data_paths import default_db_path, default_normalized_dir, default_raw_dir, load_data_paths
from rover.db import ensure_database_schema
from rover.keywords.store import ensure_keyword
from rover.pipeline_logging import configure_pipeline_logging, log_event


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DATA_DIR = default_raw_dir()
NORMALIZED_DATA_DIR = default_normalized_dir()
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "selleramp_columns.yaml"
DEFAULT_DB_PATH = default_db_path()

EMPTY_VALUES = {"", "-", "--", "n/a", "na", "none", "null", "unknown"}
ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

PRODUCT_COLUMNS = [
    "row_hash",
    "asin",
    "name",
    "amazon_url",
    "image_url",
    "category",
    "brand",
    "manufacturer",
    "upc",
    "ean",
    "weight_grams",
    "scrape_keyword",
    "selleramp_search_term",
    "quantity",
    "exported_at_utc",
    "sales_marketplace",
    "sales_currency",
    "home_marketplace",
    "home_currency",
    "cost_price",
    "sale_price",
    "buy_box_current",
    "buy_box_average_180d",
    "breakeven",
    "max_cost",
    "sale_price_for_30_roi",
    "fba_fee",
    "referral_fee",
    "fbm_fulfilment_cost",
    "vat",
    "total_fees",
    "profit",
    "roi_percent",
    "profit_margin_percent",
    "sales_rank_current",
    "estimated_sales",
    "fba_seller_count",
    "fbm_seller_count",
    "total_seller_count",
    "spread_to_max_cost",
    "spread_to_breakeven",
    "buy_box_delta_percent",
    "data_quality",
    "validation_warnings",
    "record_json",
    "raw_file",
    "raw_row_number",
    "imported_at_utc",
]


def main() -> int:
    configure_pipeline_logging(PROJECT_ROOT)
    stage_started_at = time.monotonic()
    log_event("normalization_stage_started", stage="Normalize latest product CSV")

    data_paths = load_data_paths()
    raw_csv = latest_raw_csv(data_paths.raw_dir)

    if not raw_csv:
        print(f"No raw product CSV found in {data_paths.raw_dir}")
        log_event(
            "normalization_no_raw_csv",
            stage="Normalize latest product CSV",
            level="ERROR",
            raw_dir=data_paths.raw_dir,
        )
        return 1

    config = load_config(DEFAULT_CONFIG_PATH)
    imported_at_utc = utc_now_iso()
    log_event(
        "normalization_config_loaded",
        stage="Normalize latest product CSV",
        raw_csv=raw_csv,
        db_path=data_paths.db_path,
        config_path=DEFAULT_CONFIG_PATH,
        imported_at_utc=imported_at_utc,
        column_count=len(config.get("columns", [])),
        combined_column_count=len(config.get("combined_columns", [])),
    )

    data_paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(data_paths.db_path) as conn:
        create_tables(conn)
        stats = normalize_csv(conn, raw_csv, config, imported_at_utc)

    print(
        f"Normalized {stats['saved_rows']} products into {data_paths.db_path} "
        f"({stats['inserted_rows']} inserted, {stats['updated_rows']} updated, "
        f"{stats['unchanged_rows']} unchanged, {stats['rejected_rows']} rejected, "
        f"{stats['total_rows']} total rows)."
    )
    log_event(
        "normalization_stage_completed",
        stage="Normalize latest product CSV",
        elapsed_seconds=elapsed_seconds(stage_started_at),
        **normalization_log_summary(stats),
    )
    return 0


def latest_raw_csv(raw_dir: Path) -> Path | None:
    csv_files = sorted(raw_dir.glob("products_*.csv"))
    if not csv_files:
        return None
    return csv_files[-1]


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        config = yaml.safe_load(file)

    if not isinstance(config, dict):
        raise ValueError(f"{path} must contain a YAML object")

    return config


def normalize_csv(
    conn: sqlite3.Connection,
    csv_path: Path,
    config: dict[str, Any],
    imported_at_utc: str,
) -> dict[str, Any]:
    started_at = time.monotonic()
    run_id = start_run(conn, csv_path, imported_at_utc)
    stats = {
        "total_rows": 0,
        "saved_rows": 0,
        "inserted_rows": 0,
        "updated_rows": 0,
        "unchanged_rows": 0,
        "rejected_rows": 0,
        "warning_count": 0,
        "rejection_reason_counts": Counter(),
        "data_quality_counts": Counter(),
    }
    log_event(
        "normalization_run_started",
        stage="Normalize latest product CSV",
        run_id=run_id,
        csv_path=csv_path,
        imported_at_utc=imported_at_utc,
        progress_every=normalization_progress_interval(),
    )

    with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        log_event(
            "normalization_csv_opened",
            stage="Normalize latest product CSV",
            run_id=run_id,
            csv_path=csv_path,
            headers=reader.fieldnames or [],
            header_count=len(reader.fieldnames or []),
        )

        for row_number, raw_row in enumerate(reader, start=2):
            stats["total_rows"] += 1
            clean_row = clean_raw_row(raw_row)
            record, warnings = normalize_row(clean_row, config)
            warnings.extend(compute_derived_fields(record))
            stats["warning_count"] += len(warnings)

            errors = validate_record(record, clean_row, config)
            data_quality = choose_data_quality(record, errors, warnings, config)
            record["derived"]["data_quality"] = data_quality
            stats["data_quality_counts"][data_quality] += 1

            if errors:
                save_rejected_product(
                    conn,
                    run_id,
                    csv_path,
                    row_number,
                    clean_row,
                    errors,
                    imported_at_utc,
                )
                stats["rejected_rows"] += 1
                for error in errors:
                    stats["rejection_reason_counts"][error] += 1
                log_event(
                    "normalization_row_rejected",
                    stage="Normalize latest product CSV",
                    level="DEBUG",
                    run_id=run_id,
                    row_number=row_number,
                    asin=parse_asin(raw_value(clean_row, "ASIN")),
                    errors=errors,
                )
                log_normalization_progress_at_interval(stats, run_id, started_at)
                continue

            product_id, save_action = save_product(
                conn,
                csv_path,
                row_number,
                record,
                warnings,
                imported_at_utc,
            )
            save_product_keyword_match(
                conn,
                product_id,
                run_id,
                csv_path,
                row_number,
                record,
                imported_at_utc,
            )
            stats["saved_rows"] += 1
            increment_save_action(stats, save_action)
            log_normalization_progress_at_interval(stats, run_id, started_at)

    finish_run(conn, run_id, stats)
    conn.commit()
    log_event(
        "normalization_run_completed",
        stage="Normalize latest product CSV",
        run_id=run_id,
        elapsed_seconds=elapsed_seconds(started_at),
        **normalization_log_summary(stats),
    )
    return stats


def increment_save_action(stats: dict[str, int], save_action: str) -> None:
    if save_action == "inserted":
        stats["inserted_rows"] += 1
        return

    if save_action == "updated":
        stats["updated_rows"] += 1
        return

    if save_action == "unchanged":
        stats["unchanged_rows"] += 1
        return


def log_normalization_progress_at_interval(
    stats: dict[str, Any],
    run_id: int,
    started_at: float,
) -> None:
    interval = normalization_progress_interval()
    if interval <= 0 or stats["total_rows"] % interval != 0:
        return

    log_event(
        "normalization_progress",
        stage="Normalize latest product CSV",
        run_id=run_id,
        elapsed_seconds=elapsed_seconds(started_at),
        **normalization_log_summary(stats),
    )


def normalization_progress_interval() -> int:
    try:
        return int(os.getenv("PIPELINE_LOG_PROGRESS_EVERY", "100"))
    except ValueError:
        return 100


def normalization_log_summary(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "total_rows": stats["total_rows"],
        "saved_rows": stats["saved_rows"],
        "inserted_rows": stats["inserted_rows"],
        "updated_rows": stats["updated_rows"],
        "unchanged_rows": stats["unchanged_rows"],
        "rejected_rows": stats["rejected_rows"],
        "warning_count": stats.get("warning_count", 0),
        "data_quality_counts": dict(stats.get("data_quality_counts", {})),
        "rejection_reason_counts": dict(stats.get("rejection_reason_counts", {})),
    }


def clean_raw_row(raw_row: dict[str, str]) -> dict[str, Any]:
    clean_row = {}
    simplified_row = {}

    for header, value in raw_row.items():
        clean_header = clean_column_name(header)
        clean_row[clean_header] = value
        simplified_row[simplify_column_name(clean_header)] = value

    clean_row["_simplified"] = simplified_row
    return clean_row


def clean_column_name(name: str | None) -> str:
    if not name:
        return ""
    return name.replace("\ufeff", "").strip()


def simplify_column_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def raw_value(clean_row: dict[str, Any], column_name: str | None) -> str | None:
    if not column_name:
        return None

    clean_name = clean_column_name(column_name)
    if clean_name in clean_row:
        return clean_row[clean_name]

    simplified_row = clean_row.get("_simplified", {})
    return simplified_row.get(simplify_column_name(clean_name))


def normalize_row(
    clean_row: dict[str, Any],
    config: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    record = empty_record()
    warnings = []

    for column in config.get("columns", []):
        source_name = column["raw"]
        field = column["field"]
        value_type = column.get("type", "string")
        parsed_value, warning = parse_value(raw_value(clean_row, source_name), value_type)

        if warning:
            warnings.append(f"{source_name}: {warning}")

        set_path(record, field, parsed_value)

    for column in config.get("combined_columns", []):
        parsed_value, warning = parse_combined_value(clean_row, column)

        if warning:
            warnings.append(f"{column['field']}: {warning}")

        set_path(record, column["field"], parsed_value)

    return record, warnings


def empty_record() -> dict[str, Any]:
    return {
        "product": {},
        "source": {},
        "marketplace": {},
        "pricing": {},
        "fees": {},
        "profitability": {},
        "market": {},
        "derived": {},
    }


def parse_combined_value(
    clean_row: dict[str, Any],
    column: dict[str, str],
) -> tuple[Any, str | None]:
    if column.get("type") != "datetime_utc":
        return None, f"unsupported combined type {column.get('type')}"

    direct_value = raw_value(clean_row, column.get("raw"))
    if has_value(direct_value):
        return parse_datetime_utc(direct_value)

    date_value = raw_value(clean_row, column.get("date_raw"))
    time_value = raw_value(clean_row, column.get("time_raw"))

    if not has_value(date_value):
        return None, None

    text = f"{date_value} {time_value}".strip() if has_value(time_value) else date_value
    return parse_datetime_utc(text)


def parse_value(value: Any, value_type: str) -> tuple[Any, str | None]:
    if value_type == "string":
        return parse_string(value), None
    if value_type == "asin":
        return parse_asin(value), None
    if value_type == "integer":
        return parse_integer(value)
    if value_type == "decimal":
        return parse_decimal(value)
    if value_type == "percent":
        return parse_percent(value)

    return parse_string(value), f"unknown type {value_type}; stored as text"


def parse_string(value: Any) -> str | None:
    if not has_value(value):
        return None

    text = str(value).strip()
    return text or None


def parse_asin(value: Any) -> str | None:
    text = parse_string(value)
    if not text:
        return None

    return re.sub(r"[^A-Za-z0-9]", "", text).upper()


def parse_integer(value: Any) -> tuple[int | None, str | None]:
    if not has_value(value):
        return None, None

    text = str(value).strip().lower()
    number, warning = parse_decimal_number(text)
    if warning:
        return None, warning

    if "k" in text:
        number *= 1_000
    if "m" in text:
        number *= 1_000_000

    return int(round(number)), None


def parse_decimal(value: Any) -> tuple[float | None, str | None]:
    if not has_value(value):
        return None, None

    number, warning = parse_decimal_number(str(value))
    if warning:
        return None, warning

    return round(number, 4), None


def parse_percent(value: Any) -> tuple[float | None, str | None]:
    if not has_value(value):
        return None, None

    text = str(value)
    number, warning = parse_decimal_number(text)
    if warning:
        return None, warning

    if "%" not in text and abs(number) <= 1:
        number *= 100

    return round(number, 4), None


def parse_decimal_number(text: str) -> tuple[float, str | None]:
    clean_text = text.strip()
    lower_text = clean_text.lower()

    if lower_text in EMPTY_VALUES:
        return 0.0, "empty value"

    negative_by_parentheses = clean_text.startswith("(") and clean_text.endswith(")")
    match = re.search(r"-?\d+(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?", clean_text)

    if not match:
        return 0.0, f"could not parse {text!r}"

    number = float(match.group(0).replace(",", ""))
    if negative_by_parentheses and number > 0:
        number *= -1

    return number, None


def parse_datetime_utc(value: Any) -> tuple[str | None, str | None]:
    if not has_value(value):
        return None, None

    text = str(value).strip()
    normalized = text.replace(" UTC", "").replace("Z", "+00:00")

    parsed = parse_datetime_with_fromisoformat(normalized)
    if not parsed:
        parsed = parse_datetime_with_known_formats(normalized)

    if not parsed:
        return None, f"could not parse datetime {text!r}"

    if not parsed.tzinfo:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), None


def parse_datetime_with_fromisoformat(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def parse_datetime_with_known_formats(text: str) -> datetime | None:
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
        "%m/%d/%Y %H:%M",
        "%m/%d/%Y",
    ]

    for date_format in formats:
        try:
            return datetime.strptime(text, date_format)
        except ValueError:
            pass

    return None


def has_value(value: Any) -> bool:
    if value is None:
        return False

    text = str(value).strip()
    return text.lower() not in EMPTY_VALUES


def set_path(record: dict[str, Any], field_path: str, value: Any) -> None:
    parent = record
    parts = field_path.split(".")

    for part in parts[:-1]:
        parent = parent.setdefault(part, {})

    parent[parts[-1]] = value


def get_path(record: dict[str, Any], field_path: str) -> Any:
    current: Any = record

    for part in field_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]

    return current


def compute_derived_fields(record: dict[str, Any]) -> list[str]:
    warnings = []

    fba_sellers = get_path(record, "market.fba_seller_count")
    fbm_sellers = get_path(record, "market.fbm_seller_count")
    raw_total_sellers = get_path(record, "market.total_seller_count")

    if fba_sellers is not None and fbm_sellers is not None:
        total_sellers = fba_sellers + fbm_sellers
        record["market"]["total_seller_count"] = total_sellers
        record["derived"]["total_seller_count"] = total_sellers

        if raw_total_sellers is not None and raw_total_sellers != total_sellers:
            warnings.append(
                f"FBA + FBM Seller Count was {raw_total_sellers}; computed {total_sellers}"
            )
    elif raw_total_sellers is not None:
        record["derived"]["total_seller_count"] = raw_total_sellers

    set_derived_difference(
        record,
        "spread_to_max_cost",
        "pricing.max_cost",
        "pricing.cost_price",
    )
    set_derived_difference(
        record,
        "spread_to_breakeven",
        "pricing.sale_price",
        "pricing.breakeven",
    )
    set_buy_box_delta_percent(record)

    return warnings


def set_derived_difference(
    record: dict[str, Any],
    output_name: str,
    left_field: str,
    right_field: str,
) -> None:
    left = get_path(record, left_field)
    right = get_path(record, right_field)

    if left is None or right is None:
        record["derived"][output_name] = None
        return

    record["derived"][output_name] = round(left - right, 2)


def set_buy_box_delta_percent(record: dict[str, Any]) -> None:
    current_buy_box = get_path(record, "pricing.buy_box_current")
    average_buy_box = get_path(record, "pricing.buy_box_average_180d")

    if current_buy_box is None or average_buy_box in (None, 0):
        record["derived"]["buy_box_delta_percent"] = None
        return

    delta = (current_buy_box - average_buy_box) / average_buy_box * 100
    record["derived"]["buy_box_delta_percent"] = round(delta, 2)


def validate_record(
    record: dict[str, Any],
    clean_row: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    validation_config = config.get("validation", {})
    errors = []

    errors.extend(validate_required_fields(record, validation_config))
    errors.extend(validate_asin(record))
    errors.extend(validate_number_ranges(record, validation_config))
    errors.extend(validate_roi(record))
    errors.extend(validate_alerts(clean_row, validation_config))

    return errors


def validate_required_fields(
    record: dict[str, Any],
    validation_config: dict[str, Any],
) -> list[str]:
    errors = []

    for field_path in validation_config.get("required_fields", []):
        if get_path(record, field_path) is None:
            errors.append(f"Missing required field {field_path}")

    return errors


def validate_asin(record: dict[str, Any]) -> list[str]:
    asin = get_path(record, "product.asin")
    if asin is None:
        return []

    if not ASIN_RE.fullmatch(asin):
        return [f"Invalid ASIN {asin!r}"]

    return []


def validate_number_ranges(
    record: dict[str, Any],
    validation_config: dict[str, Any],
) -> list[str]:
    errors = []

    for field_path in validation_config.get("non_negative_fields", []):
        value = get_path(record, field_path)
        if value is not None and value < 0:
            errors.append(f"{field_path} must be 0 or greater")

    for field_path in validation_config.get("positive_fields", []):
        value = get_path(record, field_path)
        if value is not None and value <= 0:
            errors.append(f"{field_path} must be greater than 0")

    return errors


def validate_roi(record: dict[str, Any]) -> list[str]:
    roi = get_path(record, "profitability.roi_percent")

    if roi is None:
        return []

    if not math.isfinite(roi):
        return ["profitability.roi_percent must be a finite number"]

    return []


def validate_alerts(
    clean_row: dict[str, Any],
    validation_config: dict[str, Any],
) -> list[str]:
    alerts = []
    safe_values = set(validation_config.get("safe_alert_values", []))
    blocking_words = validation_config.get("blocking_alert_words", [])

    for column_name in validation_config.get("alert_columns", []):
        value = raw_value(clean_row, column_name)
        if not has_value(value):
            continue

        text = str(value).strip()
        normalized = text.lower()

        if normalized in safe_values:
            continue

        if any(word in normalized for word in blocking_words):
            alerts.append(f"{column_name}: {text}")
            continue

        if normalized in {"1", "true", "yes", "y"}:
            alerts.append(f"{column_name}: {text}")

    if not alerts:
        return []

    return ["Blocking SellerAmp alert found: " + "; ".join(alerts)]


def choose_data_quality(
    record: dict[str, Any],
    errors: list[str],
    warnings: list[str],
    config: dict[str, Any],
) -> str:
    if errors:
        return "invalid"

    if warnings:
        return "partial"

    if missing_configured_fields(record, config):
        return "partial"

    return "complete"


def missing_configured_fields(
    record: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    missing_fields = []

    for column in config.get("columns", []):
        field_path = column["field"]
        if get_path(record, field_path) is None:
            missing_fields.append(field_path)

    for column in config.get("combined_columns", []):
        field_path = column["field"]
        if get_path(record, field_path) is None:
            missing_fields.append(field_path)

    for field_name, value in record["derived"].items():
        if field_name == "data_quality":
            continue
        if value is None:
            missing_fields.append(f"derived.{field_name}")

    return missing_fields


def create_tables(conn: sqlite3.Connection) -> None:
    ensure_database_schema(conn)


def start_run(conn: sqlite3.Connection, csv_path: Path, imported_at_utc: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO normalization_runs (raw_file, imported_at_utc)
        VALUES (?, ?)
        """,
        (str(csv_path), imported_at_utc),
    )
    return int(cursor.lastrowid)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    stats: dict[str, Any],
) -> None:
    conn.execute(
        """
        UPDATE normalization_runs
        SET total_rows = ?,
            saved_rows = ?,
            inserted_rows = ?,
            updated_rows = ?,
            unchanged_rows = ?,
            rejected_rows = ?
        WHERE id = ?
        """,
        (
            stats["total_rows"],
            stats["saved_rows"],
            stats["inserted_rows"],
            stats["updated_rows"],
            stats["unchanged_rows"],
            stats["rejected_rows"],
            run_id,
        ),
    )


def save_product(
    conn: sqlite3.Connection,
    csv_path: Path,
    row_number: int,
    record: dict[str, Any],
    warnings: list[str],
    imported_at_utc: str,
) -> tuple[int, str]:
    record["validation"] = {"warnings": warnings}
    values = product_values(csv_path, row_number, record, warnings, imported_at_utc)

    existing_product = find_product_by_asin(conn, values["asin"])
    if existing_product:
        return update_existing_product(
            conn,
            int(row_value(existing_product, "id", 0)),
            existing_product,
            values,
        )

    return insert_new_product(conn, values)


def find_product_by_asin(
    conn: sqlite3.Connection,
    asin: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT id, row_hash
        FROM products
        WHERE asin = ?
        ORDER BY imported_at_utc DESC, id DESC
        LIMIT 1
        """,
        (asin,),
    ).fetchone()


def update_existing_product(
    conn: sqlite3.Connection,
    product_id: int,
    existing_product: sqlite3.Row,
    values: dict[str, Any],
) -> tuple[int, str]:
    if row_value(existing_product, "row_hash", 1) == values["row_hash"]:
        return product_id, "unchanged"

    update_columns = [
        column
        for column in PRODUCT_COLUMNS
        if column not in {"row_hash", "raw_file", "raw_row_number"}
    ]
    set_clause = ", ".join(f"{column} = ?" for column in update_columns)

    try:
        conn.execute(
            f"""
            UPDATE products
            SET row_hash = ?,
                {set_clause}
            WHERE id = ?
            """,
            [
                values["row_hash"],
                *[values[column] for column in update_columns],
                product_id,
            ],
        )
    except sqlite3.IntegrityError as error:
        return handle_product_update_conflict(conn, values, error)

    return product_id, "updated"


def handle_product_update_conflict(
    conn: sqlite3.Connection,
    values: dict[str, Any],
    error: sqlite3.IntegrityError,
) -> tuple[int, str]:
    row = conn.execute(
        """
        SELECT id
        FROM products
        WHERE row_hash = ?
        LIMIT 1
        """,
        (values["row_hash"],),
    ).fetchone()

    if row:
        return int(row_value(row, "id", 0)), "unchanged"

    raise error


def insert_new_product(
    conn: sqlite3.Connection,
    values: dict[str, Any],
) -> tuple[int, str]:
    placeholders = ", ".join(["?"] * len(PRODUCT_COLUMNS))
    column_names = ", ".join(PRODUCT_COLUMNS)

    try:
        cursor = conn.execute(
            f"""
            INSERT INTO products ({column_names})
            VALUES ({placeholders})
            """,
            [values[column] for column in PRODUCT_COLUMNS],
        )
    except sqlite3.IntegrityError as error:
        return handle_product_insert_conflict(conn, values, error)

    return int(cursor.lastrowid), "inserted"


def handle_product_insert_conflict(
    conn: sqlite3.Connection,
    values: dict[str, Any],
    error: sqlite3.IntegrityError,
) -> tuple[int, str]:
    row = find_product_by_asin(conn, values["asin"])
    if row:
        return update_existing_product(conn, int(row_value(row, "id", 0)), row, values)

    row = conn.execute(
        """
        SELECT id, row_hash
        FROM products
        WHERE row_hash = ?
        LIMIT 1
        """,
        (values["row_hash"],),
    ).fetchone()

    if row:
        return int(row_value(row, "id", 0)), "unchanged"

    raise error


def row_value(row: sqlite3.Row | tuple[Any, ...], column_name: str, index: int) -> Any:
    if isinstance(row, sqlite3.Row):
        return row[column_name]

    return row[index]


def product_values(
    csv_path: Path,
    row_number: int,
    record: dict[str, Any],
    warnings: list[str],
    imported_at_utc: str,
) -> dict[str, Any]:
    record_json = json.dumps(record, sort_keys=True, separators=(",", ":"))

    return {
        "row_hash": hashlib.sha256(record_json.encode("utf-8")).hexdigest(),
        "asin": get_path(record, "product.asin"),
        "name": get_path(record, "product.name"),
        "amazon_url": get_path(record, "product.amazon_url"),
        "image_url": get_path(record, "product.image_url"),
        "category": get_path(record, "product.category"),
        "brand": get_path(record, "product.brand"),
        "manufacturer": get_path(record, "product.manufacturer"),
        "upc": get_path(record, "product.upc"),
        "ean": get_path(record, "product.ean"),
        "weight_grams": get_path(record, "product.weight_grams"),
        "scrape_keyword": get_path(record, "source.scrape_keyword"),
        "selleramp_search_term": get_path(record, "source.selleramp_search_term"),
        "quantity": get_path(record, "source.quantity"),
        "exported_at_utc": get_path(record, "source.exported_at_utc"),
        "sales_marketplace": get_path(record, "marketplace.sales_marketplace"),
        "sales_currency": get_path(record, "marketplace.sales_currency"),
        "home_marketplace": get_path(record, "marketplace.home_marketplace"),
        "home_currency": get_path(record, "marketplace.home_currency"),
        "cost_price": get_path(record, "pricing.cost_price"),
        "sale_price": get_path(record, "pricing.sale_price"),
        "buy_box_current": get_path(record, "pricing.buy_box_current"),
        "buy_box_average_180d": get_path(record, "pricing.buy_box_average_180d"),
        "breakeven": get_path(record, "pricing.breakeven"),
        "max_cost": get_path(record, "pricing.max_cost"),
        "sale_price_for_30_roi": get_path(record, "pricing.sale_price_for_30_roi"),
        "fba_fee": get_path(record, "fees.fba_fee"),
        "referral_fee": get_path(record, "fees.referral_fee"),
        "fbm_fulfilment_cost": get_path(record, "fees.fbm_fulfilment_cost"),
        "vat": get_path(record, "fees.vat"),
        "total_fees": get_path(record, "fees.total_fees"),
        "profit": get_path(record, "profitability.profit"),
        "roi_percent": get_path(record, "profitability.roi_percent"),
        "profit_margin_percent": get_path(record, "profitability.profit_margin_percent"),
        "sales_rank_current": get_path(record, "market.sales_rank_current"),
        "estimated_sales": get_path(record, "market.estimated_sales"),
        "fba_seller_count": get_path(record, "market.fba_seller_count"),
        "fbm_seller_count": get_path(record, "market.fbm_seller_count"),
        "total_seller_count": get_path(record, "derived.total_seller_count"),
        "spread_to_max_cost": get_path(record, "derived.spread_to_max_cost"),
        "spread_to_breakeven": get_path(record, "derived.spread_to_breakeven"),
        "buy_box_delta_percent": get_path(record, "derived.buy_box_delta_percent"),
        "data_quality": get_path(record, "derived.data_quality"),
        "validation_warnings": json.dumps(warnings, sort_keys=True),
        "record_json": record_json,
        "raw_file": str(csv_path),
        "raw_row_number": row_number,
        "imported_at_utc": imported_at_utc,
    }


def save_product_keyword_match(
    conn: sqlite3.Connection,
    product_id: int,
    normalization_run_id: int,
    csv_path: Path,
    row_number: int,
    record: dict[str, Any],
    imported_at_utc: str,
) -> None:
    keyword = get_path(record, "source.scrape_keyword")
    asin = get_path(record, "product.asin")

    if not keyword or not asin:
        return

    keyword_id = ensure_keyword(conn, keyword, imported_at_utc)

    conn.execute(
        """
        INSERT INTO product_keyword_matches (
            asin,
            keyword_id,
            keyword,
            product_id,
            normalization_run_id,
            raw_file,
            raw_row_number,
            exported_at_utc,
            imported_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(asin, keyword_id, raw_file, raw_row_number)
        DO UPDATE SET
            product_id = excluded.product_id,
            normalization_run_id = excluded.normalization_run_id,
            exported_at_utc = excluded.exported_at_utc,
            imported_at_utc = excluded.imported_at_utc
        """,
        (
            asin,
            keyword_id,
            keyword,
            product_id,
            normalization_run_id,
            str(csv_path),
            row_number,
            get_path(record, "source.exported_at_utc"),
            imported_at_utc,
        ),
    )


def save_rejected_product(
    conn: sqlite3.Connection,
    run_id: int,
    csv_path: Path,
    row_number: int,
    clean_row: dict[str, Any],
    errors: list[str],
    imported_at_utc: str,
) -> None:
    raw_row = {key: value for key, value in clean_row.items() if key != "_simplified"}
    asin = parse_asin(raw_value(clean_row, "ASIN"))

    conn.execute(
        """
        INSERT INTO rejected_products (
            run_id,
            raw_file,
            raw_row_number,
            asin,
            errors_json,
            raw_row_json,
            imported_at_utc
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            str(csv_path),
            row_number,
            asin,
            json.dumps(errors, sort_keys=True),
            json.dumps(raw_row, sort_keys=True),
            imported_at_utc,
        ),
    )
if __name__ == "__main__":
    raise SystemExit(main())
