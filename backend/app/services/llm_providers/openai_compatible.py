import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from pydantic import ValidationError

from app.config import settings
from app.schemas.review import DetailLevel, ExamType, LLMConfig, LLMErrorInfo, OutputStyle, ReviewPlanItem, ReviewReport, StudyGoal, StudyUnit
from app.services.chapter_extractor import clean_unit_title, is_bad_unit_title
from app.services.evidence_pack import build_evidence_pack
from app.services.generator import generate_markdown_review
from app.services.generation_diagnostics import diagnostic
from app.services.llm_capabilities import capability_for, estimate_tokens
from app.services.llm_providers.base import BaseLLMProvider, LLMProviderError
from app.services.llm_quality import build_repair_report_prompt, validate_report_quality
from app.services.llm_service_prompt import (
    CONTEXT_TOO_LONG_MESSAGE,
    MAX_LLM_INPUT_CHARS,
    build_chunk_summary_prompt,
    build_compact_review_prompt,
    build_outline_naming_prompt,
    build_review_prompt,
    prepare_llm_context,
    split_material_chunks,
)
from app.services.text_quality import clean_formula_text, clean_topic_list, clean_topic_name
from app.services.study_unit_prompt import STUDY_UNIT_SYSTEM_PROMPT, build_study_unit_prompt

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LLMChatResult:
    content: str
    finish_reason: str | None
    stop_reason: str | None
    usage: dict[str, Any]
    http_status: int


