import json
import zipfile
import shutil
from pathlib import Path

from app.schemas.review import LLMConfig, ParsedFile, ParsedPage
from app.services import generation_pipeline as pipeline
from app.services.acceptance_quality import evaluate_canonical_markdown
from app.services.generation_pipeline import repair_unit_draft
from app.schemas.course_model import UnitDraft
from app.schemas.review import ReviewReport
from app.services.export_service import export_review_report

from tests.fixtures.llm_acceptance_fixtures import FIXTURES


def _parsed(items):
    return [ParsedFile(filename=name, file_type=".txt", path=name, raw_text=text, pages=[ParsedPage(page_number=1, text=text, source="text_extract", source_anchor=f"{name}, p. 1")]) for name, text in items]


class FixtureProvider:
    display_name = "fixture"
    default_model = "fixture-model"

    def __init__(self):
        self.calls = []

    def generate_stage(self, stage, prompt, config, *, max_output_tokens):
        payload = json.loads(prompt)
        self.calls.append((stage, payload))
        if stage == "chunk_understanding":
            chunk = payload["payload"]["chunk_id"]
            return {"major_topics": [chunk], "semantic_summary": f"understanding for {chunk}", "actual_question_evidence": []}
        if stage == "course_model":
            return {"course_title": "Fixture Course", "subject_domain": "other", "concepts": [], "relationships": []}
        if stage == "study_blueprint":
            intent = payload["payload"]["intent"]
            title = {"programming": "Trace and debug", "lab_exam": "Procedure and error", "unknown": "Cross-chapter concepts"}.get(intent["exam_type"], intent["study_goal"])
            return {"units": [{"title": title, "source_regions": ["all"]}], "intent": intent}
        if stage == "unit_draft":
            return {"title": "LLM-renamed cross-chapter unit", "markdown": "## LLM-renamed cross-chapter unit\nBecause the evidence connects chapters, derive the relationship with a concrete example."}
        if stage == "unit_repair":
            return {"title": "Repaired unit", "markdown": "## Repaired unit\nThe unsupported claim is removed; evidence shows the calibrated control changes the interpretation."}
        return {"canonical_markdown": "# Canonical review\n\n## LLM-renamed cross-chapter unit\nOBSERVED: the uploaded evidence states the mechanism.\n\nINFERRED: the dependency follows from those observations.\n\nGENERATED: practice question based on the evidence."}


def test_quality_fixtures_are_course_specific_and_mode_sensitive(monkeypatch):
    provider = FixtureProvider()
    monkeypatch.setattr(pipeline, "get_llm_provider", lambda _: provider)
    outputs = []
    for exam_type, key in [("programming", "programming"), ("lab_exam", "lab"), ("unknown", "concept")]:
        result, _ = pipeline.generate_hierarchical_report(_parsed(FIXTURES[key]), None, LLMConfig(enabled=True, provider="openai"), job_id=key, title=key, study_goal="practice_heavy", exam_type=exam_type, detail_level="detailed", output_style="practice_training")
        outputs.append(result.report.markdown)
    assert len(set(outputs)) == 3 or len({call[1]["payload"]["intent"]["exam_type"] for call in provider.calls if call[0] == "chunk_understanding"}) == 3
    assert any(call[0] == "study_blueprint" for call in provider.calls)


def test_long_fixture_retains_every_source_region(monkeypatch):
    provider = FixtureProvider()
    monkeypatch.setattr(pipeline, "get_llm_provider", lambda _: provider)
    result, stats = pipeline.generate_hierarchical_report(_parsed(FIXTURES["long"]), None, LLMConfig(enabled=True, provider="openai"), job_id="long", title="long")
    coverage = result.report.overview["source_coverage"]
    assert stats.chunk_count == 8
    assert coverage["chunks_total"] == 8
    assert coverage["chunks_understood"] == 8
    assert not coverage["unresolved_chunks"]


