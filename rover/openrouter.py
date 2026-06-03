import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from rover.common import clean_text, positive_int

try:
    from pydantic_ai.models.openrouter import OpenRouterModel
    from pydantic_ai.providers.openrouter import OpenRouterProvider
except ImportError:
    OpenRouterModel = None
    OpenRouterProvider = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "deepseek/deepseek-v4-pro"
DEFAULT_TIMEOUT_SECONDS = 45
DEFAULT_APP_TITLE = "winning_product"


@dataclass(frozen=True)
class OpenRouterConfig:
    api_key: str
    model: str
    base_url: str
    timeout_seconds: int
    app_url: str | None = None
    app_title: str | None = DEFAULT_APP_TITLE

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)


def load_keyword_openrouter_config(project_root: Path = PROJECT_ROOT) -> OpenRouterConfig:
    return load_openrouter_config(
        project_root=project_root,
        api_key_env=("OPENROUTER_API_KEY",),
        model_env="OPENROUTER_MODEL",
        default_model=DEFAULT_MODEL,
        app_url_env=("OPENROUTER_REFERER",),
        app_title_env=("OPENROUTER_APP_TITLE",),
    )


def load_rover_openrouter_config(
    project_root: Path = PROJECT_ROOT,
    model_name: str | None = None,
) -> OpenRouterConfig:
    return load_openrouter_config(
        project_root=project_root,
        api_key_env=("ROVER_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
        model_env=None,
        default_model=clean_text(model_name, DEFAULT_MODEL) or DEFAULT_MODEL,
        app_url_env=("ROVER_OPENROUTER_APP_URL", "OPENROUTER_REFERER"),
        app_title_env=("ROVER_OPENROUTER_APP_TITLE", "OPENROUTER_APP_TITLE"),
    )


def load_openrouter_config(
    *,
    project_root: Path = PROJECT_ROOT,
    api_key_env: tuple[str, ...],
    model_env: str | None,
    default_model: str,
    app_url_env: tuple[str, ...],
    app_title_env: tuple[str, ...],
    timeout_env: str = "OPENROUTER_TIMEOUT_SECONDS",
    base_url_env: str = "OPENROUTER_BASE_URL",
) -> OpenRouterConfig:
    load_dotenv(project_root / ".env")
    return OpenRouterConfig(
        api_key=env_first(api_key_env, ""),
        model=env_value(model_env, default_model) if model_env else default_model,
        base_url=env_value(base_url_env, DEFAULT_BASE_URL).rstrip("/"),
        timeout_seconds=positive_int(os.getenv(timeout_env), DEFAULT_TIMEOUT_SECONDS),
        app_url=env_first(app_url_env),
        app_title=env_first(app_title_env, DEFAULT_APP_TITLE),
    )


def openrouter_config_from_values(
    *,
    api_key: str,
    model: str,
    app_url: str | None = None,
    app_title: str | None = DEFAULT_APP_TITLE,
    base_url: str | None = None,
    timeout_seconds: int | None = None,
) -> OpenRouterConfig:
    return OpenRouterConfig(
        api_key=api_key,
        model=model,
        base_url=(base_url or env_value("OPENROUTER_BASE_URL", DEFAULT_BASE_URL)).rstrip("/"),
        timeout_seconds=timeout_seconds
        if timeout_seconds is not None
        else positive_int(os.getenv("OPENROUTER_TIMEOUT_SECONDS"), DEFAULT_TIMEOUT_SECONDS),
        app_url=app_url,
        app_title=app_title,
    )


def env_value(name: str, default: str) -> str:
    return clean_text(os.getenv(name), default) or default


def env_first(names: tuple[str, ...], default: str | None = None) -> str | None:
    for name in names:
        value = clean_text(os.getenv(name))
        if value:
            return value
    return default


def openrouter_headers(config: OpenRouterConfig) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    if config.app_url:
        headers["HTTP-Referer"] = config.app_url
    if config.app_title:
        headers["X-Title"] = config.app_title
    return headers


def openrouter_chat_url(config: OpenRouterConfig) -> str:
    return f"{config.base_url}/chat/completions"


def build_openrouter_model(config: OpenRouterConfig, model_name: str | None = None) -> Any:
    if OpenRouterModel is None or OpenRouterProvider is None:
        raise RuntimeError(
            "Pydantic AI OpenRouter support is not installed. "
            "Install project requirements before running Rover."
        )

    provider = OpenRouterProvider(
        api_key=config.api_key,
        app_url=config.app_url or None,
        app_title=config.app_title or None,
    )
    return OpenRouterModel(model_name or config.model, provider=provider)