class OpenAICompatibleLLMProvider(BaseLLMProvider):
    name = "openai"
    display_name = "OpenAI"
    default_base_url = "https://api.openai.com/v1"
    default_model = "gpt-4o-mini"
    needs_v1_suffix = True

    def generate_stage(self, stage: str, prompt: str, config: LLMConfig, *, max_output_tokens: int) -> str:
        """Run a typed stage; teaching prose stays natural inside JSON fields."""
        endpoint, model = self.prepare_request(config)
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are an expert university tutor, curriculum analyst, and exam-preparation editor. Preserve grounding, distinguish observed/inferred/generated material, and write only what the requested stage needs."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "max_tokens": max(512, int(max_output_tokens)),
        }
        if self.name in {"deepseek", "openai", "qwen"}:
            payload["response_format"] = {"type": "json_object"}
        result = self.post_chat_result(endpoint, config.api_key or "", payload, timeout=max(30, settings.request_timeout_seconds))
        return result.content

    def generate_study_unit(
        self,
        chapter_title: str,
        chunk_text: str,
        config: LLMConfig,
        *,
        max_output_tokens: int,
        concise: bool = False,
    ) -> StudyUnit:
        endpoint, model = self.prepare_request(config)
        prompt = build_study_unit_prompt(chapter_title, chunk_text, concise=concise)
        configured_max_output = max(512, int(max_output_tokens))
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": STUDY_UNIT_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": configured_max_output,
        }
        diagnostic(
            "llm_request_payload",
            provider=self.display_name,
            model=model,
            prompt_token_estimate=estimate_tokens(STUDY_UNIT_SYSTEM_PROMPT, model) + estimate_tokens(prompt, model) + 32,
            configured_context_limit=capability_for(config.provider, model).context_window,
            configured_max_output=configured_max_output,
            request_body_bytes=len(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
        )
        result = self.post_chat_result(
            endpoint,
            config.api_key or "",
            payload,
            timeout=max(10, settings.request_timeout_seconds),
        )
        try:
            data = tolerant_parse_llm_report(result.content)
            if isinstance(data.get("study_unit"), dict):
                data = data["study_unit"]
            data.setdefault("name", chapter_title)
            return StudyUnit.model_validate(data)
        except (json.JSONDecodeError, TypeError, ValidationError, ValueError) as exc:
            truncated = response_likely_truncated(result, configured_max_output)
            diagnostic(
                "llm_parse_failed",
                provider=self.display_name,
                model=model,
                finish_reason=result.finish_reason,
                returned_character_count=len(result.content),
                exception_type=type(exc).__name__,
                error_code="LLM_OUTPUT_TRUNCATED" if truncated else "LLM_RESPONSE_PARSE_ERROR",
            )
            raise self.error(
                "LLM_OUTPUT_TRUNCATED" if truncated else "LLM_RESPONSE_PARSE_ERROR",
                "The model returned truncated chapter data." if truncated else "The model returned incomplete or invalid chapter data.",
                "RecallForge will reduce the current unit output and retry a bounded number of times.",
                config,
                model=model,
            ) from exc

    def enhance_report(
        self,
        materials_text: str,
        safe_draft: ReviewReport,
        config: LLMConfig,
        *,
        course_name: str | None = None,
        file_texts: list[tuple[str, str]] | None = None,
        study_goal: StudyGoal = "balanced",
        exam_type: ExamType = "unknown",
        detail_level: DetailLevel = "detailed",
        output_style: OutputStyle = "teaching_assistant",
    ) -> ReviewReport:
        endpoint, model = self.prepare_request(config)
        prepared = prepare_llm_context(materials_text, safe_draft)
        self.last_context_strategy = (
            "chunked"
            if prepared.needs_chunking
            else "compressed"
            if prepared.original_chars > MAX_LLM_INPUT_CHARS
            else "direct"
        )
        logger.info(
            "LLM evidence preparation: original_chars=%s prepared_chars=%s chunking=%s "
            "chunk_count=%s chunk_chars=%s",
            prepared.original_chars,
            prepared.compressed_chars,
            prepared.needs_chunking,
            prepared.chunk_count,
            prepared.chunk_chars,
        )

        evidence_pack = build_evidence_pack(
            prepared.text,
            safe_draft,
            course_name=course_name,
            file_texts=file_texts,
        ).to_dict()
        chunk_insights = self.build_chunk_insights(endpoint, model, config, materials_text, prepared.needs_chunking)
        prompt = build_review_prompt(
            evidence_pack,
            safe_draft,
            chunk_insights,
            study_goal=study_goal,
            exam_type=exam_type,
            detail_level=detail_level,
            output_style=output_style,
        )
        if len(prompt) > MAX_LLM_INPUT_CHARS:
            logger.info("LLM final prompt exceeded budget; switching to compact synthesis. prompt_chars=%s", len(prompt))
            self.last_context_strategy = "chunked"
            prompt = build_compact_review_prompt(
                evidence_pack,
                safe_draft,
                chunk_insights,
                study_goal=study_goal,
                exam_type=exam_type,
                detail_level=detail_level,
                output_style=output_style,
            )
        if len(prompt) > MAX_LLM_INPUT_CHARS:
            raise self.error(
                "CONTEXT_TOO_LONG",
                "资料过长，大模型深度整理未完成。",
                CONTEXT_TOO_LONG_MESSAGE,
                config,
                model=model,
            )

        try:
            report = self.request_review_report(endpoint, model, config, prompt, timeout=100)
        except LLMProviderError as exc:
            if exc.error.code != "CONTEXT_TOO_LONG":
                raise
            logger.info("LLM final synthesis hit CONTEXT_TOO_LONG; retrying with compact evidence pack.")
            self.last_context_strategy = "chunked"
            compact_prompt = build_compact_review_prompt(
                evidence_pack,
                safe_draft,
                chunk_insights,
                study_goal=study_goal,
                exam_type=exam_type,
                detail_level=detail_level,
                output_style=output_style,
                budget=int(MAX_LLM_INPUT_CHARS * 0.55),
            )
            report = self.request_review_report(endpoint, model, config, compact_prompt, timeout=100)
        fill_export_fallbacks(report, safe_draft)
        quality = validate_report_quality(report, materials_text, study_goal=study_goal, exam_type=exam_type, file_count=len(file_texts or []))
        report.quality = quality.to_model()
        logger.info(
            "LLM report quality checked: score=%s warnings=%s failures=%s repairable=%s",
            quality.quality_score,
            quality.quality_warnings,
            quality.quality_failures,
            quality.repairable,
        )
        if quality.quality_score >= 75:
            return ensure_final_markdown(report)

        if not quality.repairable:
            raise self.error(
                "QUALITY_FAILED",
                "大模型输出质量不足，系统已回退到本地安全底稿。",
                "AI 输出存在内容过短、乱码或缺少关键模块等问题。请检查材料质量或更换模型后重试。",
                config,
                model=model,
            )

        repair_prompt = build_repair_report_prompt(report, quality, prompt[:MAX_LLM_INPUT_CHARS])
        repaired = self.request_review_report(endpoint, model, config, repair_prompt, timeout=100)
        fill_export_fallbacks(repaired, safe_draft)
        repaired_quality = validate_report_quality(repaired, materials_text, study_goal=study_goal, exam_type=exam_type, file_count=len(file_texts or []))
        repaired.quality = repaired_quality.to_model()
        logger.info(
            "LLM repaired report quality checked: score=%s warnings=%s failures=%s repairable=%s",
            repaired_quality.quality_score,
            repaired_quality.quality_warnings,
            repaired_quality.quality_failures,
            repaired_quality.repairable,
        )
        if repaired_quality.quality_score >= 75:
            return ensure_final_markdown(repaired)

        raise self.error(
            "QUALITY_FAILED",
            "大模型输出质量不足，系统已回退到本地安全底稿。",
            "系统已尝试自动修复 AI 输出，但修复后仍缺少可用题目、答案、Anki 卡片或存在明显空泛内容。",
            config,
            model=model,
        )

    def improve_safe_draft_outline(
        self,
        materials_text: str,
        safe_draft: ReviewReport,
        config: LLMConfig,
        *,
        course_name: str | None = None,
        file_texts: list[tuple[str, str]] | None = None,
        study_goal: StudyGoal = "balanced",
        exam_type: ExamType = "unknown",
        detail_level: DetailLevel = "detailed",
        output_style: OutputStyle = "teaching_assistant",
    ) -> ReviewReport:
        endpoint, model = self.prepare_request(config)
        prepared = prepare_llm_context(materials_text, safe_draft)
        evidence_pack = build_evidence_pack(
            prepared.text[:MAX_LLM_INPUT_CHARS],
            safe_draft,
            course_name=course_name,
            file_texts=file_texts,
        ).to_dict()
        bad_titles = [
            chapter.chapter
            for chapter in safe_draft.chapters
            if is_bad_unit_title(chapter.chapter)
        ]
        prompt = build_outline_naming_prompt(evidence_pack, bad_titles)
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": "你只负责为本地安全底稿做轻量专题命名，输出合法 JSON，不要输出 Markdown。",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 1400,
        }
        content = self.post_chat_completions(endpoint, config.api_key or "", payload, timeout=45)
        data = tolerant_parse_llm_report(content)
        units = build_named_study_units(data.get("study_units"))
        if not units:
            raise ValueError("Outline naming did not return usable study units.")

        draft = safe_draft.model_copy(deep=True)
        draft.study_goal = study_goal
        draft.exam_type = exam_type
        draft.study_units = units
        for index, unit in enumerate(units):
            if index < len(draft.chapters):
                draft.chapters[index].chapter = unit.name
                draft.chapters[index].review_advice = unit.how_to_review or draft.chapters[index].review_advice
                if unit.key_points:
                    draft.chapters[index].keywords = unit.key_points
            if index < len(draft.review_order):
                draft.review_order[index] = ReviewPlanItem(
                    chapter=unit.name,
                    importance=unit.priority,
                    reason=unit.reason or draft.review_order[index].reason,
                )
        draft.overview = {
            **draft.overview,
            "outline_naming": "完整 AI 深度整理未成功，已使用轻量 AI 调用优化专题名称。",
        }
        draft.markdown = generate_markdown_review(draft, prefer_existing=False)
        return draft

    def build_chunk_insights(
        self,
        endpoint: str,
        model: str,
        config: LLMConfig,
        materials_text: str,
        needs_chunking: bool,
    ) -> list[str]:
        if not needs_chunking:
            return []

        chunks = split_material_chunks(materials_text)
        insights: list[str] = []
        for index, chunk in enumerate(chunks, start=1):
            logger.info("LLM chunk insight started: index=%s total=%s chars=%s", index, len(chunks), len(chunk))
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "你是课程材料理解助手。请输出简体中文 chunk_insight，重点保留考点、"
                            "题型、Anki 候选和证据片段。"
                        ),
                    },
                    {"role": "user", "content": build_chunk_summary_prompt(chunk, index, len(chunks))},
                ],
                "temperature": 0.15,
                "max_tokens": 1200,
            }
            try:
                insights.append(self.post_chat_completions(endpoint, config.api_key or "", payload, timeout=70))
                logger.info("LLM chunk insight completed: index=%s", index)
            except LLMProviderError as exc:
                logger.warning("LLM chunk insight failed: index=%s code=%s", index, exc.error.code)
                fallback = local_chunk_insight(chunk, index, len(chunks))
                insights.append(fallback)
                logger.info("LLM chunk insight fallback used: index=%s chars=%s", index, len(fallback))
                continue
                raise self.error(
                    "CONTEXT_TOO_LONG",
                    "资料过长，大模型分块理解未完成。",
                    CONTEXT_TOO_LONG_MESSAGE,
                    config,
                    model=model,
                ) from exc
        return insights

    def request_review_report(
        self,
        endpoint: str,
        model: str,
        config: LLMConfig,
        prompt: str,
        *,
        timeout: int,
    ) -> ReviewReport:
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是资深大学期末复习资料整理专家，不是普通摘要助手。优先输出合法 JSON 对象，"
                        "不要在 JSON 前后添加解释文字，不要用 Markdown 代码块包裹 JSON。"
                        "如果无法稳定输出 JSON，可以输出一份完整 Markdown 复习报告，但必须包含复习导览、"
                        "知识结构、高频考点、题型分析、模拟题与答案、Anki 卡片和考前冲刺计划。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.35,
            "max_tokens": min(
                capability_for(config.provider, model).max_output_tokens,
                capability_for(config.provider, model).reserved_output_tokens,
            ),
        }
        content = self.post_chat_completions(endpoint, config.api_key or "", payload, timeout=timeout)
        try:
            data = tolerant_parse_llm_report(content)
            normalize_review_payload(data)
            return ReviewReport.model_validate(data)
        except (json.JSONDecodeError, KeyError, TypeError, ValidationError, ValueError) as exc:
            logger.warning("LLM response parse failed. returned_chars=%s", len(content or ""))
            raise self.error(
                "RESPONSE_PARSE_ERROR",
                "大模型返回内容无法解析为可用复习资料包。",
                "请重试，或切换模型后再生成。系统已保留本地安全底稿。",
                config,
                model=model,
            ) from exc

    def test_connection(self, config: LLMConfig) -> str:
        endpoint, model = self.prepare_request(config)
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "请只回复：连接成功"}],
            "temperature": 0,
            "max_tokens": 20,
        }
        return self.post_chat_completions(endpoint, config.api_key or "", payload, timeout=20)

    def prepare_request(self, config: LLMConfig) -> tuple[str, str]:
        api_key = (config.api_key or "").strip()
        if not api_key:
            raise self.error(
                "CONFIG_MISSING",
                "大模型配置缺少 API Key。",
                "请在高级设置中填写 API Key，或关闭大模型增强使用本地整理模式。",
                config,
            )
        base_url = normalize_base_url(config.base_url or self.default_base_url, needs_v1=self.needs_v1_suffix)
        model = (config.model or self.default_model).strip()
        if not model:
            raise self.error("CONFIG_MISSING", "大模型配置缺少模型名称。", self.model_suggestion(), config)
        return f"{base_url}/chat/completions", model

    def post_chat_completions(self, endpoint: str, api_key: str, payload: dict, *, timeout: int) -> str:
        return self.post_chat_result(endpoint, api_key, payload, timeout=timeout).content

    def post_chat_result(self, endpoint: str, api_key: str, payload: dict, *, timeout: int) -> LLMChatResult:
        try:
            response = httpx.post(
                endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                timeout=timeout,
            )
            if response.status_code >= 400:
                raise self.http_error(response, payload.get("model"))
            data = response.json()
            choice = data["choices"][0]
            content = extract_chat_content(choice["message"].get("content"))
            finish_reason = choice.get("finish_reason")
            stop_reason = choice.get("stop_reason") or data.get("stop_reason")
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            diagnostic(
                "llm_provider_response",
                provider=self.display_name,
                model=str(payload.get("model") or ""),
                http_status=response.status_code,
                finish_reason=finish_reason,
                stop_reason=stop_reason,
                returned_character_count=len(content or ""),
                returned_token_usage=usage,
            )
            if finish_reason in {"length", "max_tokens", "max_output_tokens"} or stop_reason in {
                "length",
                "max_tokens",
                "max_output_tokens",
            }:
                raise self.error(
                    "LLM_OUTPUT_TRUNCATED",
                    "The model output reached its limit and was truncated.",
                    "RecallForge will reduce the current unit output and retry a bounded number of times.",
                    None,
                    model=str(payload.get("model") or ""),
                    http_status=response.status_code,
                )
            return LLMChatResult(
                content=content,
                finish_reason=finish_reason,
                stop_reason=stop_reason,
                usage=usage,
                http_status=response.status_code,
            )
        except LLMProviderError:
            raise
        except httpx.TimeoutException as exc:
            raise self.error(
                "TIMEOUT",
                "大模型请求超时。",
                "请稍后重试，或检查网络、代理和服务商状态。系统已保留本地安全底稿。",
                None,
                model=str(payload.get("model") or ""),
            ) from exc
        except httpx.NetworkError as exc:
            raise self.error(
                "NETWORK_ERROR",
                "无法连接到大模型服务。",
                "请检查 Base URL、网络连接、代理设置和服务商状态。",
                None,
                model=str(payload.get("model") or ""),
            ) from exc
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise self.error(
                "RESPONSE_PARSE_ERROR",
                "大模型服务返回格式不符合预期。",
                "请重试，或切换服务商和模型后再试。",
                None,
                model=str(payload.get("model") or ""),
            ) from exc
        except Exception as exc:
            raise self.error(
                "UNKNOWN_ERROR",
                "调用大模型时发生未知错误。",
                "请查看后端日志中的错误摘要，并检查服务商配置。",
                None,
                model=str(payload.get("model") or ""),
            ) from exc

    def http_error(self, response: httpx.Response, model: object) -> LLMProviderError:
        status = response.status_code
        message = safe_provider_message(response.text)
        if status in {401, 403}:
            code = "AUTH_FAILED"
            user_message = "认证失败。请确认 API Key 是否有效，或当前服务商是否要求不同的 Base URL。"
            suggestion = self.auth_suggestion()
        elif status == 404:
            code = "MODEL_NOT_FOUND"
            user_message = "大模型调用失败：模型名称可能不存在或当前账号无权限。"
            suggestion = self.model_suggestion()
        elif status == 429:
            quota_exceeded = bool(re.search(r"quota|balance|insufficient|余额|额度", message, re.I))
            code = "PROVIDER_QUOTA_EXCEEDED" if quota_exceeded else "RATE_LIMITED"
            user_message = "大模型服务额度不足。" if quota_exceeded else "大模型服务触发限流。"
            suggestion = (
                "请检查服务商余额或额度，已完成章节仍会保留。"
                if quota_exceeded
                else "请稍后重试，或检查当前服务商的额度和并发限制。"
            )
        elif status in {400, 413} and re.search(r"context|token|length|too long", message, re.I):
            code = "CONTEXT_TOO_LONG"
            user_message = "模型服务明确返回上下文过长。"
            suggestion = CONTEXT_TOO_LONG_MESSAGE
        elif status >= 500:
            code = "LLM_PROVIDER_ERROR"
            user_message = f"大模型服务暂时不可用（HTTP {status}）。"
            suggestion = "系统将有限重试；已完成章节会从 checkpoint 继续。"
        else:
            code = "UNKNOWN_ERROR"
            user_message = f"大模型服务返回 HTTP {status}。"
            suggestion = f"请检查服务商控制台、模型名称和 Base URL。错误摘要：{message[:160]}"
        return self.error(code, user_message, suggestion, None, model=str(model or ""), http_status=status)

    def error(
        self,
        code: str,
        message: str,
        suggestion: str,
        config: LLMConfig | None,
        *,
        model: str | None = None,
        http_status: int | None = None,
    ) -> LLMProviderError:
        error = LLMErrorInfo(
            code=code,  # type: ignore[arg-type]
            message=message,
            suggestion=suggestion,
            provider=self.display_name,
            model=model or ((config.model if config else None) or self.default_model),
            can_retry=code not in {"CONFIG_MISSING", "AUTH_FAILED", "MODEL_NOT_FOUND", "PROVIDER_QUOTA_EXCEEDED"},
            fallback_used=True,
        )
        return LLMProviderError(error, http_status=http_status)

    def model_suggestion(self) -> str:
        return "请检查模型名称，例如 deepseek-v4-flash、gpt-4o-mini 或 qwen-plus。"

    def auth_suggestion(self) -> str:
        return "请确认 API Key、Base URL 和账号权限是否匹配。"


