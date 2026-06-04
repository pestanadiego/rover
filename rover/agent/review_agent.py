import asyncio
from contextlib import AsyncExitStack
import json
import os
import re
import sqlite3
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, Field

import pydantic_ai
from pydantic_ai import exceptions as pydantic_ai_errors
from pydantic_ai.common_tools.web_fetch import WebFetchLocalTool
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.tools import Tool

from ddgs import DDGS
from fastmcp.client.transports import StdioTransport
    
from rover.agent.config import RoverAgentConfig, load_rover_agent_config
from rover.agent.review_store import (
    ensure_agent_review_tables,
    finish_agent_review_run,
    record_product_review,
    start_agent_review_run,
)
from rover.common import elapsed_seconds, utc_now_iso
from rover.data_paths import default_db_path
from rover.openrouter import (
    build_openrouter_model as build_pydantic_openrouter_model,
    openrouter_config_from_values,
)
from rover.pipeline_logging import configure_pipeline_logging, log_event


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOTENV_PATH = PROJECT_ROOT / ".env"
MCP_DIR = PROJECT_ROOT / "mcp"
MCP_SERVER_PATH = MCP_DIR / "server.py"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

from db import ProductDB

ALLOWED_DECISIONS = ("keep", "reject", "needs_manual_review", "watchlist")


class ProductReviewOutput(BaseModel):
    asin: str
    decision: Literal["keep", "reject", "needs_manual_review", "watchlist"]
    analysis: str = Field(
        description="Detailed product analysis for internal review and the run breakdown."
    )
    notes: str | None = Field(
        default=None,
        description="Deprecated fallback. Prefer analysis.",
    )
    web_research_summary: str | None = None
    source_urls: list[str] = Field(default_factory=list)


class ProductSummaryOutput(BaseModel):
    summary: str = Field(
        description="Short product summary for the email table."
    )


class ReviewRunBreakdownOutput(BaseModel):
    breakdown: str = Field(
        description="Concise run-level breakdown for the email report. No Markdown headings."
    )


class ProviderRetriesExhausted(Exception):
    def __init__(self, label: str, attempts: list[str]):
        self.label = label
        self.attempts = attempts
        super().__init__(f"{label} failed after provider retries: {'; '.join(attempts)}")


def main() -> int:
    configure_pipeline_logging(PROJECT_ROOT)
    load_dotenv(DOTENV_PATH)
    return review_products()


def review_products(
    db_path: Path | None = None,
    scheduler_run_id: int | None = None,
    normalization_completed: bool = True,
    **_kwargs: Any,
) -> int:
    stage_started_at = time.monotonic()
    load_dotenv(DOTENV_PATH)
    config = load_rover_agent_config()

    if db_path:
        config = config_with_db_path(config, Path(db_path))
    log_event(
        "rover_stage_started",
        stage="Run Rover product review",
        enabled=config.enabled,
        required=config.required,
        db_path=config.db_path,
        model=config.model,
        max_products_per_run=config.max_products_per_run,
        scheduler_run_id=scheduler_run_id,
        normalization_completed=normalization_completed,
        mcp_enabled=config.mcp_enabled,
        web_search_enabled=config.web_search_enabled,
        parallel_tool_calls=config.parallel_tool_calls,
    )

    if not config.enabled:
        print("[SKIP] Rover agent disabled.")
        log_event(
            "rover_stage_skipped",
            stage="Run Rover product review",
            level="WARNING",
            elapsed_seconds=elapsed_seconds(stage_started_at),
            reason="disabled",
        )
        return 0

    if not normalization_completed:
        print("[SKIP] Rover agent skipped because no normalization completed.")
        log_event(
            "rover_stage_skipped",
            stage="Run Rover product review",
            level="WARNING",
            elapsed_seconds=elapsed_seconds(stage_started_at),
            reason="normalization_not_completed",
        )
        return 0

    try:
        log_rover(
            "Starting product review stage "
            f"model={config.model} db={config.db_path}"
        )
        exit_code = asyncio.run(run_rover_review(config, scheduler_run_id))
        log_event(
            "rover_stage_completed",
            stage="Run Rover product review",
            elapsed_seconds=elapsed_seconds(stage_started_at),
            exit_code=exit_code,
        )
        return exit_code
    except Exception as error:
        log_event(
            "rover_stage_failed",
            stage="Run Rover product review",
            level="ERROR",
            elapsed_seconds=elapsed_seconds(stage_started_at),
            error_type=type(error).__name__,
            error=str(error),
            traceback=traceback.format_exc(),
        )
        return handle_stage_error(error, config.required)


