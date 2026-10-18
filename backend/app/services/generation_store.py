from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
import time

from app.config import settings


TERMINAL_RESUMABLE = {"retryable_failed", "partial", "failed"}


def database_path() -> Path:
    override = os.getenv("RECALLFORGE_JOB_DB")
    if override:
        return Path(override).expanduser().resolve()
    prefix = "sqlite:///"
    if not settings.database_url.startswith(prefix):
        raise RuntimeError("Generation checkpoints require the configured SQLite/local-first database.")
    return Path(settings.database_url[len(prefix) :]).expanduser().resolve()


def connect() -> sqlite3.Connection:
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    ensure_schema(connection)
    return connection


@contextmanager
def connection_scope():
    connection = connect()
    try:
        yield connection
    finally:
        connection.close()


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS generation_jobs (
            job_id TEXT PRIMARY KEY,
            project_id TEXT,
            request_fingerprint TEXT NOT NULL,
            request_json TEXT NOT NULL,
            status TEXT NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            result_json TEXT,
            parsed_json TEXT,
            error_code TEXT,
            error_message TEXT,
            retryable INTEGER NOT NULL DEFAULT 0,
            retry_count INTEGER NOT NULL DEFAULT 0,
            export_status_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS ix_generation_jobs_project
            ON generation_jobs(project_id, updated_at);
        CREATE TABLE IF NOT EXISTS chapter_generation_checkpoints (
            job_id TEXT NOT NULL,
            checkpoint_key TEXT NOT NULL,
            chapter_index INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            status TEXT NOT NULL,
            content_json TEXT,
            error_code TEXT,
            error_message TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(job_id, checkpoint_key),
            FOREIGN KEY(job_id) REFERENCES generation_jobs(job_id) ON DELETE CASCADE
        );
        """
    )
    # Additive migration: existing jobs, parsed material and checkpoints stay
    # readable. A lease permits recovery after process death without storing keys.
    columns = {row[1] for row in connection.execute("PRAGMA table_info(generation_jobs)")}
    for name, kind in (("lease_owner", "TEXT"), ("lease_expires", "REAL")):
        if name not in columns:
            connection.execute(f"ALTER TABLE generation_jobs ADD COLUMN {name} {kind}")
    connection.commit()


def request_fingerprint(payload: dict[str, Any]) -> str:
    from app.services.stage_checkpoints import PROMPT_VERSION
    from app.services.upload_resolver import resolve_uploaded_file, UploadResolveError

    stable = {
        key: value
        for key, value in payload.items()
        if key not in {"export_format", "export_formats", "llm_config"}
    }
    llm = payload.get("llm_config") or {}
    stable["llm_config"] = {
        "provider": llm.get("provider"),
        "model": llm.get("model"),
        "base_url": llm.get("base_url"),
        "enabled": llm.get("enabled"),
    }
    stable["prompt_version"] = PROMPT_VERSION
    stable["ocr_config"] = {key: value for key, value in (payload.get("ocr_config") or {}).items() if key not in {"api_key", "secret_key"}}
    content_hashes = {}
    for ref in payload.get("files", []):
        try:
            path = resolve_uploaded_file(ref)
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            content_hashes[ref] = digest.hexdigest()
        except (UploadResolveError, OSError):
            # Missing input is rejected by the parser. Never reuse a prior
            # readable file fingerprint after deletion or loss of access.
            content_hashes[ref] = "unavailable"
    stable["input_content_hashes"] = content_hashes
    encoded = json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sanitized_request(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = json.loads(json.dumps(payload, ensure_ascii=False))
    if isinstance(cleaned.get("llm_config"), dict):
        cleaned["llm_config"]["api_key"] = None
    if isinstance(cleaned.get("ocr_config"), dict):
        cleaned["ocr_config"]["api_key"] = None
        cleaned["ocr_config"]["secret_key"] = None
    return cleaned


def create_or_resume(job_id: str, payload: dict[str, Any]) -> tuple[str, bool]:
    now = datetime.now().isoformat(timespec="seconds")
    fingerprint = request_fingerprint(payload)
    project_id = payload.get("project_id")
    with connection_scope() as connection:
        connection.execute("BEGIN IMMEDIATE")
        # A project-backed request is the normal UI path, but the public API
        # also supports generation without a project. Both need resumable
        # checkpoints; treating a NULL project id as "never resume" would
        # silently re-run parsing/OCR for those callers.
        existing = connection.execute(
            """
            SELECT job_id, status, lease_expires FROM generation_jobs
            WHERE request_fingerprint=?
              AND (project_id=? OR (project_id IS NULL AND ? IS NULL))
            ORDER BY updated_at DESC LIMIT 1
            """,
            (fingerprint, project_id, project_id),
        ).fetchone()
        active = {"pending", "retrying", "parsing", "ocr", "building_evidence", "llm", "validating"}
        if existing and existing["status"] in active and (existing["lease_expires"] or 0) > time.time():
            return str(existing["job_id"]), True
        if existing and (existing["status"] in TERMINAL_RESUMABLE or existing["status"] in active):
            connection.execute(
                """
                UPDATE generation_jobs
                SET status='retrying', progress=1, message='Resuming from saved checkpoint.',
                    error_code=NULL, error_message=NULL, retryable=0,
                    retry_count=retry_count+1, request_json=?, updated_at=?, lease_owner=NULL, lease_expires=NULL
                WHERE job_id=?
                """,
                (json.dumps(sanitized_request(payload), ensure_ascii=False), now, existing["job_id"]),
            )
            connection.commit()
            return str(existing["job_id"]), True
        connection.execute(
            """
            INSERT INTO generation_jobs(
                job_id, project_id, request_fingerprint, request_json, status,
                progress, message, created_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                job_id,
                project_id,
                fingerprint,
                json.dumps(sanitized_request(payload), ensure_ascii=False),
                "pending",
                1,
                "Job created. Waiting for processing.",
                now,
                now,
            ),
        )
        connection.commit()
    return job_id, False


