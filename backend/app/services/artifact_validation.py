"""Fail closed on invalid structured outputs; never fill gaps with templates."""
import re
from pydantic import ValidationError
from app.schemas.learning_artifacts import (
    LearningArtifacts, LearningMaterial, KnowledgeGraph, Question, RecallCard,
)
from app.schemas.review import GeneratedExamQuestion, GeneratedMockExam, AnkiCard, StudyUnit, ReviewPlanItem, SprintPlan,PastExamAnalysis,PastExamFileAnalysis
from app.services.stage_checkpoints import stable_hash
from app.services.evidence_catalog import catalog_for, resolves
from app.services.numeric_verification import verify
from app.services.choice_answers import answer_labels


def _text(value):
    return re.sub(r"\s+", "", value).casefold()


def _refs_valid(refs, allowed):
    return bool(refs) and all(resolves(ref, allowed) for ref in refs)


def _question_failure(question, allowed, concepts):
    if not question.question.strip() or not question.answer.strip() or not question.explanation.strip():
        return "missing_stem_answer_or_solution"
    if any(marker in question.answer.casefold() for marker in ["应围绕", "结合材料作答", "应解释定义", "answer based on the material"]):
        return "answer_is_only_advice"
    if not _refs_valid(question.source_refs, allowed):
        return "invalid_source_ref"
    if not set(question.knowledge_ids).issubset(concepts):
        return "unknown_knowledge_id"
    if question.provenance == "OBSERVED":
        # Exact source extraction is required before generated questions may be
        # advertised as observed; current artifacts are generated exercises.
        return "observed_question_requires_extraction_verification"
    if not all(item.strip() for item in question.scoring_rubric):
        return "empty_rubric"
    if question.reference_expression is not None:
        if question.numeric_answer is None or not verify(question.reference_expression,question.numeric_answer,question.quantity_kind):
            return "numeric_solution_inconsistent_or_unsupported"
    if question.requires_diagram or re.search(r'如图|图中|见图|识图|according to (?:the )?(?:diagram|figure)|shown in (?:the )?figure',question.question,re.I):
        return "diagram_interpretation_not_verified"
    choice = question.options or re.search(r"choice|选择", question.question_type, re.I)
    if choice:
        if len(question.options) < 2 or len({_text(o) for o in question.options}) != len(question.options) or not all(o.strip() for o in question.options):
            return "invalid_options"
        labels = answer_labels(question.answer,question.options)
        if labels is None:
            return "unmapped_choice_answer"
        if len(labels)>1 and not re.search(r'多选|多项|multiple_select',question.question_type,re.I):
            return "ambiguous_choice_scoring_rule"
    return None


def build_artifacts(course, blueprint, drafts, canonical, raw, parsed_files):
    course=course.model_copy(deep=True)
    allowed = catalog_for(parsed_files)
    concepts = {c.id for c in course.concepts}
    artifacts = LearningArtifacts(course_model=course, study_blueprint=blueprint,
        knowledge_graph=KnowledgeGraph(nodes=[c.model_dump(mode="json") for c in course.concepts], edges=[r.model_dump(mode="json") for r in course.relationships]),
        learning_material=LearningMaterial(units=drafts, canonical_markdown=canonical))
    quality = artifacts.quality_report
    seen = set()
    seen_ids = set()
    for collection, target in [("practice_questions", artifacts.question_bank.questions), ("mock_exam", artifacts.mock_exam.questions)]:
        items = raw.get(collection, [])
        if isinstance(items, dict):
            items = items.get("questions", [])
        if not isinstance(items, list):
            items = []
        for index, item in enumerate(items):
            try:
                question = Question.model_validate(item)
                question.knowledge_ids = [course.concept_id_aliases.get(value,value) for value in question.knowledge_ids]
                failure = _question_failure(question, allowed, concepts)
                signature = _text(question.question)
                if signature in seen:
                    failure = "duplicate_question_or_practice_exam_overlap"
                if question.question_id in seen_ids:
                    failure = "duplicate_question_id"
                if failure:
                    raise ValueError(failure)
                seen.add(signature)
                seen_ids.add(question.question_id)
                question.quality_status = "VALIDATED"
                target.append(question)
            except (ValidationError, ValueError, TypeError) as exc:
                quality.quarantined.append({"collection": collection, "index": index, "reason": str(exc)[:400]})
        if not target:
            quality.failures.append(f"missing_valid_{collection}")
    cards = raw.get("active_recall_cards", raw.get("recall_cards", []))
    if isinstance(cards, dict):
        cards = cards.get("cards", [])
    seen_cards = set()
    for index, item in enumerate(cards if isinstance(cards, list) else []):
        try:
            card = RecallCard.model_validate(item)
            card.knowledge_ids = [course.concept_id_aliases.get(value,value) for value in card.knowledge_ids]
            if not card.front.strip() or not card.back.strip() or not _refs_valid(card.source_refs, allowed) or not set(card.knowledge_ids).issubset(concepts):
                raise ValueError("invalid_card_or_evidence")
            if _text(card.front) in seen_cards:
                raise ValueError("duplicate_card")
            seen_cards.add(_text(card.front))
            artifacts.recall_cards.cards.append(card)
        except (ValidationError, ValueError, TypeError) as exc:
            quality.quarantined.append({"collection": "recall_cards", "index": index, "reason": str(exc)[:400]})
    if not artifacts.recall_cards.cards:
        quality.failures.append("missing_valid_recall_cards")
    if not course.concepts:
        quality.failures.append("missing_knowledge_graph")
    for concept in course.concepts:
        if not _refs_valid(concept.source_refs, allowed):
            concept.basis = "INSUFFICIENT"
            quality.failures.append(f"unresolved_concept_source:{concept.id}")
    for relation in course.relationships:
        if relation.source not in concepts or relation.target not in concepts or not _refs_valid(relation.source_refs, allowed):
            quality.failures.append(f"unresolved_relationship:{relation.source}:{relation.target}")
            quality.quarantined.append({"collection":"knowledge_relationships","source":relation.source,"target":relation.target,"reason":"unresolved_source_or_entity"})
    artifacts.knowledge_graph.edges=[relation.model_dump(mode='json') for relation in course.relationships
        if relation.source in concepts and relation.target in concepts and _refs_valid(relation.source_refs,allowed)]
    artifacts.knowledge_graph.nodes=[concept.model_dump(mode='json') for concept in course.concepts]
    if any(draft.status != "READY" for draft in drafts):
        quality.failures.append("incomplete_learning_units")
    if quality.failures or quality.quarantined:
        artifacts.generation_status.state = "PARTIAL"
    return artifacts