async def run_rover_review(
    config: RoverAgentConfig,
    scheduler_run_id: int | None,
) -> int:
    started_at = time.monotonic()
    validate_runtime_config(config)

    normalization_run = latest_normalization_run(config.db_path)
    if not normalization_run:
        log_rover("[SKIP] Rover agent found no normalization run.")
        log_event(
            "rover_no_normalization_run",
            stage="Run Rover product review",
            level="WARNING",
            db_path=config.db_path,
        )
        return 0

    log_rover(
        "Latest normalization run "
        f"id={normalization_run.get('id')} imported_at={normalization_run.get('imported_at_utc')}"
    )
    log_event(
        "rover_normalization_run_selected",
        stage="Run Rover product review",
        normalization_run_id=normalization_run.get("id"),
        imported_at_utc=normalization_run.get("imported_at_utc"),
    )
    products = products_pending_review_for_run(
        config.db_path,
        normalization_run["imported_at_utc"],
        config.max_products_per_run,
    )
    if not products:
        log_rover("[SKIP] Rover agent found no products needing review.")
        log_event(
            "rover_no_products_pending_review",
            stage="Run Rover product review",
            normalization_run_id=normalization_run.get("id"),
            imported_at_utc=normalization_run.get("imported_at_utc"),
        )
        return 0

    log_rover(
        "Selected products for Rover review "
        f"count={len(products)} max={config.max_products_per_run}"
    )
    log_event(
        "rover_products_selected",
        stage="Run Rover product review",
        product_count=len(products),
        max_products_per_run=config.max_products_per_run,
        asin_sample=[str(product.get("asin")) for product in products[:10]],
    )
    run_id = start_agent_review_run(
        config.db_path,
        config.agent_name,
        config.model,
        scheduler_run_id,
        int(normalization_run["id"]),
        len(products),
    )
    log_event(
        "rover_review_run_created",
        stage="Run Rover product review",
        rover_run_id=run_id,
        scheduler_run_id=scheduler_run_id,
        normalization_run_id=normalization_run.get("id"),
        products_selected=len(products),
    )

    try:
        review_results = await review_product_batch(config, products, run_id)
        breakdown = await safe_build_run_breakdown(config, review_results)
        status = "completed" if not any(result.get("error") for result in review_results) else "partial"
        finish_agent_review_run(config.db_path, run_id, status=status, breakdown=breakdown)
        log_rover(
            f"Rover reviewed {reviewed_count(review_results)} product(s) "
            f"({failed_count(review_results)} failed) in run {run_id}."
        )
        log_event(
            "rover_review_run_completed",
            stage="Run Rover product review",
            rover_run_id=run_id,
            elapsed_seconds=elapsed_seconds(started_at),
            status=status,
            reviewed_count=reviewed_count(review_results),
            failed_count=failed_count(review_results),
            decision_counts=review_decision_counts(review_results),
            breakdown_chars=len(breakdown or ""),
        )
        return 0 if status == "completed" or config.continue_on_product_error else 1
    except Exception as error:
        finish_agent_review_run(
            config.db_path,
            run_id,
            status="failed",
            error_message=str(error),
        )
        log_event(
            "rover_review_run_failed",
            stage="Run Rover product review",
            level="ERROR",
            rover_run_id=run_id,
            elapsed_seconds=elapsed_seconds(started_at),
            error_type=type(error).__name__,
            error=str(error),
            traceback=traceback.format_exc(),
        )
        raise


async def review_product_batch(
    config: RoverAgentConfig,
    products: list[dict[str, Any]],
    run_id: int,
) -> list[dict[str, Any]]:
    log_rover("Building Rover product review agents.")
    review_agent = build_product_review_agent(config)
    summary_agent = build_product_summary_agent(config)
    fallback_review_agent = build_fallback_product_review_agent(config)
    fallback_summary_agent = build_fallback_product_summary_agent(config)
    results = []

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(review_agent)
        if fallback_review_agent is not None:
            await stack.enter_async_context(fallback_review_agent)

        for index, product in enumerate(products, start=1):
            result = await review_one_product(
                config,
                review_agent,
                summary_agent,
                fallback_review_agent,
                fallback_summary_agent,
                product,
                run_id,
                index,
                len(products),
            )
            results.append(result)

            if result.get("error") and not config.continue_on_product_error:
                break

    return results


async def review_one_product(
    config: RoverAgentConfig,
    review_agent: Any,
    summary_agent: Any,
    fallback_review_agent: Any | None,
    fallback_summary_agent: Any | None,
    product: dict[str, Any],
    run_id: int,
    index: int,
    total: int,
) -> dict[str, Any]:
    asin = str(product["asin"]).strip().upper()
    started_at = time.monotonic()

    try:
        log_rover(
            f"Product {index}/{total} {asin}: review started "
            f"name={short_value(product.get('name'), 80)}"
        )
        agent_result = await run_agent_with_provider_retries(
            config,
            review_agent,
            fallback_review_agent,
            product_review_prompt(product, config),
            config.product_review_timeout_seconds,
            f"product review for {asin}",
        )
        review = agent_result.output
        analysis = review_analysis(review)
        log_rover(
            f"Product {index}/{total} {asin}: review completed "
            f"decision={review.decision} analysis_chars={len(analysis)} "
            f"sources={len(review.source_urls or [])} elapsed={elapsed_seconds(started_at)}s"
        )
        analysis_save = save_analysis_decision(config.db_path, review, analysis)
        log_rover(
            f"Product {index}/{total} {asin}: analysis saved "
            f"saved={analysis_save['saved']}"
        )
        try:
            summary_started_at = time.monotonic()
            log_rover(f"Product {index}/{total} {asin}: summary started.")
            summary = await summarize_product_review(
                config,
                summary_agent,
                fallback_summary_agent,
                product,
                review,
                analysis,
            )
            log_rover(
                f"Product {index}/{total} {asin}: summary completed "
                f"summary_chars={len(summary)} elapsed={elapsed_seconds(summary_started_at)}s"
            )
        except Exception as error:
            if isinstance(error, ProviderRetriesExhausted):
                return handle_product_provider_skip_after_analysis(
                    config,
                    run_id,
                    asin,
                    review,
                    analysis,
                    index,
                    total,
                    error,
                )

            if is_agent_infrastructure_error(error):
                raise

            return handle_product_summary_error(
                config,
                run_id,
                asin,
                review,
                analysis,
                index,
                total,
                error,
            )

        summary_save = save_review_summary(config.db_path, review, analysis, summary)
        log_rover(
            f"Product {index}/{total} {asin}: summary saved "
            f"saved={summary_save['saved']}"
        )
        record_product_review(
            config.db_path,
            run_id,
            asin,
            status="reviewed",
            decision=review.decision,
            notes=summary,
            analysis=analysis,
            summary=summary,
            web_research_summary=review.web_research_summary,
            source_urls=review.source_urls,
        )
        return {
            "asin": asin,
            "decision": review.decision,
            "notes": summary,
            "analysis": analysis,
            "summary": summary,
            "web_research_summary": review.web_research_summary,
            "source_urls": review.source_urls,
            "analysis_saved": analysis_save["saved"],
            "summary_saved": summary_save["saved"],
        }
    except Exception as error:
        log_rover(f"Product {index}/{total} {asin}: failed with {type(error).__name__}: {error}")
        if isinstance(error, ProviderRetriesExhausted):
            return handle_product_provider_skip(config, run_id, asin, error)

        if is_agent_infrastructure_error(error):
            raise

        return handle_product_review_error(config, run_id, product, error)


