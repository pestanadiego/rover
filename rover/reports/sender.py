import smtplib
import time
from email.message import EmailMessage
from typing import Any

from rover.common import elapsed_seconds, ssl_context
from rover.reports.config import EmailReportConfig
from rover.pipeline_logging import log_event


def send_email_report(
    subject: str,
    text_body: str,
    html_body: str,
    config: EmailReportConfig,
) -> dict[str, Any]:
    started_at = time.monotonic()
    missing = missing_delivery_settings(config)
    if missing:
        log_event(
            "email_delivery_skipped",
            stage="Send email report",
            level="WARNING",
            missing_settings=missing,
            recipient_count=len(config.email_to),
        )
        return {
            "sent": False,
            "skipped": True,
            "message": f"Missing email settings: {', '.join(missing)}",
        }

    message = build_message(subject, text_body, html_body, config)
    log_event(
        "email_delivery_started",
        stage="Send email report",
        recipient_count=len(config.email_to),
        smtp_host_configured=bool(config.smtp_host),
        smtp_port=config.smtp_port,
        smtp_use_tls=config.smtp_use_tls,
        smtp_use_ssl=config.smtp_use_ssl,
    )
    deliver_message(message, config)
    log_event(
        "email_delivery_completed",
        stage="Send email report",
        elapsed_seconds=elapsed_seconds(started_at),
        recipient_count=len(config.email_to),
    )

    return {
        "sent": True,
        "skipped": False,
        "message": f"Email sent to {len(config.email_to)} recipient(s).",
    }


def missing_delivery_settings(config: EmailReportConfig) -> list[str]:
    missing = []

    if not config.smtp_host:
        missing.append("SMTP_HOST")

    if not config.email_from:
        missing.append("EMAIL_FROM")

    if not config.email_to:
        missing.append("EMAIL_TO")

    if config.smtp_username and not config.smtp_password:
        missing.append("SMTP_PASSWORD")

    return missing


def build_message(
    subject: str,
    text_body: str,
    html_body: str,
    config: EmailReportConfig,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.email_from
    message["To"] = ", ".join(config.email_to)

    if config.email_reply_to:
        message["Reply-To"] = config.email_reply_to

    message.set_content(text_body)
    message.add_alternative(html_body, subtype="html")
    return message


def deliver_message(message: EmailMessage, config: EmailReportConfig) -> None:
    started_at = time.monotonic()
    context = ssl_context()

    if config.smtp_use_ssl:
        with smtplib.SMTP_SSL(
            config.smtp_host,
            config.smtp_port,
            context=context,
        ) as smtp:
            login_if_needed(smtp, config)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(config.smtp_host, config.smtp_port) as smtp:
            smtp.ehlo()
            if config.smtp_use_tls:
                smtp.starttls(context=context)
                smtp.ehlo()

            if config.smtp_username:
                smtp.login(config.smtp_username, config.smtp_password or "")

            smtp.send_message(message)

    log_event(
        "smtp_message_sent",
        stage="Send email report",
        elapsed_seconds=elapsed_seconds(started_at),
        smtp_use_ssl=config.smtp_use_ssl,
        smtp_use_tls=config.smtp_use_tls,
        authenticated=bool(config.smtp_username),
    )
