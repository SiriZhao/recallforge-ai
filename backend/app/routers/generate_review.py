import logging
from collections.abc import Callable
from urllib.parse import quote

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.schemas.review import (
    GenerateReviewRequest,
    GenerateReviewResponse,
    GenerationSummary,
    ReoptimizeReviewRequest,
    ReoptimizeReviewResponse,
)
from app.services.export_service import (
    ExportError,
    anki_download_filename,
    export_anki_csv,
    export_review_report,
    report_download_filename,
)
from app.services.cloud_runtime import runtime_dir
from app.services.file_parser import ParseError, parse_file
from app.services.generation_diagnostics import diagnostic
from app.services.generation_pipeline import generate_hierarchical_report
from app.services.generator import generate_markdown_review
from app.services.llm_service import generate_review_summary
from app.services.llm_quality import validate_report_quality
from app.services.knowledge_extractor import build_generation_material
from app.services.ocr_service import OCRError
from app.services.review_planner import generate_review_report
from app.services.upload_resolver import (
    UploadResolveError,
    UploadedFileNotFoundError,
    resolve_uploaded_file,
)

router = APIRouter()
logger = logging.getLogger(__name__)
ProgressCallback = Callable[[int, str], None]


def download_url(filename: str) -> str:
    return f"/download/{quote(filename)}"


def with_server_default_llm(config):
    if not settings.enable_server_llm_key:
        return config
    if config.api_key:
        return config
    provider = config.provider or settings.default_llm_provider
    key = settings.deepseek_api_key if provider == "deepseek" else settings.openai_api_key
    if not key:
        return config
    merged = config.model_copy(deep=True)
    merged.enabled = True
    merged.provider = provider
    merged.model = merged.model or settings.default_llm_model
    merged.base_url = merged.base_url or settings.default_llm_base_url
    merged.api_key = key
    return merged


@router.post("/generate-review", response_model=GenerateReviewResponse)
@router.post("/api/generate-review", response_model=GenerateReviewResponse)
def generate_review(request: GenerateReviewRequest) -> GenerateReviewResponse:
    return build_generate_review_response(request)


@router.post("/review/reoptimize", response_model=ReoptimizeReviewResponse)
@router.post("/api/review/reoptimize", response_model=ReoptimizeReviewResponse)
def reoptimize_review(request: ReoptimizeReviewRequest) -> ReoptimizeReviewResponse:
    original = request.current_report
    evidence_text = request.evidence_text or original.markdown or generate_markdown_review(original)
    optimized = original.model_copy(deep=True)
    optimized.study_goal = request.original_study_goal
    optimized.exam_type = request.original_exam_type
    apply_optimization_goal(optimized, request.optimization_goal)

    if request.llm_config.enabled:
        llm_result = generate_review_summary(
            evidence_text,
            optimized,
            with_server_default_llm(request.llm_config),
            course_name=optimized.title,
            file_texts=[("current_report.md", evidence_text)],
            study_goal=request.original_study_goal,
            exam_type=request.original_exam_type,
            detail_level=request.detail_level,
            output_style=request.output_style,
        )
        candidate = llm_result.report
    else:
        candidate = optimized

    quality = validate_report_quality(
        candidate,
        evidence_text,
        study_goal=request.original_study_goal,
        exam_type=request.original_exam_type,
    )
    candidate.quality = quality.to_model()
    if quality.quality_score < 50:
        original_quality = validate_report_quality(
            original,
            evidence_text,
            study_goal=request.original_study_goal,
            exam_type=request.original_exam_type,
        ).to_model()
        original.quality = original_quality
        return ReoptimizeReviewResponse(
            review_report=original,
            markdown=generate_markdown_review(original),
            optimized=False,
            message="优化结果质量不足，已保留原报告。",
            quality=original_quality,
        )

    candidate.markdown = generate_markdown_review(candidate, prefer_existing=False)
    return ReoptimizeReviewResponse(
        review_report=candidate,
        markdown=candidate.markdown,
        optimized=True,
        message="已基于当前报告完成再优化，未重新 OCR 或重新解析文件。",
        quality=quality.to_model(),
    )