class CustomOpenAICompatibleLLMProvider(OpenAICompatibleLLMProvider):
    name = "custom_openai_compatible"
    display_name = "OpenAI-compatible 自定义接口"
    default_base_url = ""
    default_model = ""

    def prepare_request(self, config: LLMConfig) -> tuple[str, str]:
        if not (config.base_url or "").strip():
            raise self.error("CONFIG_MISSING", "自定义接口缺少 Base URL。", "请填写兼容 OpenAI Chat Completions 的接口地址。", config)
        return super().prepare_request(config)


class DeepSeekLLMProvider(OpenAICompatibleLLMProvider):
    name = "deepseek"
    display_name = "DeepSeek"
    default_base_url = "https://api.deepseek.com"
    default_model = "deepseek-v4-flash"

    def model_suggestion(self) -> str:
        return "当前 DeepSeek 模型名称可能不可用。建议尝试 deepseek-v4-flash，或检查 DeepSeek 控制台当前支持的模型名称。"

    def auth_suggestion(self) -> str:
        return "如果你使用 DeepSeek，建议 Base URL 填写 https://api.deepseek.com，模型名称填写 deepseek-v4-flash。"


class QwenLLMProvider(OpenAICompatibleLLMProvider):
    name = "qwen"
    display_name = "通义千问 / Qwen"
    default_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    default_model = "qwen-plus"


