import time
import traceback
from pathlib import Path

from dotenv import load_dotenv

from rover.common import elapsed_seconds
from rover.keywords import generator as keyword_generator
from rover.keywords import stats as keyword_stats
from rover.keywords.store import KeywordStore
from rover.pipeline_alerts import RunOutcome, process_pipeline_alert
from rover.pipeline_logging import (
    configure_pipeline_logging,
    get_pipeline_logger,
    log_event,
    set_pipeline_run_context,
)
from rover.pipeline_lock import PipelineAlreadyRunning, PipelineLock
from rover.agent import review_agent as product_review_agent
from rover.products import normalizer as product_normalizer
from rover.products import sync as sync_products
from rover.reports import email_report
from rover.scraping import product_scraper
from rover.scraping.config import load_scraper_config
from rover.scraping.sheet_keyword_writer import ScrapeKeywordSheetUpdater


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOTENV_PATH = PROJECT_ROOT / ".env"

KEYWORD_STATS_HOOK = "refresh_keyword_stats"
KEYWORD_GENERATOR_HOOK = "generate_keywords"
PRODUCT_REVIEW_AGENT_HOOK = "review_products"
EMAIL_REPORT_HOOK = "send_latest_report"


def main() -> int:
    logging_config = configure_pipeline_logging(PROJECT_ROOT)
    log_event(
        "pipeline_process_started",
        stage="orchestrator",
        log_path=logging_config.get("log_path"),
        jsonl_path=logging_config.get("jsonl_path"),
        log_level=logging_config.get("level"),
    )

    load_dotenv(DOTENV_PATH)
    keyword_store = KeywordStore()
    outcome = RunOutcome()
    exit_code = 1

    try:
        with PipelineLock(db_path=keyword_store.db_path) as lock:
            outcome.lock_run_id = lock.run_id
            outcome.reclaimed_info = lock.reclaimed_info
            set_pipeline_run_context(lock.run_id)
            log_event(
                "pipeline_lock_acquired",
                stage="orchestrator",
                db_path=keyword_store.db_path,
                lock_run_id=lock.run_id,
                reclaimed_info=lock.reclaimed_info,
            )
            exit_code = run_pipeline(keyword_store, outcome)
            if exit_code != 0:
                log_event(
                    "pipeline_marking_lock_failed",
                    stage="orchestrator",
                    level="ERROR",
                    exit_code=exit_code,
                )
                lock.mark_failed(f"Pipeline exited with code {exit_code}.")
    except PipelineAlreadyRunning as error:
        outcome.skipped = True
        outcome.skipped_detail = str(error)
        print(f"[SKIP] {error}")
        log_event(
            "pipeline_skipped_already_running",
            stage="orchestrator",
            level="WARNING",
            error=str(error),
        )
        exit_code = 1
    except Exception as error:
        outcome.exception = traceback.format_exc()
        outcome.fail("pipeline", str(error))
        print(f"[!] Pipeline crashed: {error}")
        traceback.print_exc()
        log_event(
            "pipeline_crashed",
            stage="orchestrator",
            level="ERROR",
            error_type=type(error).__name__,
            error=str(error),
            traceback=outcome.exception,
        )
        exit_code = 1
    finally:
        alert_result = process_pipeline_alert(outcome, exit_code)
        log_event(
            "pipeline_alert_processed",
            stage="alerts",
            exit_code=exit_code,
            alert_result=alert_result,
        )
        log_event(
            "pipeline_process_finished",
            stage="orchestrator",
            exit_code=exit_code,
            failed_stage=outcome.failed_stage,
            report_email_enabled=outcome.report_email_enabled,
            report_email_sent=outcome.report_email_sent,
            skipped=outcome.skipped,
        )

    return exit_code


