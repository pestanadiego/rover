import importlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

from rover.data_paths import load_data_paths
from rover.reports.config import load_email_report_config
from rover.common import sheet_id_and_gid
from rover.scraping.config import load_scraper_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOTENV_PATH = PROJECT_ROOT / ".env"
EXPECTED_PYTHON = (3, 13)

CONFIG_FILES = (
    "config/data.yaml",
    "config/scraper.yaml",
    "config/agent.yaml",
    "config/email_report.yaml",
    "config/keyword_policy.yaml",
    "config/selleramp_columns.yaml",
)

REQUIRED_IMPORTS = {
    "certifi": "certifi",
    "google-auth": "google.auth",
    "mcp": "mcp",
    "pydantic-ai-slim": "pydantic_ai",
    "python-dotenv": "dotenv",
    "PyYAML": "yaml",
    "requests": "requests",
    "selenium": "selenium",
}

REQUIRED_DISTRIBUTIONS = (
    "sentence-transformers",
)

REQUIRED_ENV_VARS = (
    "GOOGLE_SHEET_URL",
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "SELLERAMP_EMAIL",
    "SELLERAMP_PASSWORD",
    "SMTP_HOST",
    "EMAIL_FROM",
    "EMAIL_TO",
)

OPENROUTER_ENV_VARS = (
    "OPENROUTER_API_KEY",
    "ROVER_OPENROUTER_API_KEY",
)

