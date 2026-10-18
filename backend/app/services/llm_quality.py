import re
from dataclasses import dataclass

from app.schemas.review import ExamType, ReportQuality, ReviewReport, StudyGoal
from app.services.chapter_extractor import DEFAULT_CHAPTER, extract_chapters
from app.services.quality_checker import check_output_quality


@dataclass
class QualityResult:
    quality_score: int
    material_completeness_score: int
    topic_coverage_score: int
    mock_exam_quality_score: int
    anki_quality_score: int
    export_readiness_score: int
    evidence_integration_score: int
    quality_warnings: list[str]
    quality_failures: list[str]
    repairable: bool
    section_coverage_ratio: float
    missing_sections: list[str]
    duplicate_sections: list[str]
    is_empty: bool

    def to_model(self) -> ReportQuality:
        return ReportQuality(
            quality_score=self.quality_score,
            material_completeness_score=self.material_completeness_score,
            topic_coverage_score=self.topic_coverage_score,
            mock_exam_quality_score=self.mock_exam_quality_score,
            anki_quality_score=self.anki_quality_score,
            export_readiness_score=self.export_readiness_score,
            evidence_integration_score=self.evidence_integration_score,
            quality_warnings=self.quality_warnings,
            quality_failures=self.quality_failures,
            repairable=self.repairable,
            section_coverage_ratio=self.section_coverage_ratio,
            missing_sections=self.missing_sections,
            duplicate_sections=self.duplicate_sections,
            is_empty=self.is_empty,
        )


