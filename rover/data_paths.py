from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rover.common import clean_mapping, clean_text, read_yaml_mapping, resolve_project_path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "data.yaml"


@dataclass(frozen=True)
class DataPaths:
    raw_dir: Path
    normalized_dir: Path
    db_path: Path


def load_data_paths(path: Path = DEFAULT_CONFIG_PATH) -> DataPaths:
    data = read_yaml_mapping(path, description="Data path config")
    database = clean_mapping(data.get("database"))

    raw_dir = resolve_project_path(data.get("raw_dir"), default="data/raw")
    normalized_dir = resolve_project_path(data.get("normalized_dir"), default="data/normalized")
    db_path = resolve_db_path(database, normalized_dir)

    return DataPaths(
        raw_dir=raw_dir,
        normalized_dir=normalized_dir,
        db_path=db_path,
    )


def default_raw_dir() -> Path:
    return load_data_paths().raw_dir


def default_normalized_dir() -> Path:
    return load_data_paths().normalized_dir


def default_db_path() -> Path:
    return load_data_paths().db_path


def resolve_db_path(database: dict[str, Any], normalized_dir: Path) -> Path:
    explicit_path = clean_text(database.get("path"), "")
    if explicit_path:
        return resolve_project_path(explicit_path)

    filename = clean_text(database.get("filename"), "products.sqlite")
    return normalized_dir / filename
