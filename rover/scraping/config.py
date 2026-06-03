import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from rover.common import (
    clean_mapping,
    clean_text,
    non_negative_int,
    parse_bool,
    positive_float,
    positive_int,
    read_yaml_mapping,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "scraper.yaml"
ALLOWED_BROWSER_MODES = {"debug", "headed", "xvfb"}


@dataclass(frozen=True)
class BrowserConfig:
    mode: str
    debug_address: str
    user_data_dir: str | None
    window_size: str
    element_wait_timeout_seconds: int
    start_url: str | None


@dataclass(frozen=True)
class AuthConfig:
    email: str | None
    password: str | None
    post_login_wait_seconds: int


@dataclass(frozen=True)
class ScrapeConfig:
    target_winners_per_keyword: int
    global_winner_limit: int
    max_pages_to_scrape: int
    start_page: int


@dataclass(frozen=True)
class FilterConfig:
    skip_amazon: bool
    min_sales_per_month: int
    min_sellers: int
    min_cost: float


@dataclass(frozen=True)
class RetryConfig:
    no_results_max_retries: int
    no_results_cooldown_seconds: int


@dataclass(frozen=True)
class TimingConfig:
    search_results_wait_seconds: int
    sidebar_wait_seconds: int
    iframe_inner_wait_seconds: int
    next_page_wait_seconds: int
    keyword_pause_seconds: int
    sheet_row_wait_seconds: int


@dataclass(frozen=True)
class ArtifactConfig:
    enabled: bool
    directory: Path


@dataclass(frozen=True)
class ScraperConfig:
    browser: BrowserConfig
    auth: AuthConfig
    scrape: ScrapeConfig
    filters: FilterConfig
    retries: RetryConfig
    timing: TimingConfig
    artifacts: ArtifactConfig


def load_scraper_config(
    config_path: Path = DEFAULT_CONFIG_PATH,
    project_root: Path = PROJECT_ROOT,
) -> ScraperConfig:
    load_dotenv(project_root / ".env")
    data = read_yaml_mapping(config_path, description="Scraper config")

    return ScraperConfig(
        browser=load_browser_config(data.get("browser"), project_root),
        auth=load_auth_config(data.get("auth")),
        scrape=load_scrape_config(data.get("scrape")),
        filters=load_filter_config(data.get("filters")),
        retries=load_retry_config(data.get("retries")),
        timing=load_timing_config(data.get("timing")),
        artifacts=load_artifact_config(data.get("artifacts"), project_root),
    )


def load_browser_config(raw: Any, project_root: Path) -> BrowserConfig:
    data = clean_mapping(raw)
    mode = clean_text(data.get("mode"), "debug").lower()
    if mode not in ALLOWED_BROWSER_MODES:
        raise ValueError(f"browser.mode must be one of: {', '.join(sorted(ALLOWED_BROWSER_MODES))}")

    return BrowserConfig(
        mode=mode,
        debug_address=clean_text(data.get("debug_address"), "127.0.0.1:9222"),
        user_data_dir=optional_path_text(data.get("user_data_dir"), project_root),
        window_size=clean_text(data.get("window_size"), "1440,1200"),
        element_wait_timeout_seconds=positive_int(data.get("element_wait_timeout_seconds"), 12),
        start_url=clean_text(data.get("start_url")),
    )


def load_scrape_config(raw: Any) -> ScrapeConfig:
    data = clean_mapping(raw)
    return ScrapeConfig(
        target_winners_per_keyword=positive_int(data.get("target_winners_per_keyword"), 5),
        global_winner_limit=positive_int(data.get("global_winner_limit"), 20),
        max_pages_to_scrape=positive_int(data.get("max_pages_to_scrape"), 10),
        start_page=positive_int(data.get("start_page"), 1),
    )


def load_auth_config(raw: Any) -> AuthConfig:
    data = clean_mapping(raw)
    return AuthConfig(
        email=clean_text(os.getenv("SELLERAMP_EMAIL")),
        password=clean_text(os.getenv("SELLERAMP_PASSWORD")),
        post_login_wait_seconds=positive_int(data.get("post_login_wait_seconds"), 8),
    )


def load_filter_config(raw: Any) -> FilterConfig:
    data = clean_mapping(raw)
    return FilterConfig(
        skip_amazon=parse_bool(data.get("skip_amazon"), True),
        min_sales_per_month=positive_int(data.get("min_sales_per_month"), 300),
        min_sellers=positive_int(data.get("min_sellers"), 3),
        min_cost=positive_float(data.get("min_cost"), 3.0),
    )


def load_retry_config(raw: Any) -> RetryConfig:
    data = clean_mapping(raw)
    return RetryConfig(
        no_results_max_retries=non_negative_int(data.get("no_results_max_retries"), 3),
        no_results_cooldown_seconds=non_negative_int(data.get("no_results_cooldown_seconds"), 180),
    )


def load_timing_config(raw: Any) -> TimingConfig:
    data = clean_mapping(raw)
    return TimingConfig(
        search_results_wait_seconds=positive_int(data.get("search_results_wait_seconds"), 3),
        sidebar_wait_seconds=positive_int(data.get("sidebar_wait_seconds"), 4),
        iframe_inner_wait_seconds=positive_int(data.get("iframe_inner_wait_seconds"), 2),
        next_page_wait_seconds=positive_int(data.get("next_page_wait_seconds"), 3),
        keyword_pause_seconds=non_negative_int(data.get("keyword_pause_seconds"), 3),
        sheet_row_wait_seconds=positive_int(data.get("sheet_row_wait_seconds"), 10),
    )


def load_artifact_config(raw: Any, project_root: Path) -> ArtifactConfig:
    data = clean_mapping(raw)
    directory = Path(clean_text(data.get("directory"), "scraping_errors"))
    if not directory.is_absolute():
        directory = project_root / directory

    return ArtifactConfig(
        enabled=parse_bool(data.get("enabled"), True),
        directory=directory,
    )


def optional_path_text(raw_value: str | None, project_root: Path) -> str | None:
    text = clean_text(raw_value)
    if not text:
        return None

    path = Path(text).expanduser()
    if not path.is_absolute():
        path = project_root / path
    return str(path)