def project_artifacts(report, artifacts, intent):
    """Map validated artifacts to existing UI/export fields, preserving Markdown."""
    report.practice_questions = [GeneratedExamQuestion.model_validate(q.model_dump()) for q in artifacts.question_bank.questions]
    report.mock_exam = GeneratedMockExam(title=artifacts.mock_exam.title,
        questions=[GeneratedExamQuestion.model_validate(q.model_dump()) for q in artifacts.mock_exam.questions])
    report.anki_cards = [AnkiCard.model_validate(card.model_dump()) for card in artifacts.recall_cards.cards]
    course = artifacts.course_model
    for index, draft in enumerate(artifacts.learning_material.units):
        plan = artifacts.study_blueprint.units[index].model_dump(mode="json") if index < len(artifacts.study_blueprint.units) else {}
        ids = {course.concept_id_aliases.get(value,value) for value in plan.get("knowledge_ids", [])}
        concepts = [c for c in course.concepts if not ids or c.id in ids]
        report.study_units.append(StudyUnit(name=draft.title, key_points=[c.name for c in concepts],
            must_know=[c.definition for c in concepts if c.definition], how_to_review=plan.get("rationale", ""),
            formulas_or_methods=[condition for c in concepts for condition in c.conditions],
            pitfalls=[confusion for c in concepts for confusion in c.common_confusions],
            reason=plan.get("rationale", ""), priority=max(0, min(100, int(plan.get("priority", 50))))))
        report.review_order.append(ReviewPlanItem(chapter=draft.title, importance=report.study_units[-1].priority,
            reason=plan.get("rationale", "")))
    days = intent.get("days_remaining")
    if days:
        units = report.study_units
        total = int(intent["total_minutes"])
        weights = [max(1, unit.priority) for unit in units]
        allocations = [total * weight // max(1, sum(weights)) for weight in weights]
        for index in range(total - sum(allocations)):
            if allocations:
                allocations[index % len(allocations)] += 1
        report.overview["time_allocations"] = [{"unit": unit.name, "minutes": minutes} for unit, minutes in zip(units, allocations)]
        schedule = []
        for unit, minutes in zip(units, allocations):
            practice = round(minutes * intent["daily_practice_minutes"] / intent["daily_minutes"])
            schedule.append(f"{unit.name}: total {minutes} min; understanding {minutes - practice} min; practice {practice} min. {unit.reason}")
        report.sprint_plans = [SprintPlan(days=days, title=f"{days}-day plan / {intent['daily_minutes']} minutes per day", schedule=schedule)]
    report.overview["artifacts"] = artifacts.model_dump(mode="json")
    report.overview["artifact_fingerprint"] = stable_hash(artifacts.model_dump(mode="json"))
    observed=report.overview.get("exam_model",{}).get("observed_questions",[])
    files={}
    for question in observed:
        for ref in question.get("source_refs",[]):
            filename=ref.get("filename","")
            files.setdefault(filename,{"questions":set(),"types":set()})
            files[filename]["questions"].add(question["question_id"])
            files[filename]["types"].add(question.get("question_type","unknown"))
    if files:
        report.past_exam_analysis=PastExamAnalysis(
            detected_files=[PastExamFileAnalysis(filename=name,confidence=0,question_count=len(value["questions"]),question_types=sorted(value["types"])) for name,value in files.items()],
            summary=f"从用户标记的往年卷中识别 {len(observed)} 道去重候选题；题界和题型仍需核对，答案未经独立验证。未据此推算考试概率或历史年度频次。")