def normalize_base_url(base_url: str, *, needs_v1: bool = True) -> str:
    cleaned = (base_url or "").strip().rstrip("/")
    cleaned = re.sub(r"/chat/completions/?$", "", cleaned)
    if needs_v1 and not re.search(r"/v1$", cleaned):
        cleaned = f"{cleaned}/v1"
    return cleaned


def extract_chat_content(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "".join(parts)
    raise TypeError("LLM message content is not text.")


def response_likely_truncated(result: LLMChatResult, configured_max_output: int) -> bool:
    if result.finish_reason in {"length", "max_tokens", "max_output_tokens"}:
        return True
    if result.stop_reason in {"length", "max_tokens", "max_output_tokens"}:
        return True
    completion_tokens = result.usage.get("completion_tokens") or result.usage.get("output_tokens")
    if isinstance(completion_tokens, (int, float)) and completion_tokens >= configured_max_output * 0.98:
        return True
    content = result.content.strip()
    if not content.startswith("{"):
        return False
    if content.count("{") > content.count("}") or content.count("[") > content.count("]"):
        return True
    unescaped_quotes = len(re.findall(r'(?<!\\)"', content))
    return bool(unescaped_quotes % 2)


def tolerant_parse_llm_report(raw_text: str) -> dict:
    content = (raw_text or "").lstrip("\ufeff").strip()
    if not content:
        raise ValueError("LLM response is empty.")

    candidates = [content]
    candidates.extend(extract_fenced_json_candidates(content))
    if "{" in content and "}" in content:
        start = content.find("{")
        end = content.rfind("}")
        if end > start:
            candidates.append(content[start : end + 1])

    for candidate in candidates:
        for cleaned in json_cleanup_variants(candidate):
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                return data

    if looks_like_complete_markdown_report(content):
        return markdown_report_to_payload(content)

    logger.warning("LLM response parse failed. returned_chars=%s", len(content))
    raise ValueError("LLM response is neither valid JSON nor usable Markdown.")


def extract_json_object(content: str) -> dict:
    return tolerant_parse_llm_report(content)


def extract_fenced_json_candidates(text: str) -> list[str]:
    return [
        match.group(1).strip()
        for match in re.finditer(r"```(?:json|JSON)?\s*([\s\S]*?)\s*```", text)
        if "{" in match.group(1) and "}" in match.group(1)
    ]


def json_cleanup_variants(text: str) -> list[str]:
    cleaned = text.strip().lstrip("\ufeff")
    cleaned = re.sub(r"^```(?:json|JSON)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    variants = [cleaned]

    without_trailing_commas = re.sub(r",\s*([}\]])", r"\1", cleaned)
    variants.append(without_trailing_commas)

    quote_fixed = without_trailing_commas.replace("“", '"').replace("”", '"').replace("＂", '"')
    quote_fixed = quote_fixed.replace("‘", "'").replace("’", "'")
    variants.append(quote_fixed)

    variants.append(escape_newlines_inside_json_strings(quote_fixed))
    return dedupe_strings(variants)


def escape_newlines_inside_json_strings(text: str) -> str:
    output: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if char == '"' and not escaped:
            in_string = not in_string
        if char in {"\n", "\r"} and in_string:
            output.append("\\n")
        else:
            output.append(char)
        escaped = char == "\\" and not escaped
        if char != "\\":
            escaped = False
    return "".join(output)


def looks_like_complete_markdown_report(text: str) -> bool:
    if len(text.strip()) < 300:
        return False
    heading_count = len(re.findall(r"^#{1,3}\s+", text, re.M))
    required_hits = sum(
        1
        for pattern in [
            r"复习导览|总览|Overview",
            r"知识结构|章节|专题",
            r"高频考点|重点",
            r"题型|题目|模拟",
            r"答案|解析",
            r"Anki|卡片",
            r"冲刺|计划",
        ]
        if re.search(pattern, text, re.I)
    )
    return heading_count >= 2 and required_hits >= 5


def markdown_report_to_payload(markdown: str) -> dict:
    title = extract_markdown_title(markdown) or "期末复习资料包"
    cards = extract_anki_cards_from_markdown(markdown)
    questions = extract_mock_questions_from_markdown(markdown)
    return {
        "title": title,
        "summary": extract_markdown_summary(markdown),
        "overview": {"material_summary": extract_markdown_summary(markdown)},
        "study_units": [],
        "question_types": [],
        "past_exam_analysis": {"detected_files": [], "high_frequency_topics": [], "summary": "AI 返回 Markdown 报告，已按兼容模式处理。"},
        "review_order": [],
        "sprint_plans": [],
        "mock_exam": {"title": "模拟卷", "questions": questions},
        "anki_cards": cards,
        "high_frequency_points": extract_markdown_bullets(markdown, r"高频考点|重点"),
        "sprint_checklist": extract_markdown_bullets(markdown, r"冲刺|计划"),
        "low_priority": [],
        "insufficient_materials": [],
        "markdown": markdown.strip(),
        "_raw_markdown_fallback": True,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def extract_markdown_title(markdown: str) -> str:
    match = re.search(r"^#\s+(.+)$", markdown, re.M)
    return match.group(1).strip() if match else ""


def extract_markdown_summary(markdown: str) -> str:
    body = re.sub(r"^#{1,6}\s+.*$", "", markdown, flags=re.M).strip()
    body = re.sub(r"\s+", " ", body)
    return body[:240] or "大模型返回了 Markdown 复习报告，系统已完成兼容处理。"


def extract_markdown_bullets(markdown: str, heading_pattern: str) -> list[str]:
    section = extract_section(markdown, heading_pattern)
    bullets = re.findall(r"^\s*[-*]\s+(.+)$", section, re.M)
    if bullets:
        return [item.strip() for item in bullets[:20]]
    lines = [line.strip() for line in section.splitlines() if 4 <= len(line.strip()) <= 80 and not line.startswith("#")]
    return lines[:12]


def extract_anki_cards_from_markdown(markdown: str) -> list[dict]:
    section = extract_section(markdown, r"Anki|卡片")
    cards: list[dict] = []
    qa_pattern = re.compile(r"(?:Q|问|正面)[:：]\s*(.+?)\s*(?:A|答|背面)[:：]\s*(.+?)(?=\n\s*(?:[-*]?\s*)?(?:Q|问|正面)[:：]|\Z)", re.S)
    for match in qa_pattern.finditer(section):
        cards.append({"front": compact_text(match.group(1)), "back": compact_text(match.group(2)), "tags": "AI_Markdown"})
    if cards:
        return cards[:30]
    bullets = re.findall(r"^\s*[-*]\s+(.+?)[：:]\s*(.+)$", section, re.M)
    return [{"front": compact_text(front), "back": compact_text(back), "tags": "AI_Markdown"} for front, back in bullets[:30]]


def extract_mock_questions_from_markdown(markdown: str) -> list[dict]:
    section = extract_section(markdown, r"模拟|题目|练习")
    questions: list[dict] = []
    pattern = re.compile(r"(?:题目?|Q)\s*\d*[:：]\s*(.+?)\s*(?:答案|参考答案|A)[:：]\s*(.+?)(?=\n\s*(?:题目?|Q)\s*\d*[:：]|\Z)", re.S)
    for index, match in enumerate(pattern.finditer(section), start=1):
        questions.append(
            {
                "question_type": "AI Markdown 练习题",
                "difficulty": "中等",
                "question": compact_text(match.group(1)),
                "answer": compact_text(match.group(2)),
                "explanation": "",
                "chapter": "",
                "concept": "",
            }
        )
    return questions[:30]


def extract_section(markdown: str, heading_pattern: str) -> str:
    match = re.search(rf"^#{1,3}\s+.*(?:{heading_pattern}).*$", markdown, re.I | re.M)
    if not match:
        return markdown
    start = match.end()
    next_heading = re.search(r"^#{1,3}\s+", markdown[start:], re.M)
    end = start + next_heading.start() if next_heading else len(markdown)
    return markdown[start:end]


def normalize_review_payload(data: dict) -> None:
    data.setdefault("title", "期末复习资料包")
    overview = data.get("overview") if isinstance(data.get("overview"), dict) else {}
    data["overview"] = overview
    data.setdefault("summary", overview.get("material_summary", "已根据材料生成复习资料包。"))
    data.setdefault("generated_at", datetime.now().isoformat(timespec="seconds"))
    data.setdefault("study_units", [])
    data.setdefault("question_types", [])
    data.setdefault("past_exam_analysis", {"detected_files": [], "high_frequency_topics": [], "summary": ""})
    data.setdefault("review_order", [])
    data.setdefault("sprint_plans", [])
    data.setdefault("mock_exam", {"title": "模拟卷", "questions": []})
    data.setdefault("anki_cards", [])
    data.setdefault("high_frequency_points", [])
    data.setdefault("sprint_checklist", [])
    data.setdefault("low_priority", [])
    data.setdefault("insufficient_materials", [])
    data.setdefault("markdown", "")

    if isinstance(data.get("mock_exam"), list):
        data["mock_exam"] = {"title": "模拟卷", "questions": data["mock_exam"]}
    if isinstance(data.get("sprint_plan"), dict) and not data.get("sprint_plans"):
        data["sprint_plans"] = sprint_plan_dict_to_list(data["sprint_plan"])

    normalize_study_units(data)
    normalize_question_types(data)
    normalize_mock_exam(data)
    normalize_anki_cards(data)
    normalize_v030_fields(data)
    sanitize_display_fields(data)


def normalize_study_units(data: dict) -> None:
    units = data.get("study_units") or []
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            continue
        unit.setdefault("name", f"复习单元 {index + 1}")
        unit.setdefault("reason", "")
        unit["priority"] = clamp_int(unit.get("priority", unit.get("importance", 50)), 0, 100)
        unit["must_know"] = listify(unit.get("must_know"))
        unit["key_points"] = listify(unit.get("key_points"))
        unit["formulas_or_methods"] = listify(unit.get("formulas_or_methods"))
        unit["common_exam_angles"] = listify(unit.get("common_exam_angles"))
        unit["pitfalls"] = listify(unit.get("pitfalls"))
        unit.setdefault("how_to_review", "")

    data["study_units"] = [unit for unit in units if isinstance(unit, dict)]
    if not data.get("chapters") and data["study_units"]:
        data["chapters"] = [
            {
                "chapter": unit["name"],
                "importance": unit["priority"],
                "material_frequency": 0,
                "past_exam_frequency": 0,
                "weighted_score": unit["priority"],
                "keywords": listify(unit.get("key_points"))[:12] or listify(unit.get("must_know"))[:12],
                "formulas": listify(unit.get("formulas_or_methods"))[:8],
                "question_types": listify(unit.get("common_exam_angles"))[:6],
                "examples": listify(unit.get("must_know"))[:5],
                "frequency": 0,
                "review_advice": unit.get("how_to_review") or unit.get("reason") or "结合材料证据复习本单元的核心概念和典型题。",
            }
            for unit in data["study_units"]
        ]

    for chapter in data.get("chapters", []) or []:
        chapter.setdefault("chapter", "复习单元")
        chapter["importance"] = clamp_int(chapter.get("importance", 50), 0, 100)
        chapter["material_frequency"] = max(0, clamp_int(chapter.get("material_frequency", 0), 0, 100000))
        chapter["past_exam_frequency"] = max(0, clamp_int(chapter.get("past_exam_frequency", 0), 0, 100000))
        chapter["weighted_score"] = clamp_int(chapter.get("weighted_score", chapter["importance"]), 0, 100)
        chapter["question_types"] = [str(item).strip() for item in listify(chapter.get("question_types")) if str(item).strip()]
        chapter["keywords"] = [str(item).strip() for item in listify(chapter.get("keywords")) if str(item).strip()]
        chapter["formulas"] = [str(item).strip() for item in listify(chapter.get("formulas")) if str(item).strip()]
        chapter["examples"] = [str(item).strip() for item in listify(chapter.get("examples")) if str(item).strip()]
        chapter.setdefault("frequency", 0)
        chapter.setdefault("review_advice", "结合材料证据复习本单元的核心概念和典型题。")

    if not data.get("review_order") and data.get("chapters"):
        data["review_order"] = [
            {
                "chapter": item["chapter"],
                "importance": item["importance"],
                "reason": item.get("review_advice", "根据材料证据和题目线索排序。"),
            }
            for item in sorted(data["chapters"], key=lambda value: value.get("importance", 0), reverse=True)[:12]
        ]


def normalize_question_types(data: dict) -> None:
    normalized = []
    for item in data.get("question_types", []) or []:
        if isinstance(item, str):
            normalized.append(
                {
                    "name": item,
                    "evidence": "",
                    "features": [],
                    "related_topics": [],
                    "answer_strategy": "",
                    "sample_questions": [],
                }
            )
        elif isinstance(item, dict):
            item.setdefault("name", "综合题型")
            item["features"] = listify(item.get("features"))
            item["related_topics"] = listify(item.get("related_topics"))
            item["sample_questions"] = listify(item.get("sample_questions"))
            item.setdefault("evidence", "")
            item.setdefault("answer_strategy", "")
            normalized.append(item)
    data["question_types"] = normalized


def normalize_mock_exam(data: dict) -> None:
    mock = data.get("mock_exam") or {}
    mock.setdefault("title", "模拟卷")
    questions = []
    for item in mock.get("questions", []) or []:
        if not isinstance(item, dict):
            continue
        item.setdefault("question_type", item.get("type", "综合题"))
        item.setdefault("question", "")
        item.setdefault("answer", "")
        item.setdefault("chapter", item.get("topic", ""))
        item.setdefault("concept", item.get("concept", item.get("chapter", "")))
        item.setdefault("explanation", "")
        item.setdefault("difficulty", "中等")
        questions.append(item)
    mock["questions"] = questions
    data["mock_exam"] = mock


def normalize_anki_cards(data: dict) -> None:
    cards = []
    for item in data.get("anki_cards", []) or []:
        if not isinstance(item, dict):
            continue
        tags = item.get("tags", "")
        if isinstance(tags, list):
            tags = " ".join(str(tag).strip().replace(" ", "_") for tag in tags if str(tag).strip())
        cards.append(
            {
                "front": str(item.get("front", "")).strip(),
                "back": str(item.get("back", "")).strip(),
                "tags": str(tags).strip(),
            }
        )
    data["anki_cards"] = cards


def normalize_v030_fields(data: dict) -> None:
    data.setdefault("study_goal", "balanced")
    data.setdefault("exam_type", "unknown")
    data.setdefault("detail_level", "detailed")
    data.setdefault("output_style", "teaching_assistant")
    for item in data.get("question_types", []) or []:
        if not isinstance(item, dict):
            continue
        item["confidence"] = clamp_int(item.get("confidence", 70), 0, 100)
        evidence_sources = listify(item.get("evidence_sources"))
        evidence = item.get("evidence", "")
        if isinstance(evidence, list):
            evidence_sources.extend(evidence)
            evidence = "；".join(str(part) for part in evidence[:3])
        item["evidence"] = str(evidence or "").strip()
        item["evidence_sources"] = [str(part).strip() for part in evidence_sources if str(part).strip()][:8]
        item.setdefault("practice_suggestions", "")
        item["is_from_past_exam"] = bool(item.get("is_from_past_exam", False))
    for item in (data.get("mock_exam") or {}).get("questions", []) or []:
        if not isinstance(item, dict):
            continue
        item.setdefault("type", item.get("question_type", "综合题"))
        item["options"] = [str(part).strip() for part in listify(item.get("options")) if str(part).strip()]
        item.setdefault("related_topic", item.get("concept") or item.get("chapter", ""))
        item.setdefault("source_hint", "来自材料证据和题型线索")
        item.setdefault("source_basis", item.get("source_hint", "来自材料证据"))
    seen_cards: set[str] = set()
    cards = []
    for item in data.get("anki_cards", []) or []:
        if not isinstance(item, dict):
            continue
        front = str(item.get("front", "")).strip()
        back = str(item.get("back", "")).strip()
        if not front or not back or front in seen_cards:
            continue
        seen_cards.add(front)
        item["front"] = front[:160]
        item["back"] = back
        item.setdefault("card_type", "definition")
        item["priority"] = clamp_int(item.get("priority", 60), 0, 100)
        item.setdefault("source_hint", "")
        cards.append(item)
    data["anki_cards"] = cards


def sanitize_display_fields(data: dict) -> None:
    for index, unit in enumerate(data.get("study_units", []) or [], start=1):
        if not isinstance(unit, dict):
            continue
        unit["name"] = clean_topic_name(str(unit.get("name") or "")) or f"核心专题 {index}"
        unit["must_know"] = clean_topic_list([str(item) for item in listify(unit.get("must_know"))], 12)
        unit["key_points"] = clean_topic_list([str(item) for item in listify(unit.get("key_points"))], 12)
        unit["common_exam_angles"] = clean_topic_list([str(item) for item in listify(unit.get("common_exam_angles"))], 12)
        unit["pitfalls"] = clean_topic_list([str(item) for item in listify(unit.get("pitfalls"))], 12)
        unit["formulas_or_methods"] = [
            clean_formula_text(str(item))
            for item in listify(unit.get("formulas_or_methods"))
            if clean_formula_text(str(item))
        ]

    for index, chapter in enumerate(data.get("chapters", []) or [], start=1):
        if not isinstance(chapter, dict):
            continue
        chapter["chapter"] = clean_topic_name(str(chapter.get("chapter") or "")) or f"核心专题 {index}"
        chapter["keywords"] = clean_topic_list([str(item) for item in listify(chapter.get("keywords"))], 12)
        chapter["question_types"] = clean_topic_list([str(item) for item in listify(chapter.get("question_types"))], 8)
        chapter["formulas"] = [
            clean_formula_text(str(item))
            for item in listify(chapter.get("formulas"))
            if clean_formula_text(str(item))
        ]

    for index, item in enumerate(data.get("review_order", []) or [], start=1):
        if isinstance(item, dict):
            item["chapter"] = clean_topic_name(str(item.get("chapter") or "")) or f"核心专题 {index}"

    data["high_frequency_points"] = clean_topic_list([str(item) for item in data.get("high_frequency_points", []) or []], 20)

    past_exam = data.get("past_exam_analysis")
    if isinstance(past_exam, dict):
        cleaned_topics = []
        for topic in past_exam.get("high_frequency_topics", []) or []:
            if not isinstance(topic, dict):
                continue
            topic_name = clean_topic_name(str(topic.get("topic") or ""))
            if not topic_name:
                continue
            topic["topic"] = topic_name
            topic["chapter"] = clean_topic_name(str(topic.get("chapter") or "")) or "核心专题"
            topic["question_types"] = clean_topic_list([str(item) for item in listify(topic.get("question_types"))], 6)
            topic["keywords"] = clean_topic_list([str(item) for item in listify(topic.get("keywords"))], 8)
            cleaned_topics.append(topic)
        past_exam["high_frequency_topics"] = cleaned_topics

    for item in data.get("question_types", []) or []:
        if not isinstance(item, dict):
            continue
        item["name"] = clean_topic_name(str(item.get("name") or "")) or "综合题型"
        item["related_topics"] = clean_topic_list([str(part) for part in listify(item.get("related_topics"))], 10)


def sprint_plan_dict_to_list(plan: dict) -> list[dict]:
    mapping = [("one_day", 1, "1 天冲刺计划"), ("three_days", 3, "3 天复习计划"), ("seven_days", 7, "7 天复习计划")]
    result = []
    for key, days, title in mapping:
        items = plan.get(key) or []
        result.append({"days": days, "title": title, "schedule": listify(items)})
    return result


def build_named_study_units(value: object) -> list[StudyUnit]:
    units: list[StudyUnit] = []
    for index, item in enumerate(listify(value), start=1):
        if not isinstance(item, dict):
            continue
        name = clean_unit_title(str(item.get("name") or ""))
        if is_bad_unit_title(name):
            continue
        priority = item.get("priority", 70)
        try:
            priority_int = max(0, min(100, int(priority)))
        except (TypeError, ValueError):
            priority_int = 70
        units.append(
            StudyUnit(
                name=name,
                reason=str(item.get("reason") or "根据材料证据整理出的复习专题。"),
                priority=priority_int,
                key_points=[str(part) for part in listify(item.get("key_points")) if str(part).strip()][:8],
                how_to_review=str(item.get("how_to_review") or "先掌握核心概念，再结合题目线索练习。"),
            )
        )
        if len(units) >= 12:
            break
    return units


def ensure_final_markdown(report: ReviewReport) -> ReviewReport:
    if not report.markdown.strip():
        report.markdown = generate_markdown_review(report, prefer_existing=False)
    return report


def fill_export_fallbacks(report: ReviewReport, safe_draft: ReviewReport) -> None:
    if not report.raw_markdown_fallback:
        return
    if not report.anki_cards:
        report.anki_cards = safe_draft.anki_cards
    if not report.mock_exam.questions:
        report.mock_exam = safe_draft.mock_exam
    if not report.chapters and not report.study_units:
        report.chapters = safe_draft.chapters
    if not report.review_order:
        report.review_order = safe_draft.review_order
    if not report.sprint_plans:
        report.sprint_plans = safe_draft.sprint_plans
    if not report.high_frequency_points:
        report.high_frequency_points = safe_draft.high_frequency_points


def local_chunk_insight(chunk: str, index: int, total: int) -> str:
    lines = [compact_text(line) for line in chunk.splitlines() if compact_text(line)]
    scored = sorted(lines, key=local_line_score, reverse=True)
    evidence = dedupe_strings([line for line in scored if len(line) >= 8][:10])
    questions = [
        line
        for line in evidence
        if re.search(r"\?|？|题|计算|证明|分析|解释|比较|选择|填空|answer|question", line, re.I)
    ][:6]
    definitions = [
        line
        for line in evidence
        if re.search(r"定义|是指|称为|defined as|refers to|means", line, re.I)
    ][:6]
    formulas = [
        clean_formula_text(line)
        for line in evidence
        if re.search(r"[=+\-*/∑√∫≥≤]|\bP\(|\bE\(|Var\(", line)
    ][:6]
    terms = dedupe_strings(re.findall(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9_-]{2,}", "\n".join(evidence)))[:12]
    return "\n".join(
        [
            f"本块主题：材料分块 {index}/{total}，关键词：{'、'.join(terms[:8]) or '未识别明确关键词'}",
            f"关键概念：{'；'.join(terms[:10]) or '请结合原文证据复习'}",
            f"重要定义：{'；'.join(definitions) or '未发现显式定义'}",
            f"公式/方法：{'；'.join([item for item in formulas if item]) or '未发现清晰公式'}",
            f"例题/题目候选：{'；'.join(questions) or '未发现清晰题干'}",
            f"可考点：{'；'.join(evidence[:5])}",
            "可能题型：根据本块概念、定义、公式和题干线索生成练习题型，不强制套固定题型。",
            f"可转 Anki 的问答：正面：本块核心概念是什么？ 背面：{'；'.join(evidence[:3])}",
            f"原文证据片段：{'；'.join(evidence[:6])}",
        ]
    )


def local_line_score(line: str) -> int:
    score = 0
    if re.search(r"题|答案|计算|证明|分析|解释|比较|选择|填空|question|answer", line, re.I):
        score += 20
    if re.search(r"定义|公式|定理|性质|方法|步骤|重点|考点|definition|formula", line, re.I):
        score += 16
    if re.search(r"[=+\-*/∑√∫≥≤]|\bP\(|\bE\(|Var\(", line):
        score += 12
    if 20 <= len(line) <= 180:
        score += 8
    return score


def listify(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def clamp_int(value: object, low: int, high: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text)).strip()


def dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def safe_provider_message(text: str) -> str:
    if not text:
        return ""
    redacted = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer ***", text)
    redacted = re.sub(r"sk-[A-Za-z0-9_-]+", mask_api_key, redacted)
    return redacted[:300]


def mask_api_key(match: re.Match[str]) -> str:
    value = match.group(0)
    return f"{value[:3]}****{value[-4:]}" if len(value) > 8 else "***"
