"""Resolve model references against extracted spans, not model-written citations."""
from pydantic import BaseModel, Field
from app.schemas.course_model import SourceRef
from app.services.stage_checkpoints import stable_hash


class EvidenceSpan(BaseModel):
    evidence_id: str
    source: SourceRef
    text: str
    content_hash: str
    confidence: float = Field(ge=0, le=1)
    extraction_warnings: list[str] = Field(default_factory=list)


def catalog_for(parsed_files):
    spans = []
    for parsed in parsed_files:
        doc_id = parsed.document_structure.document_id if parsed.document_structure else stable_hash([parsed.filename, parsed.raw_text])[:24]
        for page in parsed.pages:
            anchor = page.source_anchor or f"{parsed.filename}, p. {page.page_number}"
            parts = [(None, page.text, page.confidence)]
            parts += [(b.block_id, b.text or "\n".join(" | ".join(row) for row in b.rows), b.confidence) for b in page.blocks]
            for block_id, text, confidence in parts:
                if not text.strip():
                    continue
                ref = SourceRef(document_id=doc_id, filename=parsed.original_filename or parsed.filename,
                    page=None if parsed.file_type == ".pptx" else page.page_number,
                    slide=page.page_number if parsed.file_type == ".pptx" else None, block_id=block_id, anchor=anchor)
                spans.append(EvidenceSpan(evidence_id=stable_hash([doc_id, page.page_number, block_id, text])[:24],
                    source=ref, text=text, content_hash=stable_hash(text), confidence=confidence,
                    extraction_warnings=[page.warning] if page.warning else []))
    return spans


def resolves(ref: SourceRef, spans: list[EvidenceSpan]) -> bool:
    for span in spans:
        source = span.source
        if ref.anchor != source.anchor:
            continue
        if any(getattr(ref, field) not in (None, "", getattr(source, field)) for field in ("document_id", "filename", "page", "slide", "block_id")):
            continue
        if ref.fragment and ref.fragment not in span.text:
            continue
        if (ref.text_start is None) != (ref.text_end is None):
            continue
        if ref.text_start is not None:
            if not 0 <= ref.text_start < ref.text_end <= len(span.text):
                continue
            if ref.fragment and span.text[ref.text_start:ref.text_end] != ref.fragment:
                continue
        return True
    return False
