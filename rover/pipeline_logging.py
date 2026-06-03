import contextlib
import contextvars
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Iterator

from rover.common import env_bool, utc_now_iso, utc_timestamp_for_filename


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PIPELINE_LOGGER_NAME = "pipeline"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_DIR = "logs"

PIPELINE_RUN_ID = contextvars.ContextVar("pipeline_run_id", default="-")
PIPELINE_STAGE = contextvars.ContextVar("pipeline_stage", default="-")
JSONL_LOCK = threading.Lock()
LOGGER_STATE = {
    "configured": False,
    "started_at_token": None,
    "log_path": None,
    "jsonl_path": None,
    "level": DEFAULT_LOG_LEVEL,
    "original_stdout": sys.stdout,
    "original_stderr": sys.stderr,
    "stdout_tee": None,
    "stderr_tee": None,
}

SECRET_FIELD_PATTERN = re.compile(
    r"(api[_-]?key|authorization|bearer|cookie|credential|password|secret|service[_-]?account|sheet[_-]?url|token)",
    re.IGNORECASE,
)
VALUE_REDACTION_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|password|secret)=([^&\s]+)"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)([a-z0-9._~+/=-]+)"),
    re.compile(r"https://docs\.google\.com/spreadsheets/[^\s]+"),
)


class PipelineContextFilter(logging.Filter):
    def filter(self, record: Any) -> bool:
        record.pipeline_run_id = PIPELINE_RUN_ID.get()
        record.pipeline_stage = PIPELINE_STAGE.get()
        return True


class UTCFormatter(logging.Formatter):
    converter = time.gmtime

    def formatTime(self, record: Any, datefmt: str | None = None) -> str:
        return time.strftime(datefmt or "%Y-%m-%dT%H:%M:%SZ", self.converter(record.created))


