from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from uuid import uuid4
from threading import Event, Thread

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import settings
from app.routers.generate_review import build_generate_review_response
from app.schemas.review import GenerateReviewRequest, GenerateReviewResponse
from app.services.cloud_runtime import cleanup_runtime_files, runtime_dir
from app.services.export_service import (
    ExportError,
    anki_download_filename,
    export_anki_csv,
    export_review_report,
    report_download_filename,
)
from app.services.generation_diagnostics import diagnostic
from app.services import generation_store
from app.services.text_quality import content_disposition_header


router = APIRouter()
_executor = ThreadPoolExecutor(max_workers=1)


@router.post("/generate-review-jobs")
@router.post("/api/review/jobs")
def create_generate_review_job(request: GenerateReviewRequest) -> dict[str, str | bool]:
    cleanup_runtime_files()
    proposed_id = uuid4().hex
    job_id, resumed = generation_store.create_or_resume(
        proposed_id,
        request.model_dump(mode="json"),
    )
    _executor.submit(_run_job, job_id, request)
    diagnostic("job_submitted", job_id=job_id, project_id=request.project_id, resumed=resumed)
    return {"job_id": job_id, "resumed": resumed}


@router.get("/generate-review-jobs/{job_id}")
@router.get("/api/review/jobs/{job_id}")
def get_generate_review_job(job_id: str) -> dict:
    job = generation_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@router.get("/api/review/jobs/{job_id}/download/{export_format}")