SERVICE_ACCOUNT_FIELDS = (
    "client_email",
    "private_key",
    "token_uri",
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


def main() -> int:
    results = run_checks()
    print_results(results)
    return 0 if all(result.ok for result in results) else 1


def run_checks() -> list[CheckResult]:
    if DOTENV_PATH.exists():
        load_dotenv(DOTENV_PATH)

    checks: list[tuple[str, Callable[[], str]]] = [
        ("Python version", check_python_version),
        ("Required imports", check_required_imports),
        (".env file", check_dotenv_file),
        ("Config files", check_config_files),
        ("Required env vars", check_required_env_vars),
        ("OpenRouter key", check_openrouter_key),
        ("Google Sheet URL", check_google_sheet_url),
        ("Google service account", check_google_service_account),
        ("SMTP config", check_smtp_config),
        ("SellerAmp credentials", check_selleramp_credentials),
        ("Writable runtime dirs", check_writable_runtime_dirs),
        ("SQLite read/write", check_sqlite_read_write),
    ]

    results = []
    for name, check in checks:
        results.append(run_check(name, check))

    return results


def run_check(name: str, check: Callable[[], str]) -> CheckResult:
    try:
        detail = check()
    except Exception as error:
        return CheckResult(name=name, ok=False, detail=str(error))

    return CheckResult(name=name, ok=True, detail=detail)


def check_python_version() -> str:
    current = sys.version_info
    if (current.major, current.minor) < EXPECTED_PYTHON:
        raise RuntimeError(
            f"expected Python {EXPECTED_PYTHON[0]}.{EXPECTED_PYTHON[1]}+, "
            f"found {current.major}.{current.minor}.{current.micro}"
        )

    return f"{current.major}.{current.minor}.{current.micro}"


def check_required_imports() -> str:
    missing = []
    for package_name, module_name in REQUIRED_IMPORTS.items():
        try:
            importlib.import_module(module_name)
        except Exception as error:
            missing.append(f"{package_name} ({type(error).__name__})")

    for package_name in REQUIRED_DISTRIBUTIONS:
        try:
            metadata.version(package_name)
        except metadata.PackageNotFoundError:
            missing.append(package_name)

    if missing:
        raise RuntimeError("missing imports: " + ", ".join(missing))

    checked_count = len(REQUIRED_IMPORTS) + len(REQUIRED_DISTRIBUTIONS)
    return f"{checked_count} packages available"


def check_dotenv_file() -> str:
    if not DOTENV_PATH.exists():
        raise RuntimeError(f"missing {relative_path(DOTENV_PATH)}")

    if not DOTENV_PATH.is_file():
        raise RuntimeError(f"{relative_path(DOTENV_PATH)} is not a file")

    return f"loaded {relative_path(DOTENV_PATH)}"


def check_config_files() -> str:
    missing = [path for path in CONFIG_FILES if not (PROJECT_ROOT / path).is_file()]
    if missing:
        raise RuntimeError("missing config files: " + ", ".join(missing))

    return f"{len(CONFIG_FILES)} config files present"


def check_required_env_vars() -> str:
    missing = [name for name in REQUIRED_ENV_VARS if not env_is_set(name)]
    if missing:
        raise RuntimeError("missing env vars: " + ", ".join(missing))

    return f"{len(REQUIRED_ENV_VARS)} required env vars set"


def check_openrouter_key() -> str:
    if not any(env_is_set(name) for name in OPENROUTER_ENV_VARS):
        joined = " or ".join(OPENROUTER_ENV_VARS)
        raise RuntimeError(f"missing {joined}")

    return "configured"


def check_google_sheet_url() -> str:
    sheet_url = clean_env("GOOGLE_SHEET_URL")
    sheet_id, gid = sheet_id_and_gid(sheet_url)
    if not sheet_id:
        raise RuntimeError("GOOGLE_SHEET_URL is not a recognized Google Sheets URL")

    return f"CSV export URL builds for gid {gid or '0'}"


def check_google_service_account() -> str:
    path = resolve_project_path(clean_env("GOOGLE_SERVICE_ACCOUNT_FILE"))
    if not path.is_file():
        raise RuntimeError(f"service account file not found: {safe_path(path)}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"service account file is not valid JSON: {error}") from error

    missing_fields = [field for field in SERVICE_ACCOUNT_FIELDS if not data.get(field)]
    if missing_fields:
        raise RuntimeError("service account JSON missing fields: " + ", ".join(missing_fields))

    return f"file exists and has required fields: {safe_path(path)}"


def check_smtp_config() -> str:
    config = load_email_report_config()
    missing = []
    if not config.smtp_host:
        missing.append("SMTP_HOST")
    if not config.email_from:
        missing.append("EMAIL_FROM")
    if not config.email_to:
        missing.append("EMAIL_TO")
    if config.smtp_username and not config.smtp_password:
        missing.append("SMTP_PASSWORD")

    if missing:
        raise RuntimeError("missing SMTP settings: " + ", ".join(missing))

    return f"configured for {len(config.email_to)} recipient(s)"


def check_selleramp_credentials() -> str:
    missing = [
        name
        for name in ("SELLERAMP_EMAIL", "SELLERAMP_PASSWORD")
        if not env_is_set(name)
    ]
    if missing:
        raise RuntimeError("missing SellerAmp settings: " + ", ".join(missing))

    return "configured"


def check_writable_runtime_dirs() -> str:
    data_paths = load_data_paths()
    scraper_config = load_scraper_config()
    log_dir = configured_log_dir()

    directories = (
        data_paths.raw_dir,
        data_paths.normalized_dir,
        data_paths.db_path.parent,
        scraper_config.artifacts.directory,
        log_dir,
    )

    for directory in unique_paths(directories):
        assert_writable_dir(directory)

    return f"{len(unique_paths(directories))} directories writable"


def check_sqlite_read_write() -> str:
    data_paths = load_data_paths()
    assert_writable_dir(data_paths.db_path.parent)

    temp_file = tempfile.NamedTemporaryFile(
        prefix="doctor_",
        suffix=".sqlite",
        dir=data_paths.db_path.parent,
        delete=False,
    )
    temp_file.close()
    temp_path = Path(temp_file.name)

    try:
        with sqlite3.connect(temp_path) as conn:
            conn.execute("CREATE TABLE doctor_check (value TEXT NOT NULL)")
            conn.execute("INSERT INTO doctor_check (value) VALUES (?)", ("ok",))
            row = conn.execute("SELECT value FROM doctor_check").fetchone()

        if not row or row[0] != "ok":
            raise RuntimeError("SQLite read/write returned unexpected data")
    finally:
        remove_sqlite_temp_files(temp_path)

    return f"read/write OK in {safe_path(data_paths.db_path.parent)}"


def assert_writable_dir(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".doctor_", dir=directory, delete=True) as file:
        file.write(b"ok")
        file.flush()


def remove_sqlite_temp_files(path: Path) -> None:
    for candidate in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def configured_log_dir() -> Path:
    text = os.getenv("PIPELINE_LOG_DIR", "logs").strip() or "logs"
    return resolve_project_path(text)


def resolve_project_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path

    return PROJECT_ROOT / path


def unique_paths(paths: tuple[Path, ...]) -> tuple[Path, ...]:
    seen = set()
    unique = []
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)

    return tuple(unique)


def env_is_set(name: str) -> bool:
    return bool(clean_env(name))


def clean_env(name: str) -> str:
    return os.getenv(name, "").strip()


def print_results(results: list[CheckResult]) -> None:
    print("Winning Product doctor")
    print(f"Project root: {PROJECT_ROOT}")
    print("")

    for result in results:
        status = "OK" if result.ok else "FAIL"
        print(f"{status:<4} {result.name}: {result.detail}")


def relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def safe_path(path: Path) -> str:
    return relative_path(path)


if __name__ == "__main__":
    raise SystemExit(main())
