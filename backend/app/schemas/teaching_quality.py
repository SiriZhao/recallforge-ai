from typing import Literal
from pydantic import BaseModel, Field
from app.schemas.course_model import SourceRef


class ClaimAssessment(BaseModel):
    claim: str = Field(min_length=1)
    basis: Literal["SUPPORTED", "INFERRED", "GENERATED", "INSUFFICIENT"]
    source_refs: list[SourceRef] = Field(default_factory=list)


class TeachingFinding(BaseModel):
    category: Literal['factual_error','missing_knowledge','citation','extraction_noise','conditions','specificity','redundancy','scope','contradiction','wording']
    severity: Literal['info','minor','major','critical'] = 'major'
    message: str = Field(min_length=1)
    knowledge_ids: list[str] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    text_start: int | None = Field(default=None,ge=0)
    text_end: int | None = Field(default=None,ge=0)
    quote: str = ''


class UnitCritique(BaseModel):
    verdict: Literal["READY", "NEEDS_REPAIR", "PARTIAL", "FAILED"]
    issues: list[str] = Field(default_factory=list)
    claims: list[ClaimAssessment] = Field(min_length=1)
    missing_knowledge_ids: list[str] = Field(default_factory=list)
    findings: list[TeachingFinding] = Field(default_factory=list)
    # An LLM review is fallible; no metric here claims human validation.
    evaluator: Literal["model"] = "model"
