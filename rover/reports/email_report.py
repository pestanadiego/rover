import time
import traceback
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from rover.common import elapsed_seconds
from rover.reports.config import load_email_report_config
from rover.reports.data import build_latest_email_report
from rover.reports.renderer import render_report_html, render_report_text
from rover.reports.sender import send_email_report
from rover.pipeline_logging import configure_pipeline_logging, log_event


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOTENV_PATH = PROJECT_ROOT / ".env"


def main() -> int:
    configure_pipeline_logging(PROJECT_ROOT)
    return send_latest_report()


def send_latest_report(
    db_path: Path | None = None,
    scheduler_run_id: int | None = None,
    scrape_summary: dict[str, Any] | None = None,
    normalization_completed: bool = True,
    run_outcome: Any = None,
    **_kwargs: Any,
) -> int:
    started_at = time.monotonic()
    load_dotenv(DOTENV_PATH)
    config = load_email_report_config()
    log_event(
        "email_report_stage_started",
        stage="Send email report",
        enabled=config.enabled,
        required=config.required,
        max_products=config.max_products,
        scheduler_run_id=scheduler_run_id,
        normalization_completed=normalization_completed,
    )

    if not config.enabled:
        print("[SKIP] Email report disabled.")
        record_report_outcome(run_outcome, sent=False, detail="Email report disabled.", enabled=False)
        log_event(
            "email_report_skipped",
            stage="Send email report",
            level="WARNING",
            elapsed_seconds=elapsed_seconds(started_at),
            reason="disabled",
        )
        return 0

    try:
        report_db_path = Path(db_path) if db_path else config.db_path
        log_event(
            "email_report_build_started",
            stage="Send email report",
            db_path=report_db_path,
            max_products=config.max_products,
        )
        report = build_latest_email_report(
            db_path=report_db_path,
            max_products=config.max_products,
            scheduler_run_id=scheduler_run_id,
            scrape_summary=scrape_summary,
            normalization_completed=normalization_completed,
        )
        log_event(
            "email_report_built",
            stage="Send email report",
            **report_log_summary(report),
        )
        subject = f"{int(report.get('product_count') or 0)} Potential Products Found 👀"
        html_body = render_report_html(report, config)
        text_body = render_report_text(report, config)
        log_event(
            "email_report_rendered",
            stage="Send email report",
            subject=subject,
            text_chars=len(text_body),
            html_chars=len(html_body),
            recipient_count=len(config.email_to),
            smtp_host_configured=bool(config.smtp_host),
        )
        result = send_email_report(subject, text_body, html_body, config)
    except Exception as error:
        record_report_outcome(run_outcome, sent=False, detail=str(error), enabled=True)
        log_event(
            "email_report_failed",
            stage="Send email report",
            level="ERROR",
            elapsed_seconds=elapsed_seconds(started_at),
            error_type=type(error).__name__,
            error=str(error),
            traceback=traceback.format_exc(),
        )
        return handle_report_error(error, config.required)

    record_report_outcome(
        run_outcome,
        sent=bool(result.get("sent")),
        detail=result.get("message"),
        enabled=True,
    )
    exit_code = handle_send_result(result, config.required)
    log_event(
        "email_report_stage_completed",
        stage="Send email report",
        elapsed_seconds=elapsed_seconds(started_at),
        exit_code=exit_code,
        sent=bool(result.get("sent")),
        skipped=bool(result.get("skipped")),
        message=result.get("message"),
    )
    return exit_code


def record_report_outcome(run_outcome: Any, sent: bool, detail: str | None, enabled: bool) -> None:
    """Annotate the shared RunOutcome if one was passed (duck-typed, no hard dependency)."""
    if run_outcome is None:
        return

    run_outcome.note_report_email(sent=sent, detail=detail, enabled=enabled)


def handle_send_result(result: dict[str, Any], required: bool) -> int:
    message = result.get("message", "Email report finished.")

    if result.get("sent"):
        print(f"[EMAIL] {message}")
        return 0

    if result.get("skipped") and not required:
        print(f"[SKIP] Email report not sent. {message}")
        return 0

    print(f"[!] Email report not sent. {message}")
    return 1


def handle_report_error(error: Exception, required: bool) -> int:
    if required:
        print(f"[!] Email report failed: {error}")
        traceback.print_exc()
        return 1

    print(f"[!] Email report failed, pipeline will continue: {error}")
    return 0


def report_log_summary(report: dict[str, Any]) -> dict[str, Any]:
    normalization_run = report.get("normalization_run") or {}
    scheduler_run = report.get("scheduler_run") or {}
    agent_review_run = report.get("agent_review_run") or {}
    return {
        "db_path": report.get("db_path"),
        "warning": report.get("warning"),
        "normalization_run_id": normalization_run.get("id"),
        "scheduler_run_id": scheduler_run.get("id"),
        "report_start_utc": report.get("report_start_utc"),
        "product_count": report.get("product_count"),
        "displayed_product_count": report.get("displayed_product_count"),
        "decision_counts": report.get("decision_counts"),
        "top_keyword_count": len(report.get("top_keywords") or []),
        "generation_summary_count": len(report.get("generation_summary") or []),
        "cooldown_keyword_count": len(report.get("cooldown_keywords") or []),
        "agent_review_run_id": agent_review_run.get("id"),
        "agent_breakdown_chars": len(report.get("agent_breakdown") or ""),
    }


if __name__ == "__main__":
    raise SystemExit(main())