def handle_product_summary_error(
    config: RoverAgentConfig,
    run_id: int,
    asin: str,
    review: ProductReviewOutput,
    analysis: str,
    index: int,
    total: int,
    error: Exception,
) -> dict[str, Any]:
    message = f"Rover generated analysis but could not generate the table summary: {error}"
    log_rover(f"Product {index}/{total} {asin}: summary failed with {type(error).__name__}: {error}")
    traceback.print_exc()

    record_product_review(
        config.db_path,
        run_id,
        asin,
        status="failed",
        decision=review.decision,
        notes="",
        analysis=analysis,
        summary="",
        web_research_summary=review.web_research_summary,
        source_urls=review.source_urls,
        error_message=message,
    )

    return {
        "asin": asin,
        "decision": review.decision,
        "notes": "",
        "analysis": analysis,
        "summary": "",
        "web_research_summary": review.web_research_summary,
        "source_urls": review.source_urls,
        "error": message,
    }


def handle_product_provider_skip(
    config: RoverAgentConfig,
    run_id: int,
    asin: str,
    error: ProviderRetriesExhausted,
) -> dict[str, Any]:
    message = f"Skipped product after retryable provider errors: {error}"
    log_rover(f"Product provider skip for {asin}: {error}")

    record_product_review(
        config.db_path,
        run_id,
        asin,
        status="skipped",
        error_message=message,
    )

    return {
        "asin": asin,
        "decision": None,
        "notes": "",
        "analysis": "",
        "summary": "",
        "error": message,
        "skipped": True,
    }


def handle_product_provider_skip_after_analysis(
    config: RoverAgentConfig,
    run_id: int,
    asin: str,
    review: ProductReviewOutput,
    analysis: str,
    index: int,
    total: int,
    error: ProviderRetriesExhausted,
) -> dict[str, Any]:
    message = f"Skipped product summary after retryable provider errors: {error}"
    log_rover(f"Product {index}/{total} {asin}: provider skip during summary: {error}")

    record_product_review(
        config.db_path,
        run_id,
        asin,
        status="skipped",
        decision=review.decision,
        notes="",
        analysis=analysis,
        summary="",
        web_research_summary=review.web_research_summary,
        source_urls=review.source_urls,
        error_message=message,
    )

    return {
        "asin": asin,
        "decision": review.decision,
        "notes": "",
        "analysis": analysis,
        "summary": "",
        "web_research_summary": review.web_research_summary,
        "source_urls": review.source_urls,
        "error": message,
        "skipped": True,
    }


def handle_product_review_error(
    config: RoverAgentConfig,
    run_id: int,
    product: dict[str, Any],
    error: Exception,
) -> dict[str, Any]:
    asin = str(product["asin"]).strip().upper()
    message = f"Rover could not review this product automatically: {error}"
    summary = clean_summary(message, config.max_note_chars)
    analysis = clean_analysis(message)
    log_rover(f"Product review fallback for {asin}: {type(error).__name__}: {error}")
    traceback.print_exc()

    if config.write_manual_review_on_error:
        product_db(config.db_path).write_decision(
            asin,
            "needs_manual_review",
            summary,
            analysis=analysis,
            summary=summary,
        )

    record_product_review(
        config.db_path,
        run_id,
        asin,
        status="failed",
        decision="needs_manual_review" if config.write_manual_review_on_error else None,
        notes=summary,
        analysis=analysis,
        summary=summary,
        error_message=str(error),
    )

    return {
        "asin": asin,
        "decision": "needs_manual_review",
        "notes": summary,
        "analysis": analysis,
        "summary": summary,
        "error": str(error),
    }


async def build_run_breakdown(
    config: RoverAgentConfig,
    review_results: list[dict[str, Any]],
) -> str:
    if not review_results:
        return f"{config.agent_name} wasn't able to generate a breakdown."

    started_at = time.monotonic()
    log_rover(f"Run breakdown started for {len(review_results)} product result(s).")
    breakdown_agent = build_breakdown_agent(config)
    fallback_breakdown_agent = build_fallback_breakdown_agent(config)
    prompt = run_breakdown_prompt(review_results, config)
    result = await run_agent_with_provider_retries(
        config,
        breakdown_agent,
        fallback_breakdown_agent,
        prompt,
        config.breakdown_timeout_seconds,
        "run breakdown",
    )
    log_rover(f"Run breakdown completed elapsed={elapsed_seconds(started_at)}s")
    return clean_breakdown(result.output.breakdown)


