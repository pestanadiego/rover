import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from rover.common import (
    csv_export_url,
    elapsed_seconds,
    ssl_context,
    utc_timestamp_for_filename,
)
from rover.data_paths import load_data_paths
from rover.pipeline_logging import configure_pipeline_logging, log_event


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOTENV_PATH = PROJECT_ROOT / ".env"

GOOGLE_SHEET_URL_ENV_VAR = "GOOGLE_SHEET_URL"


def main() -> int:
    configure_pipeline_logging(PROJECT_ROOT)
    stage_started_at = time.monotonic()
    log_event("sync_stage_started", stage="Sync products from Google Sheets")

    load_dotenv(DOTENV_PATH)
    sheet_url = os.getenv(GOOGLE_SHEET_URL_ENV_VAR)

    if not sheet_url:
        print(
            f"Missing Google Sheet URL. Set {GOOGLE_SHEET_URL_ENV_VAR} "
            f"in {DOTENV_PATH}."
        )
        log_event(
            "sync_missing_sheet_url",
            stage="Sync products from Google Sheets",
            level="ERROR",
            env_var=GOOGLE_SHEET_URL_ENV_VAR,
            dotenv_path=DOTENV_PATH,
        )
        return 1

    csv_url = csv_export_url(sheet_url)
    data_paths = load_data_paths()
    log_event(
        "sync_config_loaded",
        stage="Sync products from Google Sheets",
        raw_dir=data_paths.raw_dir,
        csv_url=csv_url,
    )

    try:
        output_path = download_csv(csv_url, data_paths.raw_dir)
    except RuntimeError as error:
        print(f"Download failed: {error}")
        log_event(
            "sync_download_failed",
            stage="Sync products from Google Sheets",
            level="ERROR",
            elapsed_seconds=elapsed_seconds(stage_started_at),
            error=str(error),
        )
        return 1

    print(f"Saved {output_path}")
    log_event(
        "sync_stage_completed",
        stage="Sync products from Google Sheets",
        elapsed_seconds=elapsed_seconds(stage_started_at),
        output_path=output_path,
    )
    return 0


def download_csv(csv_url: str, output_dir: Path) -> Path:
    started_at = time.monotonic()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"products_{utc_timestamp_for_filename()}.csv"
    log_event(
        "sync_download_started",
        stage="Sync products from Google Sheets",
        output_dir=output_dir,
        output_path=output_path,
        csv_url=csv_url,
        timeout_seconds=30,
    )

    request = Request(
        csv_url,
        headers={"User-Agent": "winning-product-pipeline/1.0"},
    )

    try:
        with urlopen(request, timeout=30, context=ssl_context()) as response:
            body = response.read()
            content_type = response.headers.get("Content-Type", "")
    except HTTPError as error:
        raise RuntimeError(f"HTTP {error.code} while fetching Google Sheet CSV") from error
    except URLError as error:
        raise RuntimeError(str(error.reason)) from error

    if not body:
        raise RuntimeError("Google Sheets returned an empty response")

    if looks_like_html(body, content_type):
        raise RuntimeError(
            "Google Sheets returned HTML instead of CSV. Make sure the sheet is "
            "published or accessible by link."
        )

    output_path.write_bytes(body)
    log_event(
        "sync_download_completed",
        stage="Sync products from Google Sheets",
        elapsed_seconds=elapsed_seconds(started_at),
        output_path=output_path,
        byte_count=len(body),
        content_type=content_type,
    )
    return output_path


def looks_like_html(body: bytes, content_type: str) -> bool:
    if "text/html" in content_type.lower():
        return True

    preview = body[:200].lstrip().lower()
    return preview.startswith(b"<!doctype html") or preview.startswith(b"<html")


if __name__ == "__main__":
    raise SystemExit(main())