def run_pipeline(keyword_store: KeywordStore, outcome: RunOutcome) -> int:
    scraper_config = load_scraper_config()
    global_winner_limit = scraper_config.scrape.global_winner_limit
    log_event(
        "pipeline_config_loaded",
        stage="orchestrator",
        db_path=keyword_store.db_path,
        browser_mode=scraper_config.browser.mode,
        global_winner_limit=global_winner_limit,
        target_winners_per_keyword=scraper_config.scrape.target_winners_per_keyword,
        max_pages_to_scrape=scraper_config.scrape.max_pages_to_scrape,
        start_page=scraper_config.scrape.start_page,
    )
    common_kwargs = {
        "keyword_store": keyword_store,
        "project_root": PROJECT_ROOT,
        "db_path": keyword_store.db_path,
        "run_outcome": outcome,
    }

    ok, _ = run_optional_module_stage(
        keyword_stats,
        "Refresh keyword stats before scrape",
        KEYWORD_STATS_HOOK,
        outcome,
        **common_kwargs,
    )
    if not ok:
        return 1

    selection_started_at = time.monotonic()
    try:
        print("[PIPELINE] Selecting scheduled keywords...")
        log_event(
            "stage_started",
            stage="Select scheduled keywords",
            label="Select scheduled keywords",
            global_winner_limit=global_winner_limit,
        )
        keywords_to_scrape, scheduler_run_id = product_scraper.select_keywords_with_scheduler(
            keyword_store=keyword_store,
            global_winner_limit=global_winner_limit,
        )
    except Exception as error:
        print(f"[!] Keyword selection failed: {error}")
        traceback.print_exc()
        log_event(
            "stage_failed",
            stage="Select scheduled keywords",
            level="ERROR",
            elapsed_seconds=elapsed_seconds(selection_started_at),
            error_type=type(error).__name__,
            error=str(error),
            traceback=traceback.format_exc(),
        )
        outcome.fail("Select scheduled keywords", str(error))
        return 1

    selected_keywords = product_scraper.normalize_selected_keywords(keywords_to_scrape)
    log_event(
        "stage_completed",
        stage="Select scheduled keywords",
        elapsed_seconds=elapsed_seconds(selection_started_at),
        scheduler_run_id=scheduler_run_id,
        selected_count=len(selected_keywords),
        selected_keywords=selected_keyword_names(selected_keywords),
    )
    if not selected_keywords:
        print("[PIPELINE] No keywords selected. Skipping scrape, sync, and normalization.")
        log_event(
            "pipeline_no_keywords_selected",
            stage="orchestrator",
            scheduler_run_id=scheduler_run_id,
        )
        return finish_without_scrape(common_kwargs, outcome, scheduler_run_id)

    sheet_setup_started_at = time.monotonic()
    try:
        log_event("stage_started", stage="Google Sheets setup", label="Google Sheets setup")
        sheet_updater = ScrapeKeywordSheetUpdater.from_env(PROJECT_ROOT)
    except RuntimeError as error:
        print(f"[!] Google Sheets setup failed: {error}")
        log_event(
            "stage_failed",
            stage="Google Sheets setup",
            level="ERROR",
            elapsed_seconds=elapsed_seconds(sheet_setup_started_at),
            error_type=type(error).__name__,
            error=str(error),
        )
        outcome.fail("Google Sheets setup", str(error))
        return 1
    log_event(
        "stage_completed",
        stage="Google Sheets setup",
        elapsed_seconds=elapsed_seconds(sheet_setup_started_at),
        sheet_id=sheet_updater.sheet_id,
        gid=sheet_updater.gid,
        sheet_title=sheet_updater.sheet_title,
    )

    scrape_started_at = time.monotonic()
    try:
        log_event(
            "stage_started",
            stage="Scrape keywords",
            label="Scrape keywords",
            keyword_count=len(selected_keywords),
            scheduler_run_id=scheduler_run_id,
            global_winner_limit=global_winner_limit,
        )
        scrape_summary = product_scraper.scrape_keywords(
            selected_keywords,
            keyword_store=keyword_store,
            sheet_updater=sheet_updater,
            global_winner_limit=global_winner_limit,
            scheduler_run_id=scheduler_run_id,
            scraper_config=scraper_config,
        )
    except Exception as error:
        print(f"[!] Scraper stage failed: {error}")
        traceback.print_exc()
        log_event(
            "stage_failed",
            stage="Scrape keywords",
            level="ERROR",
            elapsed_seconds=elapsed_seconds(scrape_started_at),
            error_type=type(error).__name__,
            error=str(error),
            traceback=traceback.format_exc(),
        )
        outcome.fail("Scrape keywords", str(error))
        return 1

    log_event(
        "stage_completed",
        stage="Scrape keywords",
        elapsed_seconds=elapsed_seconds(scrape_started_at),
        **summarize_stage_result(scrape_summary),
    )
    if scrape_summary["errors"]:
        print("[!] Scraper reported errors. Skipping sync, normalization, stats refresh, and generation.")
        log_event(
            "scraper_reported_errors",
            stage="Scrape keywords",
            level="ERROR",
            errors=scrape_summary.get("errors"),
            stopped_reason=scrape_summary.get("stopped_reason"),
        )
        outcome.fail("Scrape keywords", summarize_scrape_errors(scrape_summary))
        return 1

    if scrape_summary["keywords_attempted"] == 0:
        print("[PIPELINE] No keywords were scraped. Skipping sync and normalization.")
        log_event(
            "pipeline_no_keywords_scraped",
            stage="orchestrator",
            scheduler_run_id=scheduler_run_id,
            stopped_reason=scrape_summary.get("stopped_reason"),
        )
        return finish_without_scrape(common_kwargs, outcome, scheduler_run_id, scrape_summary)

    if not run_main_stage("Sync products from Google Sheets", sync_products.main, outcome):
        return 1

    if not run_main_stage("Normalize latest product CSV", product_normalizer.main, outcome):
        return 1

    ok, post_stats = run_optional_module_stage(
        keyword_stats,
        "Refresh keyword stats after normalization",
        KEYWORD_STATS_HOOK,
        outcome,
        scrape_summary=scrape_summary,
        scheduler_run_id=scheduler_run_id,
        **common_kwargs,
    )
    if not ok:
        return 1

    ok, _ = run_optional_module_stage(
        product_review_agent,
        "Run Rover product review",
        PRODUCT_REVIEW_AGENT_HOOK,
        outcome,
        scrape_summary=scrape_summary,
        keyword_stats=post_stats,
        scheduler_run_id=scheduler_run_id,
        normalization_completed=True,
        **common_kwargs,
    )
    if not ok:
        return 1

    ok, _ = run_optional_module_stage(
        email_report,
        "Send email report",
        EMAIL_REPORT_HOOK,
        outcome,
        scrape_summary=scrape_summary,
        scheduler_run_id=scheduler_run_id,
        normalization_completed=True,
        **common_kwargs,
    )
    if not ok:
        return 1

    ok, _ = run_optional_module_stage(
        keyword_generator,
        "Generate new keywords",
        KEYWORD_GENERATOR_HOOK,
        outcome,
        scrape_summary=scrape_summary,
        keyword_stats=post_stats,
        scheduler_run_id=scheduler_run_id,
        **common_kwargs,
    )
    if not ok:
        return 1

    print("[PIPELINE] Complete.")
    log_event("pipeline_completed", stage="orchestrator")
    return 0


