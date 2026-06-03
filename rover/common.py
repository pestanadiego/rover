import os
import ssl
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import certifi
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TRUE_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_VALUES = {"0", "false", "no", "n", "off"}
UTC_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
UTC_FILENAME_FORMAT = "%Y%m%dT%H%M%SZ"


def read_yaml_mapping(
    path: Path,
    *,
    description: str = "YAML config",
    missing_ok: bool = True,
) -> dict[str, Any]:
    if not path.exists():
        if missing_ok:
            return {}
        raise FileNotFoundError(f"{description} file not found: {path}")

    with path.open("r", encoding="utf-8") as file:
        loaded = yaml.safe_load(file)

    if loaded is None:
        return {}

    if not isinstance(loaded, dict):
        raise ValueError(f"{description} must be a YAML mapping: {path}")

    return loaded


def clean_mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def clean_text(value: Any, default: str | None = None) -> str | None:
    if value is None:
        return default

    text = str(value).strip()
    return text if text else default


def resolve_project_path(
    value: Any,
    *,
    default: Any = None,
    project_root: Path = PROJECT_ROOT,
) -> Path:
    text = clean_text(value, clean_text(default))
    if text is None:
        raise ValueError("Path value is required.")

    path = Path(text).expanduser()
    if path.is_absolute():
        return path

    return project_root / path


def env_text(name: str, fallback: Any = None, default: str | None = None) -> str | None:
    if name in os.environ:
        return clean_text(os.environ.get(name), "")

    return clean_text(fallback, default)


def env_csv(name: str) -> tuple[str, ...]:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return ()

    values = raw_value.split(",") if isinstance(raw_value, str) else [str(raw_value)]
    return tuple(text for text in (clean_text(value) for value in values) if text)


def env_bool(name: str, default: bool) -> bool:
    return parse_bool(os.getenv(name), default)


def parse_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value

    text = clean_text(value)
    if text is None:
        return default

    lowered = text.lower()
    if lowered in TRUE_VALUES:
        return True
    if lowered in FALSE_VALUES:
        return False
    return default


def int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def positive_int(value: Any, default: int) -> int:
    parsed = int_value(value, default)
    return parsed if parsed > 0 else default


def non_negative_int(value: Any, default: int) -> int:
    parsed = int_value(value, default)
    return parsed if parsed >= 0 else default


def float_value(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def positive_float(value: Any, default: float) -> float:
    parsed = float_value(value, default)
    return parsed if parsed > 0 else default


def non_negative_float(value: Any, default: float) -> float:
    parsed = float_value(value, default)
    return parsed if parsed >= 0 else default


def bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    parsed = float_value(value, default)
    if parsed < minimum:
        return minimum
    if parsed > maximum:
        return maximum
    return parsed


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    return ensure_utc(value).strftime(UTC_ISO_FORMAT)


def utc_now_iso() -> str:
    return isoformat_utc(utc_now())


def utc_timestamp_for_filename() -> str:
    return utc_now().strftime(UTC_FILENAME_FORMAT)


def parse_utc(value: str | None, default: datetime | None = None) -> datetime | None:
    if not value:
        return default

    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return default

    return ensure_utc(parsed)


def elapsed_seconds(started_at: float, digits: int = 3) -> float:
    return round(time.monotonic() - started_at, digits)


def ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


def csv_export_url(sheet_url: str) -> str:
    sheet_id, gid = sheet_id_and_gid(sheet_url)

    if not sheet_id:
        return sheet_url

    query = urlencode({"format": "csv", "gid": gid or "0"})
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?{query}"


def sheet_id_and_gid(sheet_url: str) -> tuple[str | None, str | None]:
    parsed = urlparse(sheet_url)
    path_parts = [part for part in parsed.path.split("/") if part]

    if "spreadsheets" not in path_parts or "d" not in path_parts:
        return None, None

    sheet_id_index = path_parts.index("d") + 1
    if sheet_id_index >= len(path_parts):
        return None, None

    query = parse_qs(parsed.query)
    gid = first_query_value(query, "gid") or gid_from_fragment(parsed.fragment) or "0"
    return path_parts[sheet_id_index], gid


def first_query_value(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    if not values:
        return None
    return values[0]


def gid_from_fragment(fragment: str) -> str | None:
    if not fragment:
        return None

    query = parse_qs(fragment)
    return first_query_value(query, "gid")
