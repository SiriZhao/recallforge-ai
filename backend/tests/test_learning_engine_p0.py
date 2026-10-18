"""Production-boundary regressions. All model calls use synthetic, free fakes."""
import json
from datetime import date
from pathlib import Path

import pytest
from app.schemas.review import GenerateReviewRequest, LLMConfig, ParsedFile, ParsedPage
from app.services import generation_pipeline as pipeline, generation_store as store
from app.services.learning_context import learning_constraints


class GroundedProvider:
    display_name = "synthetic"
    default_model = "synthetic-model"

    def __init__(self, fail_unit=False, invalid_question=False):
        self.calls = []
        self.fail_unit = fail_unit
        self.invalid_question = invalid_question

    def generate_stage(self, stage, prompt, config, *, max_output_tokens):
        payload = json.loads(prompt)["payload"]
        self.calls.append((stage, payload))
        ref = {"anchor": "synthetic.txt, p. 1", "filename": "synthetic.txt", "page": 1}
        if stage == "chunk_understanding":
            return {"semantic_summary": "A cell membrane controls transport.", "major_topics": ["Membrane"], "source_refs": payload["source_refs"]}
        if stage == "course_model":
            return {"course_title": "Synthetic Biology", "concepts": [{"id": "membrane", "name": "Membrane", "definition": "Controls transport", "source_refs": [ref]}]}
        if stage == "study_blueprint":
            return {"units": [{"title": "Membrane transport", "knowledge_ids": ["membrane"], "priority": 80, "rationale": "Explicit course concept"}]}
        if stage == "optional_artifacts":
            def question(identifier, stem):
                return {"question_id": identifier, "knowledge_ids": ["membrane"], "learning_objective": "Explain transport", "scoring_rubric": ["Membrane controls transport"], "source_refs": [ref], "question_type": "short_answer", "question": stem, "answer": "Controls transport", "explanation": "The source describes transport control", "provenance": "GENERATED"}
            practice = question("p1", "What controls cell transport?")
            if self.invalid_question:
                practice["answer"] = ""
            return {"practice_questions": [practice], "mock_exam": {"questions": [question("e1", "Explain the role of the membrane in transport.")]}, "active_recall_cards": [{"card_id": "c1", "knowledge_ids": ["membrane"], "front": "What does a membrane do?", "back": "It controls transport.", "tags": "membrane", "source_refs": [ref]}]}
        if stage == "unit_draft":
            if self.fail_unit:
                raise RuntimeError("synthetic interruption")
            return {"markdown": "## Membrane\nThe membrane controls transport [synthetic.txt, p. 1].", "source_refs": [ref]}
        return {"canonical_markdown": "# Membrane\nThe membrane controls transport [synthetic.txt, p. 1]."}


def materials(text="The cell membrane controls transport."):
    return [ParsedFile(filename="synthetic.txt", file_type=".txt", path="synthetic.txt", raw_text=text,
        pages=[ParsedPage(page_number=1, source="text_extract", text=text, source_anchor="synthetic.txt, p. 1")])]


def run(monkeypatch, provider, checkpoints=None, callback=None, context=None, parsed=None):
    monkeypatch.setattr(pipeline, "get_llm_provider", lambda _: provider)
    return pipeline.generate_hierarchical_report(parsed or materials(), None,
        LLMConfig(enabled=True, provider="openai", model="synthetic-model"), job_id="synthetic", title="Synthetic Biology",
        checkpoints=checkpoints, checkpoint_callback=callback, learner_context=context,
        material_types={"synthetic.txt": "syllabus"})


def test_all_five_tabs_receive_validated_artifacts(monkeypatch):
    result, _ = run(monkeypatch, GroundedProvider())
    report = result.report
    assert result.llm_status == "success"
    assert report.markdown and report.study_units and report.anki_cards
    assert report.practice_questions and report.mock_exam.questions
    assert report.practice_questions[0].question != report.mock_exam.questions[0].question
    assert report.overview["artifacts"]["question_bank"]["questions"][0]["quality_status"] == "VALIDATED"
    assert report.overview["materials"][0]["role"] == "syllabus"


def test_every_successful_stage_is_reused_and_failed_unit_recovers(monkeypatch):
    saved = {}
    def callback(key, chapter, chunk, status, content, code, message):
        saved[key] = {"status": status, "content": content}
    first = GroundedProvider(fail_unit=True)
    result, _ = run(monkeypatch, first, callback=callback)
    assert result.llm_status == "partial"
    second = GroundedProvider()
    resumed, stats = run(monkeypatch, second, checkpoints=saved, callback=callback)
    assert resumed.llm_status == "success"
    assert [name for name, _ in second.calls] == ["unit_draft", "course_synthesis"]
    assert stats.resumed_checkpoints == 4
    third = GroundedProvider()
    _, stats = run(monkeypatch, third, checkpoints=saved, callback=callback)
    assert not third.calls and stats.resumed_checkpoints == 6


def test_invalid_questions_are_quarantined_and_not_cached_as_success(monkeypatch):
    saved = {}
    def callback(key, chapter, chunk, status, content, code, message):
        saved[key] = {"status": status, "content": content}
    result, _ = run(monkeypatch, GroundedProvider(invalid_question=True), callback=callback)
    assert result.llm_status == "partial"
    assert not result.report.practice_questions
    assert result.report.mock_exam.questions
    assert result.report.overview["artifacts"]["quality_report"]["quarantined"]
    assert all(item["status"] != "completed" for key, item in saved.items() if ":optional_artifacts:" in key)
    provider = GroundedProvider()
    recovered, _ = run(monkeypatch, provider, checkpoints=saved)
    assert recovered.report.practice_questions
    assert "optional_artifacts" in [name for name, _ in provider.calls]