async def safe_build_run_breakdown(
    config: RoverAgentConfig,
    review_results: list[dict[str, Any]],
) -> str:
    try:
        return await build_run_breakdown(config, review_results)
    except Exception as error:
        print(f"[!] Rover breakdown failed: {error}")
        return f"{config.agent_name} wasn't able to generate a breakdown."


async def summarize_product_review(
    config: RoverAgentConfig,
    summary_agent: Any,
    fallback_summary_agent: Any | None,
    product: dict[str, Any],
    review: ProductReviewOutput,
    analysis: str,
) -> str:
    result = await run_agent_with_provider_retries(
        config,
        summary_agent,
        fallback_summary_agent,
        product_summary_prompt(product, review, analysis),
        config.summary_timeout_seconds,
        f"summary for {review.asin}",
        primary_model_name=summary_model_name(config),
    )
    return clean_summary(result.output.summary, config.max_note_chars)


def build_product_review_agent(config: RoverAgentConfig, model_name: str | None = None) -> Any:
    pydantic_ai = import_pydantic_ai()
    model = build_openrouter_model(config, model_name)
    toolsets = build_mcp_toolsets(config)
    tools = build_web_tools(config)
    display_model = model_name or config.model
    log_rover(
        "Product review agent tool config "
        f"model={display_model} "
        f"mcp_enabled={config.mcp_enabled} "
        f"web_search_enabled={config.web_search_enabled} "
        f"parallel_tool_calls={config.parallel_tool_calls}"
    )

    return pydantic_ai.Agent(
        model,
        output_type=ProductReviewOutput,
        instructions=product_review_instructions(config),
        tools=tools,
        toolsets=toolsets,
        model_settings=tool_model_settings(config),
        max_concurrency=tool_max_concurrency(config),
        retries=3,
    )


def build_fallback_product_review_agent(config: RoverAgentConfig) -> Any | None:
    fallback_model = clean_fallback_model(config)
    if not fallback_model:
        return None

    log_rover(f"Fallback review model enabled: {fallback_model}")
    return build_product_review_agent(config, fallback_model)


def build_product_summary_agent(config: RoverAgentConfig, model_name: str | None = None) -> Any:
    pydantic_ai = import_pydantic_ai()
    display_model = model_name or summary_model_name(config)
    model = build_openrouter_model(config, display_model)
    log_rover(f"Product summary agent model={display_model}")

    return pydantic_ai.Agent(
        model,
        output_type=ProductSummaryOutput,
        instructions=product_summary_instructions(config),
        retries=3,
    )


def build_fallback_product_summary_agent(config: RoverAgentConfig) -> Any | None:
    fallback_model = clean_fallback_model(config, summary_model_name(config))
    if not fallback_model:
        return None

    log_rover(f"Fallback summary model enabled: {fallback_model}")
    return build_product_summary_agent(config, fallback_model)


def build_breakdown_agent(config: RoverAgentConfig) -> Any:
    pydantic_ai = import_pydantic_ai()
    model = build_openrouter_model(config)

    return pydantic_ai.Agent(
        model,
        output_type=ReviewRunBreakdownOutput,
        instructions=breakdown_instructions(config),
        retries=3,
    )


def build_fallback_breakdown_agent(config: RoverAgentConfig) -> Any | None:
    fallback_model = clean_fallback_model(config)
    if not fallback_model:
        return None

    log_rover(f"Fallback breakdown model enabled: {fallback_model}")
    pydantic_ai = import_pydantic_ai()
    model = build_openrouter_model(config, fallback_model)

    return pydantic_ai.Agent(
        model,
        output_type=ReviewRunBreakdownOutput,
        instructions=breakdown_instructions(config),
        retries=3,
    )


def build_openrouter_model(config: RoverAgentConfig, model_name: str | None = None) -> Any:
    model = model_name or config.model
    openrouter_config = openrouter_config_from_values(
        api_key=config.api_key,
        model=model,
        app_url=config.app_url,
        app_title=config.app_title,
    )
    return build_pydantic_openrouter_model(openrouter_config)


def summary_model_name(config: RoverAgentConfig) -> str:
    return str(config.summary_model or config.model).strip() or config.model


def clean_fallback_model(config: RoverAgentConfig, primary_model_name: str | None = None) -> str:
    fallback_model = str(config.fallback_model or "").strip()
    if not fallback_model:
        return ""

    primary_model = primary_model_name or config.model
    if fallback_model == primary_model:
        return ""

    return fallback_model


def build_mcp_toolsets(config: RoverAgentConfig) -> list[Any]:
    if not config.mcp_enabled:
        return []

    if StdioTransport is None or MCPToolset is None:
        raise RuntimeError(
            "Pydantic AI MCP support is not installed. "
            "Install project requirements before running Rover."
        )

    env = os.environ.copy()
    env["PRODUCT_DB_PATH"] = str(config.db_path)

    pythonpath_entries = [str(MCP_DIR), str(PROJECT_ROOT)]
    if env.get("PYTHONPATH"):
        pythonpath_entries.append(env["PYTHONPATH"])

    env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)

    transport = StdioTransport(
        command=sys.executable,
        args=[str(MCP_SERVER_PATH)],
        env=env,
        cwd=str(PROJECT_ROOT),
    )

    return [
        MCPToolset(
            transport,
            process_tool_call=review_agent_mcp_tool_call,
            init_timeout=config.mcp_timeout_seconds,
            read_timeout=config.mcp_timeout_seconds,
        )
    ]


