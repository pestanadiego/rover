from dataclasses import dataclass
from pathlib import Path

from rover.common import (
    clean_mapping,
    clean_text,
    parse_bool,
    positive_int,
    read_yaml_mapping,
    resolve_project_path,
)
from rover.data_paths import default_db_path
from rover.openrouter import load_rover_openrouter_config


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "agent.yaml"
DEFAULT_DB_PATH = default_db_path()


@dataclass(frozen=True)
class RoverAgentConfig:
    enabled: bool
    required: bool
    agent_name: str
    model: str
    shared_system_prompt: str
    api_key: str
    app_url: str | None
    app_title: str | None
    db_path: Path
    max_products_per_run: int
    continue_on_product_error: bool
    write_manual_review_on_error: bool
    web_search_enabled: bool
    web_search_context_size: str
    web_search_timeout_seconds: int
    web_fetch_timeout_seconds: int
    web_search_max_searches: int
    web_fetch_max_fetches: int
    parallel_tool_calls: bool
    mcp_enabled: bool
    mcp_timeout_seconds: int
    max_note_chars: int
    provider_retry_count: int
    provider_retry_backoff_seconds: int
    fallback_model: str
    summary_model: str
    product_review_timeout_seconds: int
    summary_timeout_seconds: int
    breakdown_timeout_seconds: int


def load_rover_agent_config(path: Path = DEFAULT_CONFIG_PATH) -> RoverAgentConfig:
    data = read_yaml_mapping(path, description="Rover agent config")
    web_search = clean_mapping(data.get("web_search"))
    tools = clean_mapping(data.get("tools"))
    mcp = clean_mapping(data.get("mcp"))
    review = clean_mapping(data.get("review"))
    provider = clean_mapping(data.get("provider"))

    model = clean_text(data.get("model"), "deepseek/deepseek-v4-pro")
    openrouter_config = load_rover_openrouter_config(PROJECT_ROOT, model)

    return RoverAgentConfig(
        enabled=parse_bool(data.get("enabled"), default=True),
        required=parse_bool(data.get("required"), default=True),
        agent_name=clean_text(data.get("agent_name"), "Rover"),
        model=model,
        shared_system_prompt=clean_text(data.get("shared_system_prompt"), ""),
        api_key=openrouter_config.api_key,
        app_url=openrouter_config.app_url,
        app_title=openrouter_config.app_title,
        db_path=resolve_project_path(data.get("db_path"), default=DEFAULT_DB_PATH),
        max_products_per_run=positive_int(data.get("max_products_per_run"), default=25),
        continue_on_product_error=parse_bool(data.get("continue_on_product_error"), default=True),
        write_manual_review_on_error=parse_bool(data.get("write_manual_review_on_error"), default=True),
        web_search_enabled=parse_bool(web_search.get("enabled"), default=True),
        web_search_context_size=clean_text(web_search.get("search_context_size"), "medium"),
        web_search_timeout_seconds=positive_int(web_search.get("search_timeout_seconds"), default=45),
        web_fetch_timeout_seconds=positive_int(web_search.get("fetch_timeout_seconds"), default=45),
        web_search_max_searches=positive_int(web_search.get("max_searches_per_product"), default=3),
        web_fetch_max_fetches=positive_int(web_search.get("max_fetches_per_product"), default=3),
        parallel_tool_calls=parse_bool(tools.get("parallel_tool_calls"), default=True),
        mcp_enabled=parse_bool(mcp.get("enabled"), default=True),
        mcp_timeout_seconds=positive_int(mcp.get("timeout_seconds"), default=30),
        max_note_chars=positive_int(review.get("max_note_chars"), default=1000),
        provider_retry_count=positive_int(provider.get("retry_count"), default=2),
        provider_retry_backoff_seconds=positive_int(provider.get("retry_backoff_seconds"), default=5),
        fallback_model=clean_text(provider.get("fallback_model"), "deepseek/deepseek-v4-pro"),
        summary_model=clean_text(provider.get("summary_model"), model),
        product_review_timeout_seconds=positive_int(
            review.get("product_review_timeout_seconds"),
            default=420,
        ),
        summary_timeout_seconds=positive_int(review.get("summary_timeout_seconds"), default=120),
        breakdown_timeout_seconds=positive_int(review.get("breakdown_timeout_seconds"), default=240),
    )