def test_changed_inputs_invalidate_cache_and_source_identity_is_unique(monkeypatch):
    saved = {}
    def callback(key, chapter, chunk, status, content, code, message):
        saved[key] = {"status": status, "content": content}
    run(monkeypatch, GroundedProvider(), callback=callback)
    provider = GroundedProvider()
    run(monkeypatch, provider, checkpoints=saved, parsed=materials("Membranes selectively control transport."))
    assert "chunk_understanding" in [name for name, _ in provider.calls]
    a = pipeline.split_chapter(pipeline.ChapterSource("Chapter A", "identical text", ["doc1 p1"]), 1000)
    b = pipeline.split_chapter(pipeline.ChapterSource("Chapter B", "identical text", ["doc2 p1"]), 1000)
    assert a[0].chunk_id != b[0].chunk_id


def test_time_and_mastery_settings_change_actual_schedule(monkeypatch):
    constraints = learning_constraints({"exam_date": "2030-01-03", "daily_minutes": 30}, today=date(2030, 1, 1))
    assert constraints["total_minutes"] == 60
    first, _ = run(monkeypatch, GroundedProvider(), context={"study_goal": "one_day_sprint", "exam_date": "2030-01-01", "daily_minutes": 30, "mastery_level": "forgotten"})
    second, _ = run(monkeypatch, GroundedProvider(), context={"exam_date": "2030-01-01", "daily_minutes": 90, "mastery_level": "familiar"})
    assert first.report.sprint_plans != second.report.sprint_plans
    assert first.report.overview["learner_context"]["daily_practice_minutes"] < second.report.overview["learner_context"]["daily_practice_minutes"]


def test_request_fingerprint_uses_file_content_and_not_secrets(monkeypatch, tmp_path):
    path = tmp_path / "source.txt"
    path.write_text("old")
    monkeypatch.setattr("app.services.upload_resolver.resolve_uploaded_file", lambda _: path)
    payload = GenerateReviewRequest(files=["source.txt"]).model_dump(mode="json")
    before = store.request_fingerprint(payload)
    payload["llm_config"]["api_key"] = "fake-test-secret"
    payload["ocr_config"]["api_key"] = "fake-ocr-secret"
    assert before == store.request_fingerprint(payload)
    path.write_text("changed")
    assert before != store.request_fingerprint(payload)


def test_checkpoint_persistence_failure_does_not_become_success(monkeypatch):
    def fail(*args):
        raise OSError("simulated disk failure")
    with pytest.raises(RuntimeError, match="GENERATION_PERSIST_ERROR"):
        run(monkeypatch, GroundedProvider(), callback=fail)


def test_job_leases_prevent_duplicate_work_and_recover_interruption(monkeypatch, tmp_path):
    import sqlite3
    monkeypatch.setenv("RECALLFORGE_JOB_DB", str(tmp_path / "jobs.sqlite3"))
    payload = GenerateReviewRequest(files=["unavailable.txt"]).model_dump(mode="json")
    job_id, _ = store.create_or_resume("initial", payload)
    assert store.claim_job(job_id, "worker-one")
    assert not store.claim_job(job_id, "worker-two")
    duplicate, resumed = store.create_or_resume("duplicate", payload)
    assert duplicate == job_id and resumed
    store.save_checkpoint(job_id, "completed-stage", 0, 0, "completed", {"kept": True}, None, None)
    store.update_job(job_id, status="llm", progress=80, message="running")
    with sqlite3.connect(tmp_path / "jobs.sqlite3") as connection:
        connection.execute("UPDATE generation_jobs SET lease_expires=0")
    recovered, resumed = store.create_or_resume("after-crash", payload)
    assert recovered == job_id and resumed
    assert store.claim_job(job_id, "new-worker")
    assert store.load_checkpoints(job_id)["completed-stage"]["content"] == {"kept": True}


def test_partial_llm_result_is_never_marked_completed(monkeypatch, tmp_path):
    from app.routers import generate_review_jobs_v31 as jobs
    from app.schemas.review import GenerateReviewResponse, ReviewReport, LLMErrorInfo
    monkeypatch.setenv("RECALLFORGE_JOB_DB", str(tmp_path / "jobs.sqlite3"))
    request = GenerateReviewRequest(files=["unavailable.txt"])
    job_id, _ = store.create_or_resume("partial", request.model_dump(mode="json"))
    monkeypatch.setattr(jobs, "build_generate_review_response", lambda *args, **kwargs: GenerateReviewResponse(
        review_report=ReviewReport(title="Partial", summary="Pending repair"), markdown="# Partial", export_format="md",
        llm_status="partial", llm_error=LLMErrorInfo(code="QUALITY_FAILED", message="Invalid questions", suggestion="Repair")))
    jobs._run_job(job_id, request)
    job = store.get_job(job_id)
    assert job["status"] == "partial" and job["retryable"]


def test_partial_parse_reuses_first_successful_file(monkeypatch, tmp_path):
    from app.routers import generate_review as router
    first, second = tmp_path / "first.txt", tmp_path / "second.txt"
    first.write_text("first")
    second.write_text("second")
    cached = materials()[0].model_copy(update={"path": str(first), "filename": "first.txt"})
    calls = []
    monkeypatch.setattr(router, "resolve_uploaded_file", lambda ref: Path(ref))
    def parse(path, *args):
        calls.append(path)
        return materials()[0].model_copy(update={"path": str(path)})
    monkeypatch.setattr(router, "parse_file", parse)
    monkeypatch.setattr(router, "export_review_report", lambda *args: tmp_path / "unused.md")
    router.build_generate_review_response(GenerateReviewRequest(files=[str(first), str(second)]),
        parsed_files_checkpoint=[cached.model_dump(mode="json")], defer_exports=True)
    assert calls == [second]