def validate_report_quality(
    report: ReviewReport,
    materials_text: str = "",
    *,
    study_goal: StudyGoal = "balanced",
    exam_type: ExamType = "unknown",
    file_count: int = 1,
    has_past_exam: bool | None = None,
) -> QualityResult:
    if report.overview.get("artifacts"):
        return validate_artifact_report(report)
    warnings: list[str] = []
    failures: list[str] = []
    body = build_report_body(report)
    material_len = len(materials_text.strip())
    markdown_headings = len(re.findall(r"^#{1,3}\s+", report.markdown, re.M))
    topic_count = len(report.study_units) or len(report.chapters) or len(report.high_frequency_points) or markdown_headings
    question_count = len(report.mock_exam.questions)
    answered_count = sum(1 for item in report.mock_exam.questions if item.answer.strip())
    explained_count = sum(1 for item in report.mock_exam.questions if item.explanation.strip())
    anki_count = len(report.anki_cards)
    past_exam_detected = bool(report.past_exam_analysis.detected_files)
    if has_past_exam is not None:
        past_exam_detected = has_past_exam

    material_score = clamp_score(45 + min(30, material_len // 180) + min(20, file_count * 8) + (15 if past_exam_detected else 0))
    topic_score = clamp_score(40 + min(45, topic_count * 10) + min(20, len(report.high_frequency_points) * 3) - (15 if has_garbled_text(body) else 0))
    mock_score = clamp_score(20 + min(35, question_count * 9) + min(25, answered_count * 6) + min(20, explained_count * 5))
    anki_score = clamp_score(40 + min(50, anki_count * 8) + (10 if unique_anki_fronts(report) else 0))
    export_score = clamp_score(
        50
        + (20 if report.markdown or body else 0)
        + (15 if question_count and answered_count == question_count else 0)
        + (15 if anki_count else 0)
        + (15 if report.title and report.summary else 0)
        - (20 if has_garbled_text(body) else 0)
    )
    evidence_score = clamp_score(
        45
        + min(25, file_count * 8)
        + min(20, len(report.past_exam_analysis.high_frequency_topics) * 4)
        + min(15, len(report.question_types) * 5)
        + (15 if past_exam_detected else 0)
    )

    weights = quality_weights(study_goal)
    score = weighted_score(
        {
            "material": material_score,
            "topic": topic_score,
            "mock": mock_score,
            "anki": anki_score,
            "export": export_score,
            "evidence": evidence_score,
        },
        weights,
    )

    if len(body.strip()) < 400:
        failures.append("内容过短，无法作为完整复习资料使用。")
        score -= 28
    elif len(body.strip()) < 900:
        warnings.append("报告正文略短，建议补充更多材料证据和复习细节。")
        score -= 2
    if topic_count == 0:
        failures.append("缺少具体复习单元或考点。")
        score -= 24
    if question_count < 3:
        warnings.append("当前资料未包含足量练习题；这不是 study guide 可用性的硬失败。")
        score -= 2
    if question_count and answered_count < question_count:
        failures.append("部分练习题缺少参考答案。")
        score -= 16
    if anki_count < 3:
        warnings.append("当前资料未包含足量 Anki 卡片；可按需生成记忆卡。")
        score -= 2
    if study_goal == "anki_focused" and anki_count < 10:
        failures.append("Anki 整理目标下，卡片数量或覆盖不足。")
        score -= 12
    if study_goal == "practice_heavy" and question_count < 8:
        failures.append("重点刷题目标下，模拟题数量不足。")
        score -= 12
    if study_goal == "one_day_sprint" and len(body) > 12000:
        warnings.append("1 天速通目标下报告偏长，可能不够突出最短路径。")
        score -= 8
    if exam_type == "programming" and not re.search(r"代码|函数|编程|debug|输出|code|function|program", body, re.I):
        failures.append("缺少编程考试需要的代码阅读、调试或实现类内容。")
        score -= 12
    if exam_type == "essay_based" and not re.search(r"论述|框架|提纲|比较|分析|essay|argument", body, re.I):
        failures.append("缺少论文/论述型考试需要的答题框架。")
        score -= 12
    if has_garbled_text(body):
        failures.append("输出中存在明显乱码或 OCR 噪声。")
        score -= 22
    if has_too_much_generic_advice(body):
        warnings.append("报告中存在较多泛泛建议，需要补充材料证据和具体复习动作。")
        score -= 10
    bad_titles = [name for name in unit_names(report) if is_bad_unit_name(name)]
    if bad_titles:
        failures.append(f"存在不可用的章节/专题名称：{'、'.join(bad_titles[:3])}")
        score -= 16
    if report.markdown and not re.search(r"^#{1,3}\s+", report.markdown, re.M):
        warnings.append("Markdown 标题层级不清晰，可能影响 Word/PDF 导出阅读。")
        score -= 6

    expected_sections = list(
        dict.fromkeys(
            section.title
            for section in extract_chapters(materials_text)
            if section.title != DEFAULT_CHAPTER and section.text.strip()
        )
    )[:40]
    engine_check = check_output_quality(report, expected_sections)
    warnings.extend(item for item in engine_check.warnings if item not in warnings)
    failures.extend(item for item in engine_check.failures if item not in failures)
    if engine_check.is_empty:
        score -= 30
    if engine_check.coverage_ratio < 0.5 and len(expected_sections) >= 3:
        score -= 20
    elif engine_check.coverage_ratio < 0.8 and len(expected_sections) >= 2:
        score -= 8
    if engine_check.duplicate_sections:
        score -= min(12, len(engine_check.duplicate_sections) * 3)

    if materials_text and report.mock_exam.questions:
        material_terms = set(re.findall(r"[\u4e00-\u9fff]{2,6}|[A-Za-z]{4,}", materials_text.lower()))
        question_terms = set(
            re.findall(r"[\u4e00-\u9fff]{2,6}|[A-Za-z]{4,}", "\n".join(q.question.lower() for q in report.mock_exam.questions))
        )
        if material_terms and question_terms and len(material_terms & question_terms) < 2:
            warnings.append("模拟题与材料关键词重合较少，需要检查是否偏离材料。")
            score -= 2

    if not failures and topic_count:
        score += 8

    score = clamp_score(score)
    repairable = score >= 35 or not has_garbled_text(body)
    return QualityResult(
        quality_score=score,
        material_completeness_score=material_score,
        topic_coverage_score=topic_score,
        mock_exam_quality_score=mock_score,
        anki_quality_score=anki_score,
        export_readiness_score=export_score,
        evidence_integration_score=evidence_score,
        quality_warnings=warnings,
        quality_failures=failures,
        repairable=repairable,
        section_coverage_ratio=engine_check.coverage_ratio,
        missing_sections=engine_check.missing_sections,
        duplicate_sections=engine_check.duplicate_sections,
        is_empty=engine_check.is_empty,
    )


def validate_artifact_report(report: ReviewReport) -> QualityResult:
    """Gate the active path by validated content/status, never length or quotas."""
    artifacts=report.overview['artifacts']
    quality=artifacts.get('quality_report',{})
    failures=list(quality.get('failures',[]))
    quarantined=quality.get('quarantined',[])
    if quarantined:failures.append('quarantined_structured_items')
    statuses=report.overview.get('unit_statuses',[])
    unit_score=round(100*sum(status=='READY' for status in statuses)/len(statuses)) if statuses else 0
    if report.overview.get('course_synthesis_status')=='PARTIAL':failures.append('course_synthesis_incomplete')
    manifest=report.overview.get('coverage_manifest',{})
    total=len(manifest.get('knowledge_ids',[]))
    missing=manifest.get('missing_concept_mentions',[])
    coverage=max(0,(total-len(missing))/total) if total else 0
    if missing:failures.append('concept_mentions_missing_from_synthesis')
    source_failures=[failure for failure in failures if 'source' in failure or 'relationship' in failure]
    evidence_score=100 if total and not source_failures else 0
    mock_score=100 if report.mock_exam.questions and not any('mock_exam' in failure for failure in failures) else 0
    card_score=100 if report.anki_cards and not any('recall' in failure for failure in failures) else 0
    empty=not report.markdown.strip()
    export_score=0 if empty else 100
    overall=min(unit_score,round(coverage*100),evidence_score,mock_score,card_score,export_score)
    if failures:overall=min(overall,49)
    warnings=['Scores reflect automated schema/reference/coverage gates, not human-verified factual correctness or learning effectiveness.',
              'Concept coverage is name/alias presence; material-wide golden coverage and pedagogical depth require independent evaluation.']
    for review in report.overview.get('unit_quality',[]):
        if review.get('verdict')!='READY':
            warnings.extend(review.get('issues',[]))
    source=report.overview.get('source_coverage',{})
    material_score=round(100*source.get('chunks_understood',0)/max(1,source.get('chunks_total',0)))
    return QualityResult(quality_score=overall,material_completeness_score=material_score,topic_coverage_score=round(coverage*100),
        mock_exam_quality_score=mock_score,anki_quality_score=card_score,export_readiness_score=export_score,
        evidence_integration_score=evidence_score,quality_warnings=warnings,quality_failures=list(dict.fromkeys(failures)),
        repairable=True,section_coverage_ratio=coverage,missing_sections=missing,duplicate_sections=[],is_empty=empty)


def build_repair_report_prompt(
    report: ReviewReport,
    quality: QualityResult,
    evidence_context: str,
) -> str:
    return f"""
这是你刚生成的复习资料，但系统检测到它还不够可用。请在保留材料事实的基础上修复一次。
质量分：{quality.quality_score}/100
子分：
- 资料完整度：{quality.material_completeness_score}/100
- 考点覆盖度：{quality.topic_coverage_score}/100
- 模拟题质量：{quality.mock_exam_quality_score}/100
- Anki 可用性：{quality.anki_quality_score}/100
- 导出就绪度：{quality.export_readiness_score}/100
- 证据整合度：{quality.evidence_integration_score}/100
质量问题：
{format_items(quality.quality_failures + quality.quality_warnings)}

修复要求：
1. 不要套用固定章节名，重新命名为自然、可复习的专题/知识模块。
2. 不要把题型强行归入固定库，请根据题干和材料自动总结题型。
3. 补足具体考点、题目、答案、解析、Anki 卡片和 1/3/7 天冲刺计划。
4. 不要输出空话，不要输出乱码，不要删除已有可用内容。
5. 输出仍必须是合法 JSON，字段兼容 ReviewReport，并保留 markdown 字段。

材料证据：
{evidence_context}

待修复报告 JSON：
{report.model_dump_json(ensure_ascii=False)}
""".strip()


def build_report_body(report: ReviewReport) -> str:
    return "\n".join(
        [
            report.title,
            report.summary,
            report.markdown,
            "\n".join(unit.name for unit in report.study_units),
            "\n".join(point for point in report.high_frequency_points),
            "\n".join(question.question + question.answer + question.explanation for question in report.mock_exam.questions),
            "\n".join(card.front + card.back for card in report.anki_cards),
            "\n".join(item.name + item.answer_strategy for item in report.question_types),
        ]
    )


def quality_weights(study_goal: StudyGoal) -> dict[str, float]:
    weights = {"material": 0.16, "topic": 0.2, "mock": 0.18, "anki": 0.16, "export": 0.14, "evidence": 0.16}
    if study_goal == "anki_focused":
        weights.update({"anki": 0.3, "mock": 0.1, "topic": 0.18})
    elif study_goal == "practice_heavy":
        weights.update({"mock": 0.32, "anki": 0.1, "topic": 0.18})
    elif study_goal == "one_day_sprint":
        weights.update({"topic": 0.26, "mock": 0.16, "anki": 0.1, "export": 0.16})
    elif study_goal == "past_exam_focused":
        weights.update({"evidence": 0.28, "mock": 0.22, "topic": 0.18})
    return weights


def weighted_score(scores: dict[str, int], weights: dict[str, float]) -> int:
    total_weight = sum(weights.values())
    return int(sum(scores[key] * weights[key] for key in weights) / total_weight)


def unique_anki_fronts(report: ReviewReport) -> bool:
    fronts = [card.front.strip() for card in report.anki_cards if card.front.strip()]
    return bool(fronts) and len(fronts) == len(set(fronts))


def unit_names(report: ReviewReport) -> list[str]:
    names = [unit.name for unit in report.study_units]
    names.extend(chapter.chapter for chapter in report.chapters)
    return names


def is_bad_unit_name(name: str) -> bool:
    stripped = name.strip()
    if not stripped or len(stripped) > 80:
        return True
    if re.fullmatch(r"[A-D]|[0-9]+|[-_=+*/\\]+", stripped):
        return True
    if len(re.findall(r"[锟枴鈻燷�Ãäåç鎵锘]", stripped)) > 0:
        return True
    if stripped.endswith(("?", "？", "，", ",")) and len(stripped) > 20:
        return True
    return False


def has_garbled_text(text: str) -> bool:
    if re.search(r"[锟枴鈻燷�Ã]{2,}|(閵|閿|缁|濡|婢|閸|瀵|鐟|閻){4,}", text):
        return True
    letters = re.findall(r"[A-Za-z]", text)
    if len(letters) > 80:
        upper_ratio = sum(1 for char in letters if char.isupper()) / len(letters)
        return upper_ratio > 0.88
    return False


def has_too_much_generic_advice(text: str) -> bool:
    generic = [
        "认真复习",
        "加强理解",
        "多做练习",
        "掌握重点",
        "查漏补缺",
        "系统复习",
    ]
    return sum(text.count(item) for item in generic) >= 8


def format_items(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) or "- 暂无明确问题，请提升具体性。"


def clamp_score(value: int) -> int:
    return max(0, min(100, int(value)))
