from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class UploadedFileInfo(BaseModel):
    original_filename: str
    saved_filename: str
    file_type: str
    file_size: int
    saved_path: str


class UploadResponse(BaseModel):
    files: list[UploadedFileInfo]
    message: str


OCRProvider = Literal[
    "rapidocr",
    "local_tesseract",
    "custom_api",
    "openai_vision",
    "baidu_ocr",
]


class OCRConfig(BaseModel):
    provider: OCRProvider = "rapidocr"
    mode: Literal["fast", "full"] = "fast"
    api_url: str | None = None
    api_key: str | None = None
    secret_key: str | None = None
    model: str | None = None
    language: str = "chi_sim+eng"


class ParseRequest(BaseModel):
    files: list[str]
    ocr_config: OCRConfig = Field(default_factory=OCRConfig)


DocumentBlockType = Literal["title", "text", "table", "image", "formula"]
ProcessingStatus = Literal["processed", "processed_with_warning", "skipped_with_reason", "failed_with_reason"]


class DocumentBlock(BaseModel):
    block_id: str
    type: DocumentBlockType
    page_number: int
    source_anchor: str
    text: str = ""
    rows: list[list[str]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=1.0, ge=0, le=1)


class DocumentSection(BaseModel):
    section_id: str
    title: str
    level: int = Field(default=1, ge=1, le=6)
    parent_id: str | None = None
    child_ids: list[str] = Field(default_factory=list)
    page_start: int
    page_end: int
    block_ids: list[str] = Field(default_factory=list)
    source_anchor: str


class DocumentStructure(BaseModel):
    schema_version: Literal["3.2"] = "3.2"
    document_id: str
    filename: str
    file_type: str
    title: str
    page_count: int
    sections: list[DocumentSection] = Field(default_factory=list)
    blocks: list[DocumentBlock] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ParsedPage(BaseModel):
    page_number: int
    text: str
    source: Literal["text_extract", "ocr_fallback"]
    warning: str | None = None
    title: str | None = None
    source_anchor: str | None = None
    status: ProcessingStatus = "processed"
    confidence: float = Field(default=1.0, ge=0, le=1)
    blocks: list[DocumentBlock] = Field(default_factory=list)


class ParsedFile(BaseModel):
    filename: str
    file_type: str
    path: str
    pages: list[ParsedPage]
    raw_text: str
    warnings: list[str] = Field(default_factory=list)
    ocr_cache_used: bool = False
    document_structure: DocumentStructure | None = None


class ParseResponse(BaseModel):
    files: list[ParsedFile]
    message: str


ExportFormat = Literal["md", "docx", "pdf"]
QuestionType = str
DifficultyLevel = str
StudyGoal = Literal[
    "one_day_sprint",
    "three_day_sprint",
    "seven_day_plan",
    "memorization",
    "practice_heavy",
    "anki_focused",
    "past_exam_focused",
    "balanced",
]
ExamType = Literal[
    "unknown",
    "closed_book",
    "open_book",
    "computer_based",
    "programming",
    "lab_exam",
    "essay_based",
    "oral_presentation",
    "coursework_report",
]
OptimizationGoal = Literal[
    "memorization",
    "practice",
    "anki",
    "concise",
    "detailed",
    "sprint",
    "exam_like",
    "fix_quality",
]
DetailLevel = Literal["concise", "standard", "detailed", "exhaustive"]
OutputStyle = Literal["sprint", "top_student_notes", "teaching_assistant", "practice_training", "anki_cards"]


class LLMConfig(BaseModel):
    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    enabled: bool = False


LLMErrorCode = Literal[
    "CONFIG_MISSING",
    "AUTH_FAILED",
    "NETWORK_ERROR",
    "TIMEOUT",
    "MODEL_NOT_FOUND",
    "RATE_LIMITED",
    "CONTEXT_TOO_LONG",
    "RESPONSE_PARSE_ERROR",
    "QUALITY_FAILED",
    "UNKNOWN_ERROR",
    "LLM_OUTPUT_TRUNCATED",
    "LLM_CONTEXT_EXCEEDED",
    "LLM_TIMEOUT",
    "LLM_PROVIDER_ERROR",
    "LLM_RESPONSE_PARSE_ERROR",
    "PROVIDER_RATE_LIMIT",
    "PROVIDER_QUOTA_EXCEEDED",
    "GENERATION_PERSIST_ERROR",
    "EXPORT_RENDER_ERROR",
    "EXPORT_FILE_WRITE_ERROR",
    "LLM_STAGE_FAILED",
    "INVALID_CANONICAL",
]

ReportSource = Literal[
    "rule_based",
    "local_safe_draft_with_ai_outline",
    "llm_enhanced",
    "llm_markdown_fallback",
    "rule_based_with_llm_failed",
]
LLMStatus = Literal["disabled", "success", "failed", "partial"]
LLMContextStrategy = Literal["disabled", "direct", "compressed", "chunked", "hierarchical", "failed"]


