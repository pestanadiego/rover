import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from rover.common import int_value, utc_now_iso
from rover.db import ensure_database_schema
from rover.data_paths import default_db_path


DEFAULT_DB_PATH = default_db_path()

DEFAULT_STALE_SECONDS = 900
DEFAULT_HEARTBEAT_SECONDS = 60
BUSY_TIMEOUT_MS = 5000


class PipelineAlreadyRunning(RuntimeError):
    pass


@dataclass
class PipelineLock:
    lock_name: str = "selleramp_pipeline"
    db_path: Path = DEFAULT_DB_PATH
    stale_seconds: int | None = None
    heartbeat_seconds: int | None = None
    run_id: int | None = None
    status: str = "new"
    reclaimed_info: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.stale_seconds = resolve_int(
            self.stale_seconds, "PIPELINE_LOCK_STALE_SECONDS", DEFAULT_STALE_SECONDS
        )
        self.heartbeat_seconds = resolve_int(
            self.heartbeat_seconds, "PIPELINE_LOCK_HEARTBEAT_SECONDS", DEFAULT_HEARTBEAT_SECONDS
        )
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

    def __enter__(self) -> "PipelineLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.stop_heartbeat()

        if self.status != "in_progress":
            return

        if exc_value is not None:
            self.finish("failed", str(exc_value))
            return

        self.finish("completed")

    def acquire(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        now = utc_now_iso()
        cutoff = iso_minus_seconds(now, int(self.stale_seconds))

        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            create_pipeline_runs_table(conn)
            self.reclaim_stale_locks(conn, cutoff, now)

            try:
                cursor = conn.execute(
                    """
                    INSERT INTO pipeline_runs (
                        lock_name,
                        started_at_utc,
                        status,
                        heartbeat_at_utc
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (self.lock_name, now, "in_progress", now),
                )
            except sqlite3.IntegrityError as error:
                raise PipelineAlreadyRunning(
                    f"Pipeline lock {self.lock_name!r} is already in progress."
                ) from error

            conn.commit()
            self.run_id = int(cursor.lastrowid)
            self.status = "in_progress"

        self.start_heartbeat()

    def reclaim_stale_locks(self, conn: sqlite3.Connection, cutoff: str, now: str) -> None:
        """Flip any stale in_progress row to 'reclaimed' so the lock slot frees up."""
        stale_rows = conn.execute(
            """
            SELECT id, started_at_utc, heartbeat_at_utc
            FROM pipeline_runs
            WHERE lock_name = ?
              AND status = 'in_progress'
              AND (heartbeat_at_utc IS NULL OR heartbeat_at_utc < ?)
            ORDER BY id ASC
            """,
            (self.lock_name, cutoff),
        ).fetchall()

        if not stale_rows:
            return

        conn.execute(
            """
            UPDATE pipeline_runs
            SET status = 'reclaimed',
                finished_at_utc = ?,
                error_message = ?
            WHERE lock_name = ?
              AND status = 'in_progress'
              AND (heartbeat_at_utc IS NULL OR heartbeat_at_utc < ?)
            """,
            (now, f"Reclaimed stale lock at {now} (no heartbeat).", self.lock_name, cutoff),
        )
        conn.commit()

        self.reclaimed_info = {
            "count": len(stale_rows),
            "run_ids": [int(row[0]) for row in stale_rows],
            "oldest_started_at_utc": stale_rows[0][1],
            "last_heartbeat_at_utc": stale_rows[0][2],
            "reclaimed_at_utc": now,
        }

    def start_heartbeat(self) -> None:
        if int(self.heartbeat_seconds) <= 0 or self.run_id is None:
            return

        self._heartbeat_stop.clear()
        thread = threading.Thread(
            target=self._heartbeat_loop,
            name="pipeline-lock-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread = thread
        thread.start()

    def _heartbeat_loop(self) -> None:
        interval = int(self.heartbeat_seconds)
        while not self._heartbeat_stop.wait(interval):
            try:
                self.write_heartbeat()
            except Exception as error:  # never let a transient lock kill the run
                print(f"[pipeline-lock] heartbeat update failed: {error}")

    def write_heartbeat(self) -> bool:
        """Refresh heartbeat_at_utc for this run. Returns True if a row was updated."""
        if self.run_id is None:
            return False

        now = utc_now_iso()
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            cursor = conn.execute(
                """
                UPDATE pipeline_runs
                SET heartbeat_at_utc = ?
                WHERE id = ? AND status = 'in_progress'
                """,
                (now, self.run_id),
            )
            conn.commit()

        return cursor.rowcount > 0

    def stop_heartbeat(self) -> None:
        self._heartbeat_stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=BUSY_TIMEOUT_MS / 1000 + 1)
        self._heartbeat_thread = None

    def finish(self, status: str, error_message: str | None = None) -> None:
        self.stop_heartbeat()

        if not self.run_id:
            return

        now = utc_now_iso()
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            create_pipeline_runs_table(conn)
            conn.execute(
                """
                UPDATE pipeline_runs
                SET finished_at_utc = ?,
                    status = ?,
                    heartbeat_at_utc = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (now, status, now, error_message, self.run_id),
            )
            conn.commit()

        self.status = status

    def mark_failed(self, error_message: str) -> None:
        self.finish("failed", error_message)


def create_pipeline_runs_table(conn: sqlite3.Connection) -> None:
    ensure_database_schema(conn)


def resolve_int(value: Any, env_name: str, default: int) -> int:
    return int_value(value if value is not None else os.getenv(env_name), default)


def iso_minus_seconds(now_iso: str, seconds: int) -> str:
    parsed = datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (parsed - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