def test_blueprint_can_rename_merge_split_and_omit_irrelevant_modules(monkeypatch):
    class FreedomProvider(FixtureProvider):
        def generate_stage(self, stage, prompt, config, *, max_output_tokens):
            if stage == "study_blueprint":
                self.calls.append((stage, json.loads(prompt)))
                return {"units": [
                    {"title": "Merged foundations", "source_regions": ["chapter A", "chapter B"]},
                    {"title": "Oversized chapter: principles", "source_regions": ["chapter C:first half"]},
                    {"title": "Oversized chapter: applications", "source_regions": ["chapter C:second half"]},
                ]}
            return super().generate_stage(stage, prompt, config, max_output_tokens=max_output_tokens)

    provider = FreedomProvider()
    monkeypatch.setattr(pipeline, "get_llm_provider", lambda _: provider)
    result, _ = pipeline.generate_hierarchical_report(_parsed(FIXTURES["concept"]), None, LLMConfig(enabled=True, provider="openai"), job_id="freedom", title="Freedom", study_goal="balanced", exam_type="unknown")
    titles = [unit["title"] for unit in result.report.overview["blueprint"]["units"]]
    assert titles == ["Merged foundations", "Oversized chapter: principles", "Oversized chapter: applications"]
    # Blueprint freedom does not leave the other four result tabs unpopulated:
    # structured artifacts are requested independently of unit organization.
    assert "optional_artifacts" in [stage for stage, _ in provider.calls]


def test_targeted_unit_repair_does_not_rewrite_course(monkeypatch):
    provider = FixtureProvider()
    repaired = repair_unit_draft(provider, UnitDraft(unit_id="u1", title="Thin", markdown="generic", status="READY"), "unsupported claim", [{"semantic_summary": "calibrated control"}], LLMConfig(enabled=True, provider="openai"))
    assert repaired.title == "Repaired unit"
    assert "calibrated control" in repaired.markdown
    assert [stage for stage, _ in provider.calls] == ["unit_repair"]


def test_optional_artifacts_are_goal_driven_and_provenance_labeled(monkeypatch):
    provider = FixtureProvider()
    monkeypatch.setattr(pipeline, "get_llm_provider", lambda _: provider)
    result, _ = pipeline.generate_hierarchical_report(_parsed(FIXTURES["exam"]), None, LLMConfig(enabled=True, provider="openai"), job_id="practice", title="Exam evidence", study_goal="past_exam_focused", exam_type="closed_book")
    call = next(payload for stage, payload in provider.calls if stage == "optional_artifacts")
    assert set(call["payload"]["requested"]) == {"active_recall_cards", "practice_questions", "mock_exam"}
    assert "OBSERVED" in call["payload"]["instruction"]
    assert "INFERRED" in call["payload"]["instruction"]
    assert "GENERATED" in call["payload"]["instruction"]
    assert result.report.overview["optional_artifacts"]


def test_quality_rejects_repeated_generic_advice_and_accepts_grounded_content():
    quality = evaluate_canonical_markdown("# Review\n\n掌握重点。\n\n掌握重点。", ["Bayes"])
    assert not quality.acceptable
    grounded = evaluate_canonical_markdown("# Bayes\n\nBecause P(A|B)=P(B|A)P(A)/P(B), the denominator normalizes the posterior.", ["Bayes"])
    assert grounded.acceptable


def test_canonical_markdown_survives_all_document_exports():
    markdown = "# Canonical\n\n## Formula\n\n| Symbol | Meaning |\n| --- | --- |\n| beta | coefficient |\n\n```python\nprint(beta)\n```\n\n- Explanation with evidence"
    output = Path.cwd() / ".acceptance-export"
    output.mkdir(exist_ok=True)
    try:
        report = ReviewReport(title="Export fixture", summary="Canonical", markdown=markdown)
        md = export_review_report(report, markdown, output, "canonical", "md")
        docx = export_review_report(report, markdown, output, "canonical", "docx")
        pdf = export_review_report(report, markdown, output, "canonical", "pdf")
        assert md.read_text(encoding="utf-8") == markdown
        assert docx.stat().st_size > 0 and pdf.stat().st_size > 0
        with zipfile.ZipFile(docx) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
        assert "Canonical" in xml and "Formula" in xml and "beta" in xml
    finally:
        shutil.rmtree(output, ignore_errors=True)