def claim_job(job_id: str, owner: str) -> bool:
    with connection_scope() as connection:
        cursor = connection.execute(
            "UPDATE generation_jobs SET lease_owner=?, lease_expires=? WHERE job_id=? AND status IN ('pending', 'retrying') AND (lease_owner IS NULL OR lease_expires < ?)",
            (owner, time.time() + 120, job_id, time.time()),
        )
        connection.commit()
        return cursor.rowcount == 1


def renew_lease(job_id: str, owner: str) -> None:
    with connection_scope() as connection:
        connection.execute("UPDATE generation_jobs SET lease_expires=? WHERE job_id=? AND lease_owner=?", (time.time() + 120, job_id, owner))
        connection.commit()


def release_lease(job_id: str, owner: str) -> None:
    with connection_scope() as connection:
        connection.execute("UPDATE generation_jobs SET lease_owner=NULL, lease_expires=NULL WHERE job_id=? AND lease_owner=?", (job_id, owner))
        connection.commit()


def update_job(
    job_id: str,
    *,
    status: str,
    progress: int,
    message: str,
    result: dict | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    retryable: bool = False,
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with connection_scope() as connection:
        connection.execute(
            """
            UPDATE generation_jobs SET status=?, progress=?, message=?,
                result_json=COALESCE(?, result_json), error_code=?, error_message=?,
                retryable=?, updated_at=? WHERE job_id=?
            """,
            (
                status,
                max(0, min(100, progress)),
                message,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                error_code,
                error_message[:500] if error_message else None,
                int(retryable),
                now,
                job_id,
            ),
        )
        connection.commit()


def save_parsed(job_id: str, parsed_files: list[dict]) -> None:
    with connection_scope() as connection:
        connection.execute(
            "UPDATE generation_jobs SET parsed_json=?, updated_at=? WHERE job_id=?",
            (json.dumps(parsed_files, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), job_id),
        )
        connection.commit()


def load_parsed(job_id: str) -> list[dict] | None:
    with connection_scope() as connection:
        row = connection.execute("SELECT parsed_json FROM generation_jobs WHERE job_id=?", (job_id,)).fetchone()
    return json.loads(row["parsed_json"]) if row and row["parsed_json"] else None


def save_checkpoint(job_id: str, key: str, chapter_index: int, chunk_index: int, status: str, content: dict | None, error_code: str | None, error_message: str | None) -> None:
    with connection_scope() as connection:
        connection.execute(
            """
            INSERT INTO chapter_generation_checkpoints(
                job_id, checkpoint_key, chapter_index, chunk_index, status,
                content_json, error_code, error_message, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id, checkpoint_key) DO UPDATE SET
                status=excluded.status, content_json=excluded.content_json,
                error_code=excluded.error_code, error_message=excluded.error_message,
                updated_at=excluded.updated_at
            """,
            (
                job_id,
                key,
                chapter_index,
                chunk_index,
                status,
                json.dumps(content, ensure_ascii=False) if content is not None else None,
                error_code,
                error_message[:500] if error_message else None,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        connection.commit()


def load_checkpoints(job_id: str) -> dict[str, dict]:
    with connection_scope() as connection:
        rows = connection.execute(
            "SELECT checkpoint_key, status, content_json, error_code, error_message FROM chapter_generation_checkpoints WHERE job_id=?",
            (job_id,),
        ).fetchall()
    return {
        row["checkpoint_key"]: {
            "status": row["status"],
            "content": json.loads(row["content_json"]) if row["content_json"] else None,
            "error_code": row["error_code"],
            "error_message": row["error_message"],
        }
        for row in rows
    }


def get_job(job_id: str) -> dict | None:
    with connection_scope() as connection:
        row = connection.execute("SELECT * FROM generation_jobs WHERE job_id=?", (job_id,)).fetchone()
    if not row:
        return None
    return {
        "job_id": row["job_id"],
        "project_id": row["project_id"],
        "status": row["status"],
        "progress": row["progress"],
        "message": row["message"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": row["error_message"],
        "error_code": row["error_code"],
        "retryable": bool(row["retryable"]),
        "retry_count": row["retry_count"],
        "export_status": json.loads(row["export_status_json"] or "{}"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def save_export_status(job_id: str, export_format: str, status: str, error_code: str | None = None) -> None:
    with connection_scope() as connection:
        row = connection.execute("SELECT export_status_json FROM generation_jobs WHERE job_id=?", (job_id,)).fetchone()
        values = json.loads(row["export_status_json"] or "{}") if row else {}
        values[export_format] = {"status": status, "error_code": error_code}
        connection.execute(
            "UPDATE generation_jobs SET export_status_json=?, updated_at=? WHERE job_id=?",
            (json.dumps(values, ensure_ascii=False), datetime.now().isoformat(timespec="seconds"), job_id),
        )
        connection.commit()
