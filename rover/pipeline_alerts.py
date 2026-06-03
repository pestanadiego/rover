import html
import os
import socket
import sys
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from rover.common import env_bool, int_value, utc_now_iso
from rover.reports.config import EmailReportConfig, load_email_report_config
from rover.reports.sender import build_message, deliver_message


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALERT_SUBJECT_PREFIX = "[Rover]"
MAX_DETAIL_CHARS = 3000
DEFAULT_ALERT_LOG_TAIL_LINES = 20
ALERT_LOG_TAIL_LINES_ENV_VAR = "PIPELINE_ALERT_LOG_TAIL_LINES"
PIPELINE_LOG_GLOB = "pipeline_*.log"

SenderFn = Callable[[Any, EmailReportConfig], None]


@dataclass
class RunOutcome:
    """Mutable per-run state used to decide whether (and what) to alert."""

    failed_stage: str | None = None
    failed_detail: str | None = None
    exception: str | None = None
    skipped: bool = False
    skipped_detail: str | None = None
    report_email_enabled: bool = True
    report_email_sent: bool = False
    report_email_detail: str | None = None
    lock_run_id: int | None = None
    reclaimed_info: dict[str, Any] | None = None

    def fail(self, stage: str, detail: str | None = None) -> None:
        """Record the first stage that failed (later failures are downstream noise)."""
        if self.failed_stage is None:
            self.failed_stage = stage
            self.failed_detail = detail

    def note_report_email(
        self,
        sent: bool,
        detail: str | None = None,
        enabled: bool = True,
    ) -> None:
        self.report_email_enabled = enabled
        self.report_email_sent = sent
        self.report_email_detail = detail


def process_pipeline_alert(
    outcome: RunOutcome,
    exit_code: int,
    *,
    sender: SenderFn | None = None,
    status_log_path: Path | None = None,
    config: EmailReportConfig | None = None,
) -> dict[str, Any]:
    """Decide whether to alert, always log a breadcrumb, never raise."""
    status_log_path = status_log_path or default_status_log_path()

    try:
        kind = classify(outcome, exit_code)
        write_status_log(status_log_path, kind, outcome, exit_code)

        if not should_alert(kind, outcome):
            return {"alerted": False, "kind": kind}

        if not alerts_enabled():
            return {"alerted": False, "kind": kind, "reason": "alerts_disabled"}

        config = config or load_email_report_config()
        log_tail = read_alert_log_tail(status_log_path.parent)
        subject, body = build_alert(kind, outcome, exit_code, log_tail=log_tail)
        result = send_alert(subject, body, config, sender=sender)
        result["kind"] = kind
        return result
    except Exception as error:  # alerting must never break the pipeline
        print(f"[pipeline-alert] failed to process alert: {error}", file=sys.stderr)
        try:
            write_status_log(status_log_path, "alert_error", outcome, exit_code, extra=str(error))
        except Exception:
            pass
        return {"alerted": False, "error": str(error)}


def classify(outcome: RunOutcome, exit_code: int) -> str:
    if outcome.exception:
        return "exception"
    if outcome.skipped:
        return "skipped"
    if exit_code != 0 or outcome.failed_stage:
        return "failed"
    if outcome.report_email_enabled and not outcome.report_email_sent:
        return "no_report_email"
    return "ok"


def should_alert(kind: str, outcome: RunOutcome) -> bool:
    if kind in {"exception", "failed", "no_report_email"}:
        return True
    if kind == "skipped":
        return alert_on_skip()
    if kind == "ok":
        # Success, but flag a reclaim once: a prior run died without releasing its lock.
        return outcome.reclaimed_info is not None
    return True


def build_alert(
    kind: str,
    outcome: RunOutcome,
    exit_code: int,
    log_tail: list[str] | None = None,
) -> tuple[str, str]:
    now = utc_now_iso()
    subject = f"{ALERT_SUBJECT_PREFIX} {subject_for_kind(kind, outcome)}"
    body = "\n".join(body_lines(kind, outcome, exit_code, now, log_tail or []))
    return subject, body


def subject_for_kind(kind: str, outcome: RunOutcome) -> str:
    if kind in {"failed", "exception"}:
        return f"pipeline FAILED at {outcome.failed_stage or 'pipeline'}"
    if kind == "no_report_email":
        return "pipeline finished but NO REPORT EMAIL was sent"
    if kind == "skipped":
        return "pipeline run SKIPPED (already running)"
    if kind == "ok":
        return "previous run was reclaimed"
    return "pipeline alert"