def finish_without_scrape(
    common_kwargs,
    outcome: RunOutcome,
    scheduler_run_id=None,
    scrape_summary=None,
) -> int:
    ok, post_stats = run_optional_module_stage(
        keyword_stats,
        "Refresh keyword stats after skipped scrape",
        KEYWORD_STATS_HOOK,
        outcome,
        scrape_summary=scrape_summary,
        scheduler_run_id=scheduler_run_id,
        **common_kwargs,
    )
    if not ok:
        return 1

    ok, _ = run_optional_module_stage(
        email_report,
        "Send email report",
        EMAIL_REPORT_HOOK,
        outcome,
        scrape_summary=scrape_summary,
        scheduler_run_id=scheduler_run_id,
        normalization_completed=False,
        **common_kwargs,
    )
    if not ok:
        return 1

    ok, _ = run_optional_module_stage(
        keyword_generator,
        "Generate new keywords",
        KEYWORD_GENERATOR_HOOK,
        outcome,
        scrape_summary=scrape_summary,
        keyword_stats=post_stats,
        scheduler_run_id=scheduler_run_id,
        **common_kwargs,
    )
    return 0 if ok else 1


def run_main_stage(label, func, outcome: RunOutcome) -> bool:
    print(f"[PIPELINE] {label}...")
    started_at = time.monotonic()
    log_event(
        "stage_started",
        stage=label,
        label=label,
        callable=getattr(func, "__name__", repr(func)),
    )

    try:
        result = func()
    except Exception as error:
        print(f"[!] {label} failed: {error}")
        traceback.print_exc()
        log_event(
            "stage_failed",
            stage=label,
            level="ERROR",
            elapsed_seconds=elapsed_seconds(started_at),
            error_type=type(error).__name__,
            error=str(error),
            traceback=traceback.format_exc(),
        )
        outcome.fail(label, str(error))
        return False

    if isinstance(result, int) and result != 0:
        print(f"[!] {label} returned exit code {result}.")
        log_event(
            "stage_failed",
            stage=label,
            level="ERROR",
            elapsed_seconds=elapsed_seconds(started_at),
            exit_code=result,
        )
        outcome.fail(label, f"returned exit code {result}")
        return False

    log_event(
        "stage_completed",
        stage=label,
        elapsed_seconds=elapsed_seconds(started_at),
        **summarize_stage_result(result),
    )
    return True