def build_generate_review_response(
    request: GenerateReviewRequest,
    progress_callback: ProgressCallback | None = None,
    *,
    job_id: str = "direct",
    parsed_files_checkpoint: list[dict] | None = None,
    parsed_callback: Callable[[list[dict]], None] | None = None,
    checkpoints: dict[str, dict] | None = None,
    checkpoint_callback: Callable | None = None,
    defer_exports: bool = False,
) -> GenerateReviewResponse:
    if not request.files:
        raise HTTPException(status_code=400, detail="请至少上传一个文件后再生成复习资料。")

    from app.schemas.review import ParsedFile
    parsed_files = [
        ParsedFile.model_validate(item) for item in (parsed_files_checkpoint or [])
    ]
    total_files = len(request.files)
    from pathlib import Path
    parsed_by_path = {str(Path(item.path).resolve()): item for item in parsed_files}
    for file_index, file_ref in enumerate(request.files, start=1):
        try:
            path = resolve_uploaded_file(file_ref)
            if str(path.resolve()) in parsed_by_path:
                continue
            if progress_callback:
                progress_callback(
                    10 + int(((file_index - 1) / max(total_files, 1)) * 45),
                    f"正在解析第 {file_index}/{total_files} 个文件。本地 OCR 处理扫描 PDF 可能较慢，请耐心等待。",
                )
            logger.info("Generate-review parse started: file_ref=%s", file_ref)
            def parse_progress(message: str, ratio: float | None) -> None:
                if not progress_callback:
                    return
                file_ratio = ratio or 0
                percent = 10 + int(((file_index - 1 + file_ratio) / max(total_files, 1)) * 55)
                progress_callback(min(65, percent), message)

            if progress_callback:
                parsed_files.append(parse_file(path, request.ocr_config, parse_progress))
            else:
                parsed_files.append(parse_file(path, request.ocr_config))
            # Persist after each successful file so a later OCR failure does
            # not discard already verified parsing work.
            if parsed_callback:
                try:
                    parsed_callback([item.model_dump(mode="json") for item in parsed_files])
                except Exception as exc:
                    raise RuntimeError("GENERATION_PERSIST_ERROR: parsed material checkpoint could not be saved") from exc
            logger.info("Generate-review parse completed: file_ref=%s", file_ref)
        except UploadedFileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except UploadResolveError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (ParseError, OCRError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if parsed_callback and not parsed_files_checkpoint:
        try:
            parsed_callback([item.model_dump(mode="json") for item in parsed_files])
            diagnostic(
                "parse_persisted",
                job_id=job_id,
                source_page_count=sum(len(item.pages) for item in parsed_files),
                extracted_text_character_count=sum(len(item.raw_text) for item in parsed_files),
                persistence_status="success",
            )
        except Exception as exc:
            diagnostic("parse_persist_failed", job_id=job_id, exception_type=type(exc).__name__, exception_message=str(exc)[:200])
            raise RuntimeError("GENERATION_PERSIST_ERROR: parsed material checkpoint could not be saved") from exc


    if progress_callback:
        progress_callback(66, "正在分析标题、章节树、页码、表格和图片。")
    combined_text, file_texts, knowledge = build_generation_material(parsed_files)
    report_title = request.course_name or request.title

    if progress_callback:
        progress_callback(
            68,
            f"正在提取知识：已识别 {len(knowledge.topics)} 个章节节点、"
            f"{len(knowledge.formulas)} 条公式和 {len(knowledge.tables)} 个表格。",
        )
    llm_config = with_server_default_llm(request.llm_config)
    safe_draft = None
    # A deterministic report is deliberately delayed until offline mode (or an
    # explicit catastrophic provider fallback). It is not the AI input.
    if not llm_config.enabled:
        logger.info("Offline/basic review selected; generating deterministic fallback.")
        safe_draft = generate_review_report(
            combined_text, title=report_title, file_texts=file_texts,
            study_goal=request.study_goal, exam_type=request.exam_type,
        )
        safe_draft.detail_level = request.detail_level
        safe_draft.output_style = request.output_style

    if progress_callback:
        if request.llm_config.enabled:
            progress_callback(
                78,
                "正在进行 AI 深度整理：先理解证据，再建立课程模型、学习蓝图并完成全局合成。",
            )
        else:
            progress_callback(78, "正在生成本地安全底稿。")

    try:
        llm_result, pipeline_stats = generate_hierarchical_report(
            parsed_files, safe_draft, llm_config, job_id=job_id,
            checkpoints=checkpoints, checkpoint_callback=checkpoint_callback,
            progress_callback=progress_callback, study_goal=request.study_goal,
            exam_type=request.exam_type, detail_level=request.detail_level,
            output_style=request.output_style, title=report_title,
            learner_context=request.model_dump(include={"exam_date", "daily_minutes", "mastery_level", "target_score", "focus", "learning_mode"}),
            material_types=request.material_types,
        )
    except Exception as exc:
        if "GENERATION_PERSIST_ERROR" in str(exc):
            raise
        if not llm_config.enabled:
            raise
        logger.warning("AI pipeline failed; entering explicit Basic Offline Review fallback: %s", str(exc)[:200])
        fallback = generate_review_report(
            combined_text, title=report_title, file_texts=file_texts,
            study_goal=request.study_goal, exam_type=request.exam_type,
        )
        fallback.detail_level = request.detail_level
        fallback.output_style = request.output_style
        from app.services.llm_service import LLMEnhancementResult
        llm_result = LLMEnhancementResult(
            report=fallback, report_source="rule_based_with_llm_failed", llm_status="failed",
            fallback_used=True, llm_error=getattr(exc, "error", None), llm_context_strategy="failed",
        )
        pipeline_stats = type("FallbackStats", (), {"chunk_count": 0})()
    report = llm_result.report
    report.study_goal = request.study_goal
    report.exam_type = request.exam_type
    report.detail_level = request.detail_level
    report.output_style = request.output_style
    quality = validate_report_quality(
        report,
        combined_text,
        study_goal=request.study_goal,
        exam_type=request.exam_type,
        file_count=len(parsed_files),
    )
    report.quality = quality.to_model()

    if progress_callback:
        progress_callback(94, "AI 生成已完成，正在保存可重复导出的学习内容。")
    markdown = report.markdown or generate_markdown_review(report)
    export_formats = request.export_formats or [request.export_format]
    if "md" not in export_formats:
        export_formats = ["md", *export_formats]

    download_links = {}
    anki_csv_download_path = None
    if not defer_exports:
        try:
            output_dir = runtime_dir(settings.output_dir)
            anki_path = export_anki_csv(report, output_dir, anki_download_filename(report_title))
            anki_csv_download_path = download_url(anki_path.name)
            for export_format in export_formats:
                logger.info("Export started: format=%s", export_format)
                output_path = export_review_report(
                    report=report,
                    markdown=markdown,
                    output_dir=output_dir,
                    basename=report_download_filename(report_title, export_format).rsplit(".", 1)[0],
                    export_format=export_format,
                )
                download_links[export_format] = download_url(output_path.name)
                logger.info("Export completed: format=%s filename=%s", export_format, output_path.name)
        except ExportError as exc:
            diagnostic("export_failed", job_id=job_id, export_status="failed", exception_type=type(exc).__name__, exception_message=str(exc)[:200])
            raise HTTPException(status_code=500, detail=f"EXPORT_RENDER_ERROR: {exc}") from exc

    if progress_callback:
        progress_callback(100, "已完成。")
    generation_summary = build_generation_summary(parsed_files, report, llm_result)
    return GenerateReviewResponse(
        review_report=report,
        markdown=markdown,
        download_path=download_links.get(request.export_format) or next(iter(download_links.values()), ""),
        download_links=download_links,
        anki_csv_download_path=anki_csv_download_path,
        export_format=request.export_format,
        report_source=llm_result.report_source,
        llm_status=llm_result.llm_status,
        fallback_used=llm_result.fallback_used,
        llm_error=llm_result.llm_error,
        llm_context_strategy=llm_result.llm_context_strategy,
        generation_summary=generation_summary,
    )


def build_generation_summary(parsed_files, report, llm_result) -> GenerationSummary:
    pages = [page for parsed in parsed_files for page in parsed.pages]
    pages_text = [page for page in pages if page.source == "text_extract" and page.text.strip()]
    pages_ocr = [page for page in pages if page.source == "ocr_fallback" and page.text.strip()]
    pages_skipped = [page for page in pages if page.source == "ocr_fallback" and not page.text.strip()]
    cache_hits = sum(1 for parsed in parsed_files if getattr(parsed, "ocr_cache_used", False))
    notes: list[str] = []
    for parsed in parsed_files:
        notes.extend(getattr(parsed, "warnings", []) or [])
    if pages_text and not pages_ocr:
        notes.append("已检测到文字版 PDF 或可直接提取文本的材料，跳过不必要 OCR。")
    if llm_result.fallback_used:
        notes.append("AI 深度整理存在未完成阶段；已标记未解析证据，可从 checkpoint 重试。")
    if llm_result.llm_context_strategy == "chunked" and not llm_result.fallback_used:
        notes.append("AI 深度整理已完成：系统已自动使用分块整理处理长材料。")
    return GenerationSummary(
        files_processed=len(parsed_files),
        pages_total=len(pages),
        pages_text_extracted=len(pages_text),
        pages_ocr_processed=len(pages_ocr),
        pages_ocr_skipped=len(pages_skipped),
        ocr_cache_hits=cache_hits,
        evidence_chunks=max(1, sum(max(1, len(parsed.raw_text) // 1800) for parsed in parsed_files)) if parsed_files else 0,
        detected_study_units=len(report.study_units) or len(report.chapters),
        detected_question_types=len(report.question_types),
        mock_questions_count=len(report.mock_exam.questions),
        anki_cards_count=len(report.anki_cards),
        llm_calls=llm_result.llm_calls,
        retry_count=llm_result.retry_count,
        chapter_count=len(report.study_units) or len(report.chapters),
        chunk_count=llm_result.chunk_count,
        fallback_used=llm_result.fallback_used,
        final_report_source=llm_result.report_source,
        notes=notes[:12],
    )


def apply_optimization_goal(report, optimization_goal: str) -> None:
    report.overview = {**report.overview, "reoptimized_for": optimization_goal}
    if optimization_goal == "concise":
        report.low_priority = (report.low_priority or []) + ["已压缩低频背景内容，优先保留高频考点、题型和冲刺清单。"]
        report.review_order = report.review_order[:6]
    elif optimization_goal in {"memorization", "anki"}:
        report.anki_cards = dedupe_anki_cards(report.anki_cards)
        for card in report.anki_cards:
            card.priority = max(card.priority, 75)
            card.card_type = card.card_type or "definition"
    elif optimization_goal in {"practice", "exam_like"}:
        for question in report.mock_exam.questions:
            question.source_hint = question.source_hint or "来自当前报告中的考点和题型线索"
            question.related_topic = question.related_topic or question.concept or question.chapter
    elif optimization_goal == "sprint":
        report.sprint_checklist = report.sprint_checklist[:12] or ["先复习最高优先级专题", "完成模拟题并核对答案", "用 Anki 复盘易错点"]
    report.markdown = generate_markdown_review(report, prefer_existing=False)


def dedupe_anki_cards(cards):
    seen: set[str] = set()
    deduped = []
    for card in cards:
        key = card.front.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(card)
    return deduped