def body_lines(
    kind: str,
    outcome: RunOutcome,
    exit_code: int,
    now: str,
    log_tail: list[str],
) -> list[str]:
    lines = [
        f"Status: {kind}",
        f"Exit code: {exit_code}",
    ]

    if outcome.lock_run_id is not None:
        lines.append(f"Pipeline run id: {outcome.lock_run_id}")

    if outcome.failed_stage:
        lines.append(f"Failed stage: {outcome.failed_stage}")

    detail = outcome.exception or outcome.failed_detail or outcome.skipped_detail
    if detail:
        lines.append("")
        lines.append("Detail:")
        lines.append(truncate(detail))

    if kind == "no_report_email":
        lines.append("")
        lines.append(
            "The pipeline completed but the report email did not go out "
            f"(reason: {outcome.report_email_detail or 'unknown'}). "
            "Check SMTP settings — the deliverable was not sent."
        )

    if outcome.reclaimed_info:
        info = outcome.reclaimed_info
        lines.append("")
        lines.append(
            "Note: reclaimed {count} stale lock(s) (run id(s) {ids}) on start; "
            "last heartbeat {hb}. A previous run died without releasing its lock.".format(
                count=info.get("count"),
                ids=info.get("run_ids"),
                hb=info.get("last_heartbeat_at_utc"),
            )
        )

    if log_tail:
        lines.append("")
        lines.extend(log_tail)

    return lines


def send_alert(
    subject: str,
    body: str,
    config: EmailReportConfig,
    *,
    sender: SenderFn | None = None,
) -> dict[str, Any]:
    recipients = alert_recipients(config)
    if not recipients:
        return {"alerted": False, "reason": "no_recipients"}

    if not config.smtp_host or not config.email_from:
        return {"alerted": False, "reason": "smtp_not_configured"}

    alert_config = replace(config, email_to=recipients)
    message = build_message(subject, body, html_body(body), alert_config)
    send = sender or deliver_message
    send(message, alert_config)
    return {"alerted": True, "recipients": list(recipients)}


def html_body(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"


def alert_recipients(config: EmailReportConfig) -> tuple[str, ...]:
    raw = os.getenv("ALERT_EMAIL_TO")
    if raw:
        return tuple(part.strip() for part in raw.split(",") if part.strip())
    return tuple(config.email_to or ())


def alerts_enabled() -> bool:
    return env_bool("PIPELINE_ALERT_ENABLED", default=True)


def alert_on_skip() -> bool:
    return env_bool("PIPELINE_ALERT_ON_SKIP", default=False)


def default_status_log_path(project_root: Path = PROJECT_ROOT) -> Path:
    raw_log_dir = (os.getenv("PIPELINE_LOG_DIR") or "logs").strip() or "logs"
    log_dir = Path(raw_log_dir).expanduser()
    if not log_dir.is_absolute():
        log_dir = project_root / log_dir
    return log_dir / "pipeline_status.log"


def write_status_log(
    status_log_path: Path,
    kind: str,
    outcome: RunOutcome,
    exit_code: int,
    extra: str | None = None,
) -> None:
    status_log_path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        utc_now_iso(),
        kind,
        f"exit={exit_code}",
        f"run_id={outcome.lock_run_id}",
        f"stage={outcome.failed_stage or '-'}",
        f"report_email_sent={outcome.report_email_sent}",
        f"reclaimed={bool(outcome.reclaimed_info)}",
    ]
    if extra:
        fields.append(extra.replace("\n", " ").replace("\t", " "))

    with status_log_path.open("a", encoding="utf-8") as log_file:
        log_file.write("\t".join(fields) + "\n")


def read_alert_log_tail(log_dir: Path) -> list[str]:
    line_count = int_value(os.getenv(ALERT_LOG_TAIL_LINES_ENV_VAR), DEFAULT_ALERT_LOG_TAIL_LINES)
    if line_count < 1:
        return []

    try:
        log_path = latest_pipeline_log_path(log_dir)
        lines = tail_file(log_path, line_count)
    except Exception as error:
        return [
            f"Last {line_count} log lines:",
            f"Could not read pipeline log: {error}",
        ]

    if not lines:
        return [
            f"Last {line_count} log lines ({log_path.name}):",
            "Pipeline log was empty.",
        ]

    return [f"Last {line_count} log lines ({log_path.name}):", *lines]


def latest_pipeline_log_path(log_dir: Path) -> Path:
    candidates = [
        path
        for path in log_dir.glob(PIPELINE_LOG_GLOB)
        if path.name != "pipeline_status.log" and not path.name.startswith("pipeline_events_")
    ]
    if not candidates:
        raise FileNotFoundError(f"no {PIPELINE_LOG_GLOB} files found in {log_dir}")

    return max(candidates, key=lambda path: path.stat().st_mtime)


def tail_file(path: Path, line_count: int) -> list[str]:
    with path.open("r", encoding="utf-8", errors="replace") as file:
        return [line.rstrip("\n") for line in deque(file, maxlen=line_count)]


def truncate(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