def download_generate_review_job_file(job_id: str, export_format: str) -> FileResponse:
    job = generation_store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] not in {"completed", "partial"} or not job.get("result"):
        raise HTTPException(status_code=409, detail="Generation is not complete.")
    if export_format not in {"md", "docx", "pdf", "anki"}:
        raise HTTPException(status_code=400, detail="Unsupported export format.")

    result = GenerateReviewResponse.model_validate(job["result"])
    output_dir = runtime_dir(settings.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    title = result.review_report.title
    friendly_filename = (
        anki_download_filename(title)
        if export_format == "anki"
        else report_download_filename(title, export_format)
    )
    generation_store.save_export_status(job_id, export_format, "running")
    diagnostic("export_started", job_id=job_id, export_format=export_format, export_status="running")
    try:
        with TemporaryDirectory(prefix=f"recallforge-export-{job_id[:12]}-", dir=output_dir) as temporary:
            temporary_dir = Path(temporary)
            if export_format == "anki":
                rendered_path = export_anki_csv(
                    result.review_report,
                    temporary_dir,
                    friendly_filename,
                )
            else:
                rendered_path = export_review_report(
                    result.review_report,
                    result.markdown,
                    temporary_dir,
                    "artifact",
                    export_format,
                )
            _validate_export(rendered_path, export_format)
            output_path = output_dir / f"{job_id}-{friendly_filename}"
            rendered_path.replace(output_path)
    except OSError as exc:
        generation_store.save_export_status(job_id, export_format, "failed", "EXPORT_FILE_WRITE_ERROR")
        diagnostic("export_failed", job_id=job_id, export_format=export_format, export_status="failed", exception_type=type(exc).__name__, exception_message=str(exc)[:200])
        raise HTTPException(status_code=500, detail="EXPORT_FILE_WRITE_ERROR: export file could not be written") from exc
    except (ExportError, ValueError) as exc:
        generation_store.save_export_status(job_id, export_format, "failed", "EXPORT_RENDER_ERROR")
        diagnostic("export_failed", job_id=job_id, export_format=export_format, export_status="failed", exception_type=type(exc).__name__, exception_message=str(exc)[:200])
        raise HTTPException(status_code=500, detail=f"EXPORT_RENDER_ERROR: {exc}") from exc

    generation_store.save_export_status(job_id, export_format, "completed")
    diagnostic(
        "export_completed",
        job_id=job_id,
        export_format=export_format,
        export_status="completed",
        file_size=output_path.stat().st_size,
    )
    return FileResponse(
        path=output_path,
        filename=friendly_filename,
        headers={"Content-Disposition": content_disposition_header(friendly_filename)},
    )


def _validate_export(path: Path, export_format: str) -> None:
    if not path.exists() or path.stat().st_size <= 0:
        raise ValueError(f"{export_format} renderer produced an empty file")
    if export_format == "docx":
        from docx import Document

        reopened = Document(path)
        if not any(paragraph.text.strip() for paragraph in reopened.paragraphs):
            raise ValueError("DOCX has no readable body")
    elif export_format == "pdf":
        from pypdf import PdfReader

        if not PdfReader(str(path)).pages:
            raise ValueError("PDF has no readable pages")
    elif export_format in {"md", "anki"}:
        if not path.read_text(encoding="utf-8-sig").strip():
            raise ValueError(f"{export_format} has no readable content")


def _run_job(job_id: str, request: GenerateReviewRequest) -> None:
    owner = uuid4().hex
    if not generation_store.claim_job(job_id, owner):
        return
    stopped = Event()
    def heartbeat():
        while not stopped.wait(30):
            try:
                generation_store.renew_lease(job_id, owner)
            except Exception:
                diagnostic("job_lease_renewal_failed", job_id=job_id)
    worker = Thread(target=heartbeat, daemon=True)
    worker.start()
    try:
        _execute_job(job_id, request)
    finally:
        stopped.set()
        worker.join(timeout=1)
        generation_store.release_lease(job_id, owner)


def _execute_job(job_id: str, request: GenerateReviewRequest) -> None:
    started = time.perf_counter()
    diagnostic(
        "job_started",
        job_id=job_id,
        request_start=datetime.now().isoformat(timespec="milliseconds"),
    )
    generation_store.update_job(
        job_id,
        status="parsing",
        progress=3,
        message="Starting review generation.",
    )

    def progress_callback(progress: int, message: str) -> None:
        generation_store.update_job(
            job_id,
            status=stage_from_progress(progress),
            progress=progress,
            message=message,
        )

    try:
        result = build_generate_review_response(
            request,
            progress_callback,
            job_id=job_id,
            parsed_files_checkpoint=generation_store.load_parsed(job_id),
            parsed_callback=lambda parsed: generation_store.save_parsed(job_id, parsed),
            checkpoints=generation_store.load_checkpoints(job_id),
            checkpoint_callback=lambda key, chapter, chunk, status, content, code, message: generation_store.save_checkpoint(
                job_id, key, chapter, chunk, status, content, code, message
            ),
            # Canonical material is persisted before any renderer is invoked.
            # Export remains a separate, repeatable endpoint operation.
            defer_exports=True,
        )
        partial = result.llm_status in {"partial", "failed"} and result.review_report is not None
        error_code = result.llm_error.code if partial and result.llm_error else None
        retryable = bool(
            partial
            and result.llm_error
            and result.llm_error.can_retry
            and error_code
            and _retryable(error_code)
        )
        try:
            generation_store.update_job(
                job_id,
                status="partial" if partial else "completed",
                progress=100,
                message="Partially completed; provider action is required." if partial and not retryable else "Partially completed; retryable provider issue." if partial else "Completed.",
                result=result.model_dump(mode="json"),
                error_code=error_code,
                error_message=result.llm_error.message if partial and result.llm_error else None,
                retryable=retryable,
            )
        except Exception as exc:
            diagnostic(
                "generation_persist_failed",
                job_id=job_id,
                persistence_status="failed",
                error_code="GENERATION_PERSIST_ERROR",
                exception_type=type(exc).__name__,
                exception_message=str(exc)[:200],
            )
            raise
        persisted_payload = result.model_dump_json()
        diagnostic(
            "generation_persisted",
            job_id=job_id,
            persistence_status="success",
            status="partial" if partial else "completed",
            canonical_character_count=len(persisted_payload),
            chapter_count=result.generation_summary.chapter_count,
            chunk_count=result.generation_summary.chunk_count,
            llm_calls=result.generation_summary.llm_calls,
            retry_count=result.generation_summary.retry_count,
            latency_ms=int((time.perf_counter() - started) * 1000),
            request_end=datetime.now().isoformat(timespec="milliseconds"),
        )
    except HTTPException as exc:
        code = _classify_error(str(exc.detail))
        generation_store.update_job(job_id, status="retryable_failed" if _retryable(code) else "failed", progress=100, message="Stopped.", error_code=code, error_message=str(exc.detail), retryable=_retryable(code))
        diagnostic("job_failed", job_id=job_id, error_code=code, exception_type=type(exc).__name__, exception_message=str(exc.detail)[:200], latency_ms=int((time.perf_counter() - started) * 1000), request_end=datetime.now().isoformat(timespec="milliseconds"))
    except Exception as exc:
        code = _classify_error(str(exc))
        generation_store.update_job(job_id, status="retryable_failed" if _retryable(code) else "failed", progress=100, message="Stopped.", error_code=code, error_message=str(exc), retryable=_retryable(code))
        diagnostic("job_failed", job_id=job_id, error_code=code, exception_type=type(exc).__name__, exception_message=str(exc)[:200], latency_ms=int((time.perf_counter() - started) * 1000), request_end=datetime.now().isoformat(timespec="milliseconds"))


def _classify_error(message: str) -> str:
    known = (
        "GENERATION_PERSIST_ERROR",
        "LLM_OUTPUT_TRUNCATED",
        "LLM_CONTEXT_EXCEEDED",
        "LLM_TIMEOUT",
        "LLM_RESPONSE_PARSE_ERROR",
        "LLM_STAGE_FAILED",
        "QUALITY_FAILED",
        "PROVIDER_RATE_LIMIT",
        "PROVIDER_QUOTA_EXCEEDED",
        "LLM_PROVIDER_ERROR",
        "AUTH_FAILED",
        "CONFIG_MISSING",
        "MODEL_NOT_FOUND",
        "EXPORT_RENDER_ERROR",
        "EXPORT_FILE_WRITE_ERROR",
    )
    upper = message.upper()
    return next((code for code in known if code in upper), "GENERATION_UNKNOWN_ERROR")


def _retryable(code: str) -> bool:
    return code in {
        "LLM_STAGE_FAILED",
        "QUALITY_FAILED",
        "LLM_OUTPUT_TRUNCATED",
        "LLM_CONTEXT_EXCEEDED",
        "LLM_TIMEOUT",
        "LLM_RESPONSE_PARSE_ERROR",
        "PROVIDER_RATE_LIMIT",
        "LLM_PROVIDER_ERROR",
    }


def stage_from_progress(progress: int) -> str:
    if progress < 20:
        return "parsing"
    if progress < 60:
        return "ocr"
    if progress < 72:
        return "building_evidence"
    if progress < 94:
        return "llm"
    return "validating"
