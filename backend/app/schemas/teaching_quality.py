from typing import Literal
from pydantic import BaseModel, Field
from app.schemas.course_model import SourceRef


class ClaimAssessment(BaseModel):
    claim: str = Field(min_length=1)
    basis: Literal["SUPPORTED", "INFERRED", "GENERATED", "INSUFFICIENT"]
    source_refs: list[SourceRef] = Field(default_factory=list)


class UnitCritique(BaseModel):
    verdict: Literal["READY", "NEEDS_REPAIR", "PARTIAL", "FAILED"]
    issues: list[str] = Field(default_factory=list)
    claims: list[ClaimAssessment] = Field(min_length=1)
    missing_knowledge_ids: list[str] = Field(default_factory=list)
    # An LLM review is fallible; no metric here claims human validation.
    evaluator: Literal["model"] = "model"