def run_optional_module_stage(module, label, hook_name, outcome: RunOutcome, **kwargs):
    module_name = module.__name__.rsplit(".", 1)[-1]
    hook = getattr(module, hook_name)
    if not callable(hook):
        print(f"[!] {label}: {module.__name__} has non-callable hook: {hook_name}")
        log_event(
            "stage_failed",
            stage=label,
            level="ERROR",
            module_name=module_name,
            hook_name=hook_name,
            reason="non_callable_hook",
        )
        outcome.fail(label, f"non-callable hook: {hook_name}")
        return False, None

    print(f"[PIPELINE] {label}...")
    started_at = time.monotonic()
    log_event(
        "stage_started",
        stage=label,
        label=label,
        module_name=module_name,
        hook=getattr(hook, "__name__", repr(hook)),
    )
    try:
        result = hook(**kwargs)
    except Exception as error:
        print(f"[!] {label} failed: {error}")
        traceback.print_exc()
        log_event(
            "stage_failed",
            stage=label,
            level="ERROR",
            elapsed_seconds=elapsed_seconds(started_at),
            module_name=module_name,
            hook=getattr(hook, "__name__", repr(hook)),
            error_type=type(error).__name__,
            error=str(error),
            traceback=traceback.format_exc(),
        )
        outcome.fail(label, str(error))
        return False, None

    if isinstance(result, int) and result != 0:
        print(f"[!] {label} returned exit code {result}.")
        log_event(
            "stage_failed",
            stage=label,
            level="ERROR",
            elapsed_seconds=elapsed_seconds(started_at),
            module_name=module_name,
            hook=getattr(hook, "__name__", repr(hook)),
            exit_code=result,
        )
        outcome.fail(label, f"returned exit code {result}")
        return False, result

    log_event(
        "stage_completed",
        stage=label,
        elapsed_seconds=elapsed_seconds(started_at),
        module_name=module_name,
        hook=getattr(hook, "__name__", repr(hook)),
        **summarize_stage_result(result),
    )
    return True, result


def selected_keyword_names(selected_keywords, limit: int = 25) -> list[str]:
    names = []
    for selected_keyword in list(selected_keywords)[:limit]:
        keyword = product_scraper.selected_keyword_text(selected_keyword)
        if keyword:
            names.append(keyword)
        else:
            names.append(short_value(repr(selected_keyword), 120))
    return names


def summarize_stage_result(result) -> dict:
    if result is None:
        return {"result_type": "none"}

    if isinstance(result, int):
        return {"result_type": "exit_code", "exit_code": result}

    summary = {"result_type": "dict"}
    copied_keys = (
        "db_path",
        "scheduler_run_id",
        "generation_run_id",
        "keywords_requested",
        "keywords_attempted",
        "keywords_skipped",
        "keywords_completed",
        "products_seen",
        "products_exported",
        "total_winners_found",
        "sheet_rows_updated",
        "duplicate_asins_skipped",
        "maxed_out_keywords",
        "stopped_reason",
        "keywords_refreshed",
        "retired_keywords",
        "cooldown_keywords",
        "source_keyword_count",
        "source_winner_count",
        "candidates_generated",
        "candidates_accepted",
        "candidates_inserted",
        "enabled_engines",
        "disabled_engines",
    )
    for key in copied_keys:
        if key in result:
            summary[key] = result[key]

    for key in ("errors", "keyword_results", "products", "top_keywords"):
        value = result.get(key)
        if value is not None:
            summary[f"{key}_count"] = len(value)
    return summary


def short_value(value, limit: int) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def summarize_scrape_errors(scrape_summary) -> str:
    errors = scrape_summary.get("errors") or []
    parts = [f"{item.get('keyword')}: {item.get('error')}" for item in errors]
    return "Scraper reported errors. " + "; ".join(parts) if parts else "Scraper reported errors."


if __name__ == "__main__":
    raise SystemExit(main())
