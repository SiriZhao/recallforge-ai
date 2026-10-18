"""Typed internal artifacts; legacy ReviewReport projections stay additive."""
from typing import Literal
from pydantic import BaseModel, Field
from app.schemas.course_model import CourseModel, SourceRef, StudyBlueprint, UnitDraft
from app.schemas.review import GeneratedExamQuestion, AnkiCard


class Question(GeneratedExamQuestion):
    question_id: str = Field(min_length=1)
    knowledge_ids: list[str] = Field(min_length=1)
    learning_objective: str = Field(min_length=1)
    scoring_rubric: list[str] = Field(min_length=1)
    common_errors: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(min_length=1)
    provenance: Literal["OBSERVED", "INFERRED", "GENERATED"] = "GENERATED"
    quality_status: Literal["VALIDATED", "QUARANTINED"] = "QUARANTINED"
    reference_expression: str | None = None
    numeric_answer: float | None = None
    quantity_kind: Literal["number", "probability"] = "number"
    requires_diagram: bool = False


class RecallCard(AnkiCard):
    card_id: str = Field(min_length=1)
    knowledge_ids: list[str] = Field(min_length=1)
    source_refs: list[SourceRef] = Field(min_length=1)


class KnowledgeGraph(BaseModel):
    nodes: list[dict] = Field(default_factory=list)
    edges: list[dict] = Field(default_factory=list)


class LearningMaterial(BaseModel):
    units: list[UnitDraft] = Field(default_factory=list)
    canonical_markdown: str = ""


class QuestionBank(BaseModel):
    questions: list[Question] = Field(default_factory=list)


class MockExam(BaseModel):
    title: str = "模拟考试"
    questions: list[Question] = Field(default_factory=list)


class RecallCardCollection(BaseModel):
    cards: list[RecallCard] = Field(default_factory=list)


class GeneratedArtifactBundle(BaseModel):
    practice_questions: list[Question] = Field(min_length=1)
    mock_exam: MockExam
    active_recall_cards: list[RecallCard] = Field(min_length=1)


class QualityReport(BaseModel):
    # Structural gates are not claims of factual correctness or learning efficacy.
    failures: list[str] = Field(default_factory=list)
    quarantined: list[dict] = Field(default_factory=list)
    factual_correctness: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"


class GenerationStatus(BaseModel):
    state: Literal["READY", "PARTIAL", "FAILED"] = "READY"
    stages: dict[str, str] = Field(default_factory=dict)


class LearningArtifacts(BaseModel):
    schema_version: str = "1.0"
    course_model: CourseModel
    study_blueprint: StudyBlueprint
    knowledge_graph: KnowledgeGraph
    learning_material: LearningMaterial
    question_bank: QuestionBank = Field(default_factory=QuestionBank)
    mock_exam: MockExam = Field(default_factory=MockExam)
    recall_cards: RecallCardCollection = Field(default_factory=RecallCardCollection)
    quality_report: QualityReport = Field(default_factory=QualityReport)
    generation_status: GenerationStatus = Field(default_factory=GenerationStatus)
