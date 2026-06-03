import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from rover.common import elapsed_seconds
from rover.openrouter import (
    OpenRouterConfig,
    load_keyword_openrouter_config,
    openrouter_chat_url,
    openrouter_headers,
)
from rover.pipeline_logging import log_event


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class LLMKeywordIdea:
    keyword: str
    reason: str | None = None


class OpenRouterKeywordClient:
    def __init__(self, config: OpenRouterConfig):
        self.config = config

    @classmethod
    def from_env(cls, project_root: Path = PROJECT_ROOT) -> "OpenRouterKeywordClient":
        return cls(load_keyword_openrouter_config(project_root))

    def is_configured(self) -> bool:
        return self.config.is_configured

    def generate_keyword_ideas(
        self,
        base_keyword: str,
        winning_titles: list[str],
        bad_keywords: list[str],
        banned_terms: list[str],
        limit: int,
    ) -> list[LLMKeywordIdea]:
        if not self.is_configured():
            return []

        messages = build_messages(
            base_keyword=base_keyword,
            winning_titles=winning_titles,
            bad_keywords=bad_keywords,
            banned_terms=banned_terms,
            limit=limit,
        )

        first_attempt = self._request_keywords(messages)
        if first_attempt:
            return first_attempt

        retry_messages = messages + [
            {
                "role": "user",
                "content": "Return valid JSON only. Use the exact schema: {\"keywords\":[{\"keyword\":\"...\",\"reason\":\"...\"}]}",
            }
        ]
        return self._request_keywords(retry_messages)

    def _request_keywords(self, messages: list[dict[str, str]]) -> list[LLMKeywordIdea]:
        started_at = time.monotonic()
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 1400,
            "response_format": {"type": "json_object"},
        }
        log_event(
            "openrouter_keyword_request_started",
            stage="Generate new keywords",
            model=self.config.model,
            base_url=self.config.base_url,
            timeout_seconds=self.config.timeout_seconds,
            message_count=len(messages),
        )

        try:
            response = requests.post(
                openrouter_chat_url(self.config),
                headers=openrouter_headers(self.config),
                json=payload,
                timeout=self.config.timeout_seconds,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            response = getattr(error, "response", None)
            log_event(
                "openrouter_keyword_request_failed",
                stage="Generate new keywords",
                level="WARNING",
                elapsed_seconds=elapsed_seconds(started_at),
                model=self.config.model,
                status_code=getattr(response, "status_code", None),
                error_type=type(error).__name__,
                error=str(error),
            )
            return []

        ideas = parse_keyword_response(response.json())
        log_event(
            "openrouter_keyword_request_completed",
            stage="Generate new keywords",
            elapsed_seconds=elapsed_seconds(started_at),
            model=self.config.model,
            status_code=response.status_code,
            ideas_returned=len(ideas),
        )
        return ideas


def build_messages(
    base_keyword: str,
    winning_titles: list[str],
    bad_keywords: list[str],
    banned_terms: list[str],
    limit: int,
) -> list[dict[str, str]]:
    system_prompt = (
        "You generate retail search keywords for product sourcing. "
        "Return only JSON. Do not include markdown."
    )
    user_prompt = {
        "task": f"Generate {limit} related retail search keywords.",
        "base_keyword": base_keyword,
        "winning_product_titles": winning_titles[:20],
        "bad_keywords": bad_keywords[:30],
        "banned_terms": banned_terms,
        "rules": [
            "Prefer generic product search phrases.",
            "Avoid brands unless the user explicitly supplied the brand as the base keyword.",
            "Avoid supplements, food, cosmetics, medicines, gated items, and restricted items.",
            "Avoid overly broad keywords like toys, home, kitchen, gift, supplies, or accessories.",
            "Keep each keyword between 2 and 6 words.",
        ],
        "json_schema": {
            "keywords": [
                {
                    "keyword": "generic retail search phrase",
                    "reason": "short reason",
                }
            ]
        },
    }
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": json.dumps(user_prompt, sort_keys=True)},
    ]


def parse_keyword_response(response_json: dict[str, Any]) -> list[LLMKeywordIdea]:
    content = response_content(response_json)
    if not content:
        return []

    payload = parse_json_content(content)
    if not payload:
        return []

    if isinstance(payload, list):
        raw_keywords = payload
    elif isinstance(payload, dict):
        raw_keywords = payload.get("keywords", [])
    else:
        raw_keywords = []

    if not isinstance(raw_keywords, list):
        return []

    ideas = []
    for raw_item in raw_keywords:
        idea = parse_keyword_item(raw_item)
        if idea is None:
            continue
        ideas.append(idea)

    return ideas


def response_content(response_json: dict[str, Any]) -> str:
    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""

    message = choices[0].get("message")
    if not isinstance(message, dict):
        return ""

    content = message.get("content")
    return content.strip() if isinstance(content, str) else ""


def parse_json_content(content: str) -> Any | None:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    start = content.find("{")
    end = content.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        return json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None


def parse_keyword_item(raw_item: Any) -> LLMKeywordIdea | None:
    if isinstance(raw_item, str):
        keyword = raw_item.strip()
        return LLMKeywordIdea(keyword=keyword) if keyword else None

    if not isinstance(raw_item, dict):
        return None

    keyword = str(raw_item.get("keyword", "")).strip()
    if not keyword:
        return None

    reason = raw_item.get("reason")
    return LLMKeywordIdea(
        keyword=keyword,
        reason=str(reason).strip() if reason else None,
    )