def build_web_tools(config: RoverAgentConfig) -> list[Any]:
    if not config.web_search_enabled:
        log_rover("Web search tools disabled by config.")
        return []

    if DDGS is None or WebFetchLocalTool is None or Tool is None:
        raise RuntimeError(
            "Pydantic AI web search tools are not installed. "
            "Install project requirements before running Rover."
        )

    duckduckgo_client = DDGS()
    fetch_client = WebFetchLocalTool(
        max_content_length=50_000,
        allow_local_urls=False,
        timeout=config.web_fetch_timeout_seconds,
    )

    async def duckduckgo_search(query: str) -> list[dict[str, Any]]:
        """Search DuckDuckGo for product sourcing evidence."""
        started_at = time.monotonic()
        log_rover(f"Web tool started: duckduckgo_search query={short_value(query, 180)}")
        try:
            search = lambda: duckduckgo_client.text(
                query,
                max_results=web_search_max_results(config.web_search_context_size),
            )
            results = await run_with_timeout(
                asyncio.to_thread(search),
                config.web_search_timeout_seconds,
                "duckduckgo_search",
            )
        except Exception as error:
            log_rover(
                "Web tool failed: duckduckgo_search "
                f"error={type(error).__name__}: {error}"
            )
            raise

        log_rover(
            "Web tool completed: duckduckgo_search "
            f"results={len(results or [])} elapsed={elapsed_seconds(started_at)}s"
        )
        return list(results or [])

    async def web_fetch(url: str) -> Any:
        """Fetch a web page and return markdown or binary content."""
        started_at = time.monotonic()
        log_rover(f"Web tool started: web_fetch url={short_value(url, 220)}")
        try:
            result = await run_with_timeout(
                fetch_client(url),
                config.web_fetch_timeout_seconds,
                f"web_fetch {short_value(url, 120)}",
            )
        except Exception as error:
            log_rover(f"Web tool failed: web_fetch error={type(error).__name__}: {error}")
            return soft_web_fetch_failure(url, error)

        log_rover(
            "Web tool completed: web_fetch "
            f"{web_fetch_result_summary(result)} elapsed={elapsed_seconds(started_at)}s"
        )
        return result

    return [
        Tool(
            duckduckgo_search,
            name="duckduckgo_search",
            description="Searches DuckDuckGo for the given query and returns the results.",
            timeout=config.web_search_timeout_seconds,
        ),
        Tool(
            web_fetch,
            name="web_fetch",
            description="Fetches the content of a web page at the given URL and returns it as markdown or binary content.",
            timeout=config.web_fetch_timeout_seconds,
        ),
    ]


def soft_web_fetch_failure(url: str, error: Exception) -> dict[str, Any]:
    return {
        "fetch_ok": False,
        "url": url,
        "error_type": type(error).__name__,
        "error": short_value(str(error), 500),
        "message": "Fetch failed. Treat this URL as unavailable and do not retry the same URL.",
    }


def tool_model_settings(config: RoverAgentConfig) -> dict[str, Any]:
    return {
        "parallel_tool_calls": config.parallel_tool_calls,
    }


def tool_max_concurrency(config: RoverAgentConfig) -> int | None:
    if config.parallel_tool_calls:
        return None

    return 1


def import_pydantic_ai() -> Any:
    if pydantic_ai is None:
        raise RuntimeError(
            "Missing pydantic-ai. Install project requirements before running Rover."
        )

    return pydantic_ai


async def review_agent_mcp_tool_call(
    _ctx: Any,
    call_tool: Any,
    name: str,
    tool_args: dict[str, Any],
) -> Any:
    if name == "write_decision":
        log_rover("MCP tool blocked: write_decision")
        return {
            "saved": False,
            "message": "write_decision is disabled for Rover's research step. Return structured output instead.",
        }

    started_at = time.monotonic()
    log_rover(f"MCP tool started: {name} args={safe_tool_args(tool_args)}")
    try:
        result = await call_tool(name, tool_args)
    except Exception as error:
        log_rover(f"MCP tool failed: {name} error={type(error).__name__}: {error}")
        raise

    log_rover(f"MCP tool completed: {name} elapsed={elapsed_seconds(started_at)}s")
    return result


def web_search_max_results(context_size: str) -> int:
    sizes = {
        "low": 3,
        "small": 3,
        "medium": 5,
        "high": 8,
        "large": 8,
    }
    return sizes.get(str(context_size or "").strip().lower(), 5)