class LLMErrorInfo(BaseModel):
    code: LLMErrorCode
    message: str
    suggestion: str
    provider: str | None = None
    model: str | None = None
    can_retry: bool = True
    fallback_used: bool = False


class LLMTestRequest(LLMConfig):
    enabled: bool = True


class LLMTestResponse(BaseModel):
    ok: bool
    provider: str
    model: str
    message: str | None = None
    error: LLMErrorInfo | None = None


class GenerateReviewRequest(BaseModel):
    files: list[str]
    project_id: str | None = None
    export_format: ExportFormat = "md"
    export_formats: list[ExportFormat] | None = None
    title: str = "期末复习资料包"
    course_name: str | None = None
    study_goal: StudyGoal = "balanced"
    exam_type: ExamType = "unknown"
    detail_level: DetailLevel = "detailed"
    output_style: OutputStyle = "teaching_assistant"
    material_types: dict[str, str] = Field(default_factory=dict)
    exam_date: str = ""
    daily_minutes: int = Field(default=90, ge=15, le=960)
    mastery_level: str = "forgotten"
    target_score: str = ""
    focus: str = ""
    learning_mode: str = ""
    ocr_config: OCRConfig = Field(default_factory=OCRConfig)
    llm_config: LLMConfig = Field(default_factory=LLMConfig)
    enable_chunked_llm: bool = True
    retry_on_context_too_long: bool = True


class ExamQuestion(BaseModel):
    question: str
    question_type: QuestionType
    chapter: str | None = None
    difficulty: DifficultyLevel = "未知"
    keywords: list[str] = Field(default_factory=list)


class ChapterReview(BaseModel):
    chapter: str
    importance: int = Field(ge=0, le=100)
    material_frequency: int = Field(default=0, ge=0)
    past_exam_frequency: int = Field(default=0, ge=0)
    weighted_score: int = Field(default=0, ge=0, le=100)
    keywords: list[str] = Field(default_factory=list)
    formulas: list[str] = Field(default_factory=list)
    question_types: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    frequency: int = Field(ge=0)
    review_advice: str


class PastExamTopic(BaseModel):
    topic: str
    chapter: str
    frequency: int = Field(ge=0)
    question_types: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class PastExamFileAnalysis(BaseModel):
    filename: str
    confidence: int = Field(ge=0, le=100)
    question_count: int = Field(ge=0)
    question_types: list[str] = Field(default_factory=list)
    matched_chapters: list[str] = Field(default_factory=list)


class PastExamAnalysis(BaseModel):
    detected_files: list[PastExamFileAnalysis] = Field(default_factory=list)
    high_frequency_topics: list[PastExamTopic] = Field(default_factory=list)
    summary: str = ""


class ReviewPlanItem(BaseModel):
    chapter: str
    importance: int = Field(ge=0, le=100)
    reason: str


class SprintPlan(BaseModel):
    days: int
    title: str
    schedule: list[str] = Field(default_factory=list)


class GeneratedExamQuestion(BaseModel):
    question_type: str
    question: str
    answer: str
    chapter: str = ""
    concept: str = ""
    explanation: str = ""
    difficulty: str = "中等"
    type: str = ""
    options: list[str] = Field(default_factory=list)
    related_topic: str = ""
    source_hint: str = ""
    source_basis: str = ""


class GeneratedMockExam(BaseModel):
    title: str = "模拟卷"
    questions: list[GeneratedExamQuestion] = Field(default_factory=list)


class AnkiCard(BaseModel):
    front: str
    back: str
    tags: str
    card_type: str = "definition"
    priority: int = Field(default=60, ge=0, le=100)
    source_hint: str = ""


class StudyUnit(BaseModel):
    name: str
    reason: str = ""
    priority: int = Field(default=50, ge=0, le=100)
    must_know: list[str] = Field(default_factory=list)
    key_points: list[str] = Field(default_factory=list)
    formulas_or_methods: list[str] = Field(default_factory=list)
    common_exam_angles: list[str] = Field(default_factory=list)
    pitfalls: list[str] = Field(default_factory=list)
    how_to_review: str = ""


class QuestionTypeInsight(BaseModel):
    name: str
    confidence: int = Field(default=70, ge=0, le=100)
    evidence: str = ""
    evidence_sources: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    related_topics: list[str] = Field(default_factory=list)
    answer_strategy: str = ""
    sample_questions: list[str] = Field(default_factory=list)
    practice_suggestions: str = ""
    is_from_past_exam: bool = False


