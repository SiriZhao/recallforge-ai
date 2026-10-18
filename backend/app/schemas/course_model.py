from __future__ import annotations
from pydantic import BaseModel, Field
from typing import Literal

class SourceRef(BaseModel):
    document_id: str = ""
    filename: str = ""
    block_id: str | None = None
    text_start: int | None = Field(default=None, ge=0)
    text_end: int | None = Field(default=None, ge=0)
    page: int | None = None
    slide: int | None = None
    section: str | None = None
    fragment: str = ""
    anchor: str = ""

class Concept(BaseModel):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    definition: str = ""
    plain_explanation: str = ""
    importance: int = Field(50, ge=0, le=100)
    difficulty: int = Field(50, ge=0, le=100)
    prerequisites: list[str] = Field(default_factory=list)
    related_concepts: list[str] = Field(default_factory=list)
    common_confusions: list[str] = Field(default_factory=list)
    formula_refs: list[str] = Field(default_factory=list)
    example_refs: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    confidence: float = Field(0.0, ge=0, le=1)
    basis: Literal["OBSERVED", "INFERRED", "INSUFFICIENT"] = "INFERRED"
    conditions: list[str] = Field(default_factory=list)
    mechanism: str = ""
    examples: list[str] = Field(default_factory=list)

class Relationship(BaseModel):
    source: str
    target: str
    type: Literal['prerequisite','is_a','part_of','causes','derived_from','contrasts_with','used_for','example_of','depends_on']
    confidence: float = Field(0.0, ge=0, le=1)
    source_refs: list[SourceRef] = Field(default_factory=list)

class CourseModel(BaseModel):
    schema_version: Literal['4.0'] = '4.0'
    course_title: str = ''
    document_type: str = 'unknown'
    subject_domain: str = 'other'
    estimated_level: str = 'undergraduate'
    chapters: list[dict] = Field(default_factory=list)
    concepts: list[Concept] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    formulas: list[dict] = Field(default_factory=list)
    examples: list[dict] = Field(default_factory=list)
    learning_objectives: list[dict] = Field(default_factory=list)
    exam_signals: list[dict] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    concept_id_aliases: dict[str, str] = Field(default_factory=dict)


class ChunkUnderstanding(BaseModel):
    """Evidence-oriented interpretation of one infrastructure chunk.

    This is deliberately permissive: chunks are for understanding and provenance,
    not for forcing the final presentation into a fixed StudyUnit shape.
    """
    chunk_id: str
    source_refs: list[SourceRef] = Field(default_factory=list)
    major_topics: list[str] = Field(default_factory=list)
    concepts: list[dict | str] = Field(default_factory=list)
    definitions: list[str] = Field(default_factory=list)
    formulas: list[str] = Field(default_factory=list)
    mechanisms: list[str] = Field(default_factory=list)
    methods: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    experiments: list[str] = Field(default_factory=list)
    comparisons: list[str] = Field(default_factory=list)
    exceptions: list[str] = Field(default_factory=list)
    misconceptions: list[str] = Field(default_factory=list)
    exam_signals: list[str] = Field(default_factory=list)
    actual_question_evidence: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    unresolved_points: list[str] = Field(default_factory=list)
    semantic_summary: str = ""


class UnitPlan(BaseModel):
    unit_id: str = ""
    title: str
    knowledge_ids: list[str] = Field(default_factory=list)
    source_regions: list[str] = Field(default_factory=list)
    priority: int = Field(default=50,ge=0,le=100)
    rationale: str = ""
    estimated_minutes: int = Field(default=15,ge=1,le=960)


class StudyBlueprint(BaseModel):
    schema_version: Literal["4.0"] = "4.0"
    intent: dict = Field(default_factory=dict)
    units: list[UnitPlan] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    omitted_artifacts: list[str] = Field(default_factory=list)
    rationale: str = ""


class UnitDraft(BaseModel):
    unit_id: str = Field(min_length=1)
    title: str
    markdown: str
    source_refs: list[SourceRef] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
    status: Literal["READY", "PARTIAL", "NEEDS_RETRY", "NEEDS_REPAIR", "FAILED"] = "READY"