def product_review_instructions(config: RoverAgentConfig) -> str:
    web_research_instruction = product_review_web_research_instruction(config)
    base_instructions = f"""
You are {config.agent_name}, a product sourcing review agent.

For every product:
- Call get_product_by_asin with the supplied ASIN before deciding.
{web_research_instruction}
- Consider profitability, sales rank, estimated sales, seller count, brand/category risk, and sourcing evidence.
- Do not invent facts, URLs, retailers, or pricing.
- Do not call write_decision. The pipeline saves your structured decision after validation.
- Treat cost_price of 0, blank, or missing as unknown cost. Never treat it as free inventory or as proof of bad economics.
- When cost is unknown and web research is enabled, estimate a realistic acquisition cost from external sources such as Walmart, Target, brand pages, wholesale pages, Faire, Alibaba, or other retail/source listings. Mention the estimate and source in analysis.
- Compare known or estimated acquisition cost against max_cost and breakeven. If cost is still unknown after research, say that plainly.
- Use "keep" only when the product has strong economics, acceptable risk, and a known or well-supported estimated acquisition cost below max_cost from a plausible source.
- Use "watchlist" when demand and competition look promising but cost, authorization, sourcing quality, or brand risk still needs confirmation.
- Use "needs_manual_review" when the available data is contradictory, too incomplete, or requires a human to verify authorization/source terms before deciding.
- Use "reject" for weak margin, poor demand, excessive competition, high fees/weight, risky/gated categories, confirmed source cost above max_cost, or no viable sourcing signal after bounded research.
- Do not choose "reject" solely because cost is unknown if demand and market signals are otherwise promising.
- Return the ASIN exactly as supplied.
- Return source_urls only for real URLs found through tools.
- Return analysis as detailed product reasoning, usually several sentences or compact bullets. For non-obvious products, use up to about 1000 characters. Include the cost assumption, source evidence, and the reason for the decision.
- Do not use Markdown headings in analysis. Never start a line with #, ##, or ###.
- Never use em dashes. Use commas, semicolons, parentheses, or short sentences instead.
- Use plain ASCII punctuation only.

Return structured output only.
""".strip()

    return join_instruction_sections(base_instructions, shared_system_prompt_section(config))


def product_review_web_research_instruction(config: RoverAgentConfig) -> str:
    if config.web_search_enabled:
        return (
            "- Use duckduckgo_search and web_fetch to verify the product "
            "and look for external retail/source signals.\n"
            "- Research budget: use at most "
            f"{config.web_search_max_searches} duckduckgo_search calls and at most "
            f"{config.web_fetch_max_fetches} web_fetch calls per product.\n"
            "- Do not fetch the same URL more than once. If a fetch returns fetch_ok=false, treat that URL as unavailable and move on.\n"
            "- Stop web research once you have an exact or near-exact product match, a realistic cost/source estimate, and one risk signal."
        )

    return (
        "- Web research tools are disabled for this run. "
        "Do not use product database search tools as a substitute for external web research."
    )


def product_summary_instructions(config: RoverAgentConfig) -> str:
    base_instructions = f"""
You are {config.agent_name}, writing the table summary after a product has already been reviewed.

Write one short sentence for the email table:
- Summarize only the supplied analysis and decision.
- Do not add new facts, retailers, pricing, or risk claims.
- Make it useful at a glance.
- Do not use Markdown.
- Do not use Markdown headings. Never start a line with #, ##, or ###.
- Never use em dashes. Use commas, semicolons, parentheses, or short sentences instead.
- Use plain ASCII punctuation only.

Return structured output only.
""".strip()

    return join_instruction_sections(base_instructions, shared_system_prompt_section(config))


def breakdown_instructions(config: RoverAgentConfig) -> str:
    base_instructions = f"""
You are {config.agent_name}, summarizing one product review run.

Write a concise breakdown for an email report. Include:
- strongest opportunities, if any
- common concerns or rejection patterns
- useful sourcing or market observations
- what a human should check next

Do not claim certainty beyond the reviewed evidence.
Do not use Markdown headings. Never start a line with #, ##, or ###.
Bold words and bullet points are allowed.
Never use em dashes. Use commas, semicolons, parentheses, or short sentences instead.
Use plain ASCII punctuation only.
""".strip()

    return join_instruction_sections(base_instructions, shared_system_prompt_section(config))


def shared_system_prompt_section(config: RoverAgentConfig) -> str:
    if not config.shared_system_prompt:
        return ""

    return f"""
Shared writing style:
{config.shared_system_prompt}
""".strip()


def join_instruction_sections(*sections: str) -> str:
    return "\n\n".join(section.strip() for section in sections if section and section.strip())


def product_review_prompt(product: dict[str, Any], config: RoverAgentConfig) -> str:
    payload = {
        "asin": product.get("asin"),
        "name": product.get("name"),
        "brand": product.get("brand"),
        "category": product.get("category"),
        "scrape_keyword": product.get("scrape_keyword"),
        "cost_price": product.get("cost_price"),
        "profit": product.get("profit"),
        "roi_percent": product.get("roi_percent"),
        "profit_margin_percent": product.get("profit_margin_percent"),
        "sale_price": product.get("sale_price"),
        "buy_box_current": product.get("buy_box_current"),
        "breakeven": product.get("breakeven"),
        "max_cost": product.get("max_cost"),
        "fba_fee": product.get("fba_fee"),
        "total_fees": product.get("total_fees"),
        "sales_rank_current": product.get("sales_rank_current"),
        "estimated_sales": product.get("estimated_sales"),
        "total_seller_count": product.get("total_seller_count"),
    }

    return (
        "Review this product. Use MCP tools and web search as instructed. "
        "Return the structured decision only; do not save the decision yourself. "
        "Put the detailed reasoning in analysis. Do not write the table summary in this step.\n\n"
        f"{json.dumps(payload, sort_keys=True)}"
    )


def product_summary_prompt(
    product: dict[str, Any],
    review: ProductReviewOutput,
    analysis: str,
) -> str:
    payload = {
        "asin": review.asin,
        "name": product.get("name"),
        "brand": product.get("brand"),
        "decision": review.decision,
        "analysis": analysis,
        "web_research_summary": review.web_research_summary,
    }

    return (
        "Generate the short email-table summary for this completed product review. "
        "Do not re-review the product. Do not add new facts.\n\n"
        f"{json.dumps(payload, sort_keys=True)}"
    )