class ReportQuality(BaseModel):
    quality_score: int = Field(default=0, ge=0, le=100)
    material_completeness_score: int = Field(default=0, ge=0, le=100)
    topic_coverage_score: int = Field(default=0, ge=0, le=100)
    mock_exam_quality_score: int = Field(default=0, ge=0, le=100)
    anki_quality_score: int = Field(default=0, ge=0, le=100)
    export_readiness_score: int = Field(default=0, ge=0, le=100)
    evidence_integration_score: int = Field(default=0, ge=0, le=100)
    quality_warnings: list[str] = Field(default_factory=list)
    quality_failures: list[str] = Field(default_factory=list)
    repairable: bool = True
    section_coverage_ratio: float = Field(default=1.0, ge=0, le=1)
    missing_sections: list[str] = Field(default_factory=list)
    duplicate_sections: list[str] = Field(default_factory=list)
    is_empty: bool = False


class GenerationSummary(BaseModel):
    files_processed: int = 0
    pages_total: int = 0
    pages_text_extracted: int = 0
    pages_ocr_processed: int = 0
    pages_ocr_skipped: int = 0
    ocr_cache_hits: int = 0
    evidence_chunks: int = 0
    detected_study_units: int = 0
    detected_question_types: int = 0
    mock_questions_count: int = 0
    anki_cards_count: int = 0
    llm_calls: int = 0
    retry_count: int = 0
    chapter_count: int = 0
    chunk_count: int = 0
    fallback_used: bool = False
    final_report_source: ReportSource = "rule_based"
    notes: list[str] = Field(default_factory=list)


class ReviewReport(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str
    summary: str
    study_goal: StudyGoal = "balanced"
    exam_type: ExamType = "unknown"
    detail_level: DetailLevel = "detailed"
    output_style: OutputStyle = "teaching_assistant"
    overview: dict[str, Any] = Field(default_factory=dict)
    chapters: list[ChapterReview] = Field(default_factory=list)
    study_units: list[StudyUnit] = Field(default_factory=list)
    question_types: list[QuestionTypeInsight] = Field(default_factory=list)
    past_exam_analysis: PastExamAnalysis = Field(default_factory=PastExamAnalysis)
    review_order: list[ReviewPlanItem] = Field(default_factory=list)
    sprint_plans: list[SprintPlan] = Field(default_factory=list)
    mock_exam: GeneratedMockExam = Field(default_factory=GeneratedMockExam)
    practice_questions: list[GeneratedExamQuestion] = Field(default_factory=list)
    anki_cards: list[AnkiCard] = Field(default_factory=list)
    high_frequency_points: list[str] = Field(default_factory=list)
    sprint_checklist: list[str] = Field(default_factory=list)
    low_priority: list[str] = Field(default_factory=list)
    insufficient_materials: list[str] = Field(default_factory=list)
    quality: ReportQuality | None = None
    markdown: str = ""
    raw_markdown_fallback: bool = Field(default=False, alias="_raw_markdown_fallback")
    generated_at: str = Field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))


class GenerateReviewResponse(BaseModel):
    review_report: ReviewReport
    markdown: str
    download_path: str = ""
    download_links: dict[ExportFormat, str] = Field(default_factory=dict)
    anki_csv_download_path: str | None = None
    export_format: ExportFormat
    report_source: ReportSource = "rule_based"
    llm_status: LLMStatus = "disabled"
    fallback_used: bool = False
    llm_error: LLMErrorInfo | None = None
    llm_context_strategy: LLMContextStrategy = "disabled"
    generation_summary: GenerationSummary = Field(default_factory=GenerationSummary)


class ReoptimizeReviewRequest(BaseModel):
    current_report: ReviewReport
    evidence_text: str = ""
    optimization_goal: OptimizationGoal = "fix_quality"
    original_study_goal: StudyGoal = "balanced"
    original_exam_type: ExamType = "unknown"
    detail_level: DetailLevel = "detailed"
    output_style: OutputStyle = "teaching_assistant"
    llm_config: LLMConfig = Field(default_factory=LLMConfig)


class ReoptimizeReviewResponse(BaseModel):
    review_report: ReviewReport
    markdown: str
    optimized: bool
    message: str
    quality: ReportQuality


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str
    review_report: ReviewReport | None = None
    history: list[ChatMessage] = Field(default_factory=list)


class ChatResponse(BaseModel):
    reply: str


class MockExamQuestion(BaseModel):
    question: str
    answer: str
    chapter: str
    concept: str
    question_type: QuestionType


class GenerateMockExamRequest(BaseModel):
    review_report: ReviewReport
    count: int = Field(default=8, ge=1, le=30)


class GenerateMockExamResponse(BaseModel):
    questions: list[MockExamQuestion]
    message: str


class AnalyzeRequest(BaseModel):
    file_id: str
    focus: str | None = None


class TopicSummary(BaseModel):
    topic: str
    confidence: float
    notes: list[str]


class AnalyzeResponse(BaseModel):
    file_id: str
    summaries: list[TopicSummary]
    recommendations: list[str]


class ExportRequest(BaseModel):
    file_id: str
    format: str = "markdown"


class ExportResponse(BaseModel):
    file_id: str
    output_path: str
