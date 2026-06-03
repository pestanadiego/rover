from dataclasses import dataclass
from pathlib import Path

from rover.common import (
    clean_mapping,
    clean_text,
    env_csv,
    env_text,
    parse_bool,
    positive_int,
    read_yaml_mapping,
    resolve_project_path,
)
from rover.data_paths import default_db_path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "email_report.yaml"
DEFAULT_DB_PATH = default_db_path()


@dataclass(frozen=True)
class EmailReportConfig:
    enabled: bool
    required: bool
    db_path: Path
    max_products: int
    note_max_chars: int
    agent_name: str
    recipient_name: str
    subject_prefix: str
    display_timezone: str
    smtp_host: str | None
    smtp_port: int
    smtp_username: str | None
    smtp_password: str | None
    smtp_use_tls: bool
    smtp_use_ssl: bool
    email_from: str | None
    email_to: tuple[str, ...]
    email_reply_to: str | None


def load_email_report_config(path: Path = DEFAULT_CONFIG_PATH) -> EmailReportConfig:
    data = read_yaml_mapping(path, description="Email report config")
    database = clean_mapping(data.get("database"))
    report = clean_mapping(data.get("report"))
    delivery = clean_mapping(data.get("delivery"))

    return EmailReportConfig(
        enabled=parse_bool(data.get("enabled"), default=True),
        required=parse_bool(data.get("required"), default=False),
        db_path=resolve_project_path(database.get("path"), default=DEFAULT_DB_PATH),
        max_products=positive_int(report.get("max_products"), default=25),
        note_max_chars=positive_int(report.get("agent_note_max_chars"), default=240),
        agent_name=clean_text(report.get("agent_name"), "Rover"),
        recipient_name=clean_text(report.get("recipient_name"), "there"),
        subject_prefix=clean_text(report.get("subject_prefix"), "SellerAmp"),
        display_timezone=clean_text(report.get("display_timezone"), "America/New_York"),
        smtp_host=env_text("SMTP_HOST"),
        smtp_port=positive_int(delivery.get("smtp_port"), default=587),
        smtp_username=env_text("SMTP_USERNAME"),
        smtp_password=env_text("SMTP_PASSWORD"),
        smtp_use_tls=parse_bool(delivery.get("smtp_use_tls"), default=True),
        smtp_use_ssl=parse_bool(delivery.get("smtp_use_ssl"), default=False),
        email_from=env_text("EMAIL_FROM"),
        email_to=env_csv("EMAIL_TO"),
        email_reply_to=env_text("EMAIL_REPLY_TO"),
    )