class TeeStream:
    """Write normal print output to the terminal and the pipeline text log."""

    def __init__(self, original_stream: Any, log_path: Path):
        self.original_stream = original_stream
        self.log_file = log_path.open("a", encoding="utf-8", buffering=1)

    def write(self, text: str) -> int:
        written = self.original_stream.write(text)
        if text:
            self.log_file.write(text)
        return written

    def flush(self) -> None:
        self.original_stream.flush()
        self.log_file.flush()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.log_file.close()

    def isatty(self) -> bool:
        return bool(getattr(self.original_stream, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self.original_stream.fileno()

    @property
    def encoding(self) -> str | None:
        return getattr(self.original_stream, "encoding", None)


def configure_pipeline_logging(
    project_root: Path | str = PROJECT_ROOT,
    run_id: int | str | None = None,
    *,
    log_dir: Path | str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Configure console logging, file logging, JSONL events, and print capture."""
    if run_id is not None:
        set_pipeline_run_context(run_id)

    if LOGGER_STATE["configured"] and not force:
        return dict(LOGGER_STATE)

    project_root = Path(project_root)
    log_dir_path = Path(log_dir or os.getenv("PIPELINE_LOG_DIR", DEFAULT_LOG_DIR))
    if not log_dir_path.is_absolute():
        log_dir_path = project_root / log_dir_path
    log_dir_path.mkdir(parents=True, exist_ok=True)

    started_at_token = LOGGER_STATE["started_at_token"] or utc_timestamp_for_filename()
    log_path = log_dir_path / f"pipeline_{started_at_token}.log"
    jsonl_path = (
        log_dir_path / f"pipeline_events_{started_at_token}.jsonl"
        if env_bool("PIPELINE_LOG_JSON", True)
        else None
    )
    level_name, level_value = parse_log_level(os.getenv("PIPELINE_LOG_LEVEL", DEFAULT_LOG_LEVEL))

    logger = logging.getLogger(PIPELINE_LOGGER_NAME)
    remove_pipeline_handlers(logger)

    formatter = UTCFormatter(
        "%(asctime)s %(levelname)s [%(pipeline_stage)s] "
        "run=%(pipeline_run_id)s %(name)s: %(message)s"
    )
    context_filter = PipelineContextFilter()

    console_handler = logging.StreamHandler(LOGGER_STATE["original_stderr"])
    console_handler.setLevel(level_value)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(context_filter)
    console_handler.pipeline_handler = True

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level_value)
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context_filter)
    file_handler.pipeline_handler = True

    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    LOGGER_STATE.update(
        {
            "configured": True,
            "started_at_token": started_at_token,
            "log_path": log_path,
            "jsonl_path": jsonl_path,
            "level": level_name,
        }
    )
    install_print_tee(log_path)
    return dict(LOGGER_STATE)


def get_pipeline_logger(stage: str | None = None):
    if not LOGGER_STATE["configured"]:
        configure_pipeline_logging()

    if not stage:
        return logging.getLogger(PIPELINE_LOGGER_NAME)

    logger_name = f"{PIPELINE_LOGGER_NAME}.{stage_slug(stage)}"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.NOTSET)
    return logger


def set_pipeline_run_context(run_id: int | str | None) -> None:
    PIPELINE_RUN_ID.set(str(run_id) if run_id is not None else "-")


@contextlib.contextmanager
def stage_context(stage: str) -> Iterator[None]:
    token = PIPELINE_STAGE.set(stage)
    try:
        yield
    finally:
        PIPELINE_STAGE.reset(token)


def log_event(
    event: str,
    *,
    stage: str | None = None,
    level: int | str = logging.INFO,
    logger: Any | None = None,
    **fields: Any,
) -> None:
    if not LOGGER_STATE["configured"]:
        configure_pipeline_logging()

    level_name, level_value = parse_log_level(level)
    safe_fields = redact_fields(fields)
    message = format_event_message(event, safe_fields)
    selected_logger = logger or get_pipeline_logger(stage)

    if stage:
        with stage_context(stage):
            selected_logger.log(level_value, message)
            write_json_event(event, level_name, stage, safe_fields)
    else:
        selected_logger.log(level_value, message)
        write_json_event(event, level_name, PIPELINE_STAGE.get(), safe_fields)


def redact_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return {str(key): redact_value(value, str(key)) for key, value in fields.items()}


def install_print_tee(log_path: Path) -> None:
    restore_print_streams()
    stdout_tee = TeeStream(LOGGER_STATE["original_stdout"], log_path)
    stderr_tee = TeeStream(LOGGER_STATE["original_stderr"], log_path)
    sys.stdout = stdout_tee
    sys.stderr = stderr_tee
    LOGGER_STATE["stdout_tee"] = stdout_tee
    LOGGER_STATE["stderr_tee"] = stderr_tee


def restore_print_streams() -> None:
    if LOGGER_STATE.get("stdout_tee") is not None:
        sys.stdout = LOGGER_STATE["original_stdout"]
        LOGGER_STATE["stdout_tee"].close()
        LOGGER_STATE["stdout_tee"] = None

    if LOGGER_STATE.get("stderr_tee") is not None:
        sys.stderr = LOGGER_STATE["original_stderr"]
        LOGGER_STATE["stderr_tee"].close()
        LOGGER_STATE["stderr_tee"] = None


def remove_pipeline_handlers(logger: Any) -> None:
    for handler in list(logger.handlers):
        if not getattr(handler, "pipeline_handler", False):
            continue
        logger.removeHandler(handler)
        with contextlib.suppress(Exception):
            handler.close()


def parse_log_level(raw_level: int | str | None) -> tuple[str, int]:
    if isinstance(raw_level, int):
        return logging.getLevelName(raw_level), raw_level

    level_name = str(raw_level or DEFAULT_LOG_LEVEL).strip().upper()
    if level_name.isdigit():
        level_value = int(level_name)
        return logging.getLevelName(level_value), level_value

    level_value = getattr(logging, level_name, logging.INFO)
    if not isinstance(level_value, int):
        return DEFAULT_LOG_LEVEL, logging.INFO
    return level_name, level_value


def format_event_message(event: str, fields: dict[str, Any]) -> str:
    parts = [f"event={event}"]
    for key in sorted(fields):
        parts.append(f"{key}={format_message_value(fields[key])}")
    return " ".join(parts)


def format_message_value(value: Any) -> str:
    value = json_safe(value)
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    text = str(value)
    if not text or any(character.isspace() for character in text):
        return json.dumps(text)
    return text


def write_json_event(event: str, level_name: str, stage: str, fields: dict[str, Any]) -> None:
    jsonl_path = LOGGER_STATE.get("jsonl_path")
    if not jsonl_path:
        return

    payload = {
        "time_utc": utc_now_iso(),
        "level": level_name,
        "event": event,
        "stage": stage,
        "pipeline_run_id": PIPELINE_RUN_ID.get(),
    }
    payload.update({key: json_safe(value) for key, value in fields.items()})

    with JSONL_LOCK:
        with Path(jsonl_path).open("a", encoding="utf-8") as file:
            file.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def redact_value(value: Any, key: str | None = None) -> Any:
    if key and SECRET_FIELD_PATTERN.search(key):
        return "[REDACTED]"

    if isinstance(value, dict):
        return {str(item_key): redact_value(item_value, str(item_key)) for item_key, item_value in value.items()}

    if isinstance(value, (list, tuple, set)):
        return [redact_value(item, key) for item in value]

    if isinstance(value, Path):
        return str(value)

    if isinstance(value, str):
        return redact_text(value)

    return value


def redact_text(text: str) -> str:
    redacted = text
    for pattern in VALUE_REDACTION_PATTERNS:
        if pattern.pattern.startswith("https://docs"):
            redacted = pattern.sub("[REDACTED_URL]", redacted)
        elif pattern.pattern.startswith("(?i)(authorization"):
            redacted = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", redacted)
        else:
            redacted = pattern.sub(lambda match: f"{match.group(1)}=[REDACTED]", redacted)
    return redacted


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def stage_slug(stage: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", stage.strip().lower()).strip("_")
    return slug or "unknown"
