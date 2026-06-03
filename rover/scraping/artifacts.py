import json
import re
import traceback
from pathlib import Path
from typing import Any

from rover.common import utc_now, utc_now_iso
from rover.scraping.config import ScraperConfig


def save_scraper_error(
    driver: Any,
    config: ScraperConfig,
    keyword: str,
    error: BaseException | str,
    page_number: int | None = None,
    product_index: int | None = None,
) -> Path | None:
    if not config.artifacts.enabled:
        return None

    artifact_dir = config.artifacts.directory / artifact_directory_name(keyword)
    artifact_dir.mkdir(parents=True, exist_ok=True)

    metadata = build_metadata(
        driver=driver,
        config=config,
        keyword=keyword,
        error=error,
        page_number=page_number,
        product_index=product_index,
    )
    write_json(artifact_dir / "metadata.json", metadata)
    write_page_html(driver, artifact_dir / "page.html")
    write_screenshot(driver, artifact_dir / "screenshot.png")

    return artifact_dir


def build_metadata(
    driver: Any,
    config: ScraperConfig,
    keyword: str,
    error: BaseException | str,
    page_number: int | None,
    product_index: int | None,
) -> dict[str, Any]:
    if isinstance(error, BaseException):
        error_type = type(error).__name__
        error_message = str(error)
        error_traceback = traceback.format_exception_only(type(error), error)
    else:
        error_type = "ScraperError"
        error_message = str(error)
        error_traceback = []

    return {
        "created_at_utc": utc_now_iso(),
        "keyword": keyword,
        "page_number": page_number,
        "product_index": product_index,
        "browser_mode": config.browser.mode,
        "current_url": safe_current_url(driver),
        "title": safe_title(driver),
        "error_type": error_type,
        "error_message": error_message,
        "error_traceback": error_traceback,
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def write_page_html(driver: Any, path: Path) -> None:
    try:
        path.write_text(driver.page_source or "", encoding="utf-8")
    except Exception as error:
        path.write_text(f"Could not save page HTML: {error}", encoding="utf-8")


def write_screenshot(driver: Any, path: Path) -> None:
    try:
        driver.save_screenshot(str(path))
    except Exception:
        return


def safe_current_url(driver: Any) -> str | None:
    try:
        return driver.current_url
    except Exception:
        return None


def safe_title(driver: Any) -> str | None:
    try:
        return driver.title
    except Exception:
        return None


def artifact_directory_name(keyword: str) -> str:
    timestamp = utc_now().strftime("%Y-%m-%dT%H-%M-%SZ")
    safe_keyword = re.sub(r"[^a-zA-Z0-9]+", "-", keyword).strip("-").lower()
    if not safe_keyword:
        return timestamp

    return f"{timestamp}_{safe_keyword[:40]}"