def run_breakdown_prompt(
    review_results: list[dict[str, Any]],
    config: RoverAgentConfig,
) -> str:
    compact_results = [
        {
            "asin": result.get("asin"),
            "decision": result.get("decision"),
            "analysis": result.get("analysis"),
            "summary": result.get("summary") or result.get("notes"),
            "web_research_summary": result.get("web_research_summary"),
            "source_urls": result.get("source_urls", [])[:3],
            "error": result.get("error"),
        }
        for result in review_results
    ]

    return (
        "Create Rover's run-level breakdown from these product review results. "
        "Use the analysis field for substance and the summary field for quick grouping. "
        "Keep it concise enough for an email. Do not include titles, subtitles, or Markdown headings.\n\n"
        f"{json.dumps(compact_results, sort_keys=True)}"
    )


def latest_normalization_run(db_path: Path) -> dict[str, Any] | None:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        ensure_agent_review_tables(conn)
        row = conn.execute(
            """
            SELECT *
            FROM normalization_runs
            ORDER BY imported_at_utc DESC, id DESC
            LIMIT 1
            """
        ).fetchone()

    return dict(row) if row else None


def products_pending_review_for_run(
    db_path: Path,
    imported_at_utc: str,
    limit: int,
) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT p.*
            FROM products p
            LEFT JOIN product_reviews r ON r.asin = p.asin
            WHERE p.imported_at_utc = ?
              AND (
                  r.status IS NULL
                  OR r.status = 'pending'
                  OR r.status = 'summary_pending'
              )
            ORDER BY p.profit DESC, p.roi_percent DESC, p.sales_rank_current ASC, p.id DESC
            LIMIT ?
            """,
            (imported_at_utc, limit),
        ).fetchall()

    return [dict(row) for row in rows]


def save_analysis_decision(
    db_path: Path,
    review: ProductReviewOutput,
    analysis: str,
) -> dict[str, Any]:
    db = product_db(db_path)
    result = db.write_decision(
        review.asin,
        review.decision,
        "",
        analysis=analysis,
        summary="",
        status_override="summary_pending",
    )
    return {"saved": bool(result.get("saved"))}


def save_review_summary(
    db_path: Path,
    review: ProductReviewOutput,
    analysis: str,
    summary: str,
) -> dict[str, Any]:
    db = product_db(db_path)
    result = db.write_decision(
        review.asin,
        review.decision,
        summary,
        analysis=analysis,
        summary=summary,
    )
    return {"saved": bool(result.get("saved"))}


async def run_with_timeout(awaitable: Any, timeout_seconds: int, label: str) -> Any:
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_seconds)
    except asyncio.TimeoutError as error:
        raise TimeoutError(f"{label} timed out after {timeout_seconds}s") from error


async def run_agent_with_provider_retries(
    config: RoverAgentConfig,
    primary_agent: Any,
    fallback_agent: Any | None,
    prompt: str,
    timeout_seconds: int,
    label: str,
    primary_model_name: str | None = None,
) -> Any:
    attempts = []
    primary_model = primary_model_name or config.model
    agent_options = [("primary", primary_model, primary_agent)]

    fallback_model = clean_fallback_model(config, primary_model)
    if fallback_agent is not None and fallback_model:
        agent_options.append(("fallback", fallback_model, fallback_agent))

    for agent_role, model_name, agent in agent_options:
        for attempt_number in range(1, config.provider_retry_count + 2):
            try:
                if agent_role == "fallback" and attempt_number == 1:
                    log_rover(f"{label}: switching to fallback model {model_name}")

                return await run_with_timeout(
                    agent.run(prompt),
                    timeout_seconds,
                    label,
                )
            except Exception as error:
                if not is_retryable_provider_error(error):
                    raise

                detail = retryable_provider_error_detail(error)
                attempts.append(f"{model_name} attempt {attempt_number}: {detail}")
                log_rover(
                    f"{label}: retryable provider error on {model_name} "
                    f"attempt={attempt_number} error={detail}"
                )

                if attempt_number <= config.provider_retry_count:
                    await sleep_before_provider_retry(config, attempt_number)

    raise ProviderRetriesExhausted(label, attempts)


async def sleep_before_provider_retry(config: RoverAgentConfig, attempt_number: int) -> None:
    delay = config.provider_retry_backoff_seconds * attempt_number
    if delay <= 0:
        return

    log_rover(f"Waiting {delay}s before retrying provider request.")
    await asyncio.sleep(delay)


def is_retryable_provider_error(error: Exception) -> bool:
    if not is_model_http_error(error):
        return False

    status_code = int(getattr(error, "status_code", 0) or 0)
    if status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
        return True

    text = retryable_provider_error_detail(error).lower()
    retryable_fragments = (
        "temporarily rate-limited",
        "rate-limited upstream",
        "rate limited",
        "rate limit",
        "'code': 429",
        '"code":429',
        "provider returned error",
        "upstream",
        "overloaded",
        "timeout",
    )
    return any(fragment in text for fragment in retryable_fragments)


def is_model_http_error(error: Exception) -> bool:
    if pydantic_ai_errors is None:
        return False

    return isinstance(error, pydantic_ai_errors.ModelHTTPError)


def retryable_provider_error_detail(error: Exception) -> str:
    body = getattr(error, "body", None)
    if body:
        return short_value(body, 500)

    return short_value(str(error), 500)


def is_agent_infrastructure_error(error: Exception) -> bool:
    if pydantic_ai_errors is None:
        return False

    infrastructure_errors = (
        pydantic_ai_errors.ModelAPIError,
        pydantic_ai_errors.ModelHTTPError,
        pydantic_ai_errors.ToolRetryError,
        pydantic_ai_errors.UnexpectedModelBehavior,
        pydantic_ai_errors.UsageLimitExceeded,
        pydantic_ai_errors.UserError,
    )
    return isinstance(error, infrastructure_errors)


def product_db(db_path: Path | None = None) -> Any:
    return ProductDB(db_path or default_db_path())


def validate_runtime_config(config: RoverAgentConfig) -> None:
    if not config.api_key:
        raise RuntimeError("Missing OPENROUTER_API_KEY or ROVER_OPENROUTER_API_KEY.")


def reviewed_count(results: list[dict[str, Any]]) -> int:
    return sum(1 for result in results if not result.get("error"))


def failed_count(results: list[dict[str, Any]]) -> int:
    return sum(1 for result in results if result.get("error"))


def review_decision_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {decision: 0 for decision in ALLOWED_DECISIONS}
    counts["error"] = 0
    counts["skipped"] = 0
    for result in results:
        if result.get("skipped"):
            counts["skipped"] += 1
        if result.get("error"):
            counts["error"] += 1
        decision = result.get("decision")
        if decision in counts:
            counts[decision] += 1
    return counts


def log_rover(message: str) -> None:
    configure_pipeline_logging(PROJECT_ROOT)
    print(f"{utc_now_iso()} [ROVER] {message}", flush=True)
    log_event(
        "rover_log",
        stage="Run Rover product review",
        level="DEBUG",
        message=message,
    )


def short_value(value: Any, max_chars: int) -> str:
    text = str(value or "N/A").strip()
    if len(text) <= max_chars:
        return text

    return text[: max_chars - 3].rstrip() + "..."


def safe_tool_args(tool_args: dict[str, Any]) -> str:
    safe_args = {}
    for key, value in (tool_args or {}).items():
        if is_sensitive_key(key):
            safe_args[key] = "***"
            continue

        safe_args[key] = short_value(value, 120)

    return json.dumps(safe_args, sort_keys=True)


def web_fetch_result_summary(result: Any) -> str:
    url = short_value(getattr(result, "url", ""), 120)
    content = getattr(result, "content", None)
    media_type = getattr(result, "media_type", None)

    if content is not None:
        return f"url={url or 'N/A'} content_chars={len(str(content))}"

    if media_type:
        data = getattr(result, "data", b"")
        return f"media_type={media_type} bytes={len(data or b'')}"

    return f"type={type(result).__name__}"


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in ("password", "token", "secret", "key"))


def clean_notes(notes: Any, max_chars: int) -> str:
    return clean_summary(notes, max_chars)


def review_analysis(review: ProductReviewOutput) -> str:
    return clean_analysis(review.analysis or review.notes)


def clean_summary(value: Any, max_chars: int) -> str:
    text = normalize_agent_text(value)
    if not text:
        return "No summary generated."

    return text


def clean_analysis(value: Any) -> str:
    text = normalize_agent_text(value)
    if not text:
        return "No analysis generated."

    lines = [strip_markdown_heading(line) for line in text.splitlines()]
    return "\n".join(line for line in lines if line.strip()).strip()


def clean_breakdown(value: Any) -> str:
    text = clean_analysis(value)
    if text == "No analysis generated.":
        return "Rover wasn't able to generate a breakdown."

    return text


def strip_markdown_heading(line: str) -> str:
    return re.sub(r"^\s*#{1,6}\s*", "", line).strip()


def normalize_agent_text(value: Any) -> str:
    text = str(value or "").strip()
    replacements = {
        "\u2013": "-",
        "\u2014": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text


def config_with_db_path(config: RoverAgentConfig, db_path: Path) -> RoverAgentConfig:
    return RoverAgentConfig(
        enabled=config.enabled,
        required=config.required,
        agent_name=config.agent_name,
        model=config.model,
        shared_system_prompt=config.shared_system_prompt,
        api_key=config.api_key,
        app_url=config.app_url,
        app_title=config.app_title,
        db_path=db_path,
        max_products_per_run=config.max_products_per_run,
        continue_on_product_error=config.continue_on_product_error,
        write_manual_review_on_error=config.write_manual_review_on_error,
        web_search_enabled=config.web_search_enabled,
        web_search_context_size=config.web_search_context_size,
        web_search_timeout_seconds=config.web_search_timeout_seconds,
        web_fetch_timeout_seconds=config.web_fetch_timeout_seconds,
        web_search_max_searches=config.web_search_max_searches,
        web_fetch_max_fetches=config.web_fetch_max_fetches,
        parallel_tool_calls=config.parallel_tool_calls,
        mcp_enabled=config.mcp_enabled,
        mcp_timeout_seconds=config.mcp_timeout_seconds,
        max_note_chars=config.max_note_chars,
        provider_retry_count=config.provider_retry_count,
        provider_retry_backoff_seconds=config.provider_retry_backoff_seconds,
        fallback_model=config.fallback_model,
        summary_model=config.summary_model,
        product_review_timeout_seconds=config.product_review_timeout_seconds,
        summary_timeout_seconds=config.summary_timeout_seconds,
        breakdown_timeout_seconds=config.breakdown_timeout_seconds,
    )


def handle_stage_error(error: Exception, required: bool) -> int:
    if required:
        print(f"[!] Rover agent failed: {error}")
        traceback.print_exc()
        return 1

    print(f"[!] Rover agent failed, pipeline will continue: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
