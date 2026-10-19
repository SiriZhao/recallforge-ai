"""Observed exam records preserve exact text; predictions never become counts."""
import re
from collections import Counter
from pydantic import BaseModel, Field
from app.schemas.course_model import SourceRef
from app.services.stage_checkpoints import stable_hash


class ObservedQuestion(BaseModel):
    question_id: str
    text: str
    source_refs: list[SourceRef]
    question_type: str = "unknown"
    question_type_basis: str = "HEURISTIC_REQUIRES_REVIEW"
    options: list[str] = Field(default_factory=list)
    provenance: str = "OBSERVED"
    answer_status: str = "UNVERIFIED"
    completeness: str = "REQUIRES_REVIEW"
    duplicate_sources: list[SourceRef] = Field(default_factory=list)


class CourseExamModel(BaseModel):
    observed_questions: list[ObservedQuestion] = Field(default_factory=list)
    observed_type_counts: dict[str, int] = Field(default_factory=dict)
    explicit_scope: list[dict] = Field(default_factory=list)
    exclusions: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def question_signature(text):
    # Keep mathematical signs, case in source, decimals and option content.
    cleaned = re.sub(r"^\s*\d+\s*[.、．)]\s*", "", text)
    if re.search(r"\bdef |\bclass |print\(|\bif .*:",cleaned):
        # Python indentation and case are behavior, not presentation noise.
        return cleaned.replace("\r\n","\n").strip()
    return re.sub(r"\s+", "", cleaned)


def page_kind(text):
    blank_fields = bool(re.search(r"学\s*号|姓\s*名", text))
    numbered_stems = len(re.findall(r"(?:^|\n)\s*\d{1,3}[.、．]\s*\S.{5,}", text))
    if ("答题纸" in text or "答题卡" in text) and blank_fields and numbered_stems == 0:
        return "answer_sheet"
    return "content"


def observed_exam_model(parsed_files, roles):
    model = CourseExamModel()
    for parsed in parsed_files:
        role = roles.get(parsed.filename, roles.get(parsed.path, "other"))
        if role == "syllabus":
            for page in parsed.pages:
                for line in page.text.splitlines():
                    if re.search(r"不考|不在.*范围|excluded|not assessed",line,re.I):
                        model.exclusions.append({"text":line,"source_ref":{"anchor":page.source_anchor}})
                    elif re.search(r"考试范围|考查范围|exam scope|assessed",line,re.I):
                        model.explicit_scope.append({"text":line,"source_ref":{"anchor":page.source_anchor}})
        if role != "past_exam":
            continue
        current = None
        for page in parsed.pages:
            if page_kind(page.text) == "answer_sheet":
                model.warnings.append(f"ANSWER_SHEET_EXCLUDED:{page.source_anchor}")
                continue
            # Carry an unfinished stem into the next page instead of treating
            # every page as an independent question set.
            matches = list(re.finditer(r"(?m)^\s*(\d{1,3})\s*[.、．]\s*(?=\S)",page.text))
            if not matches:
                if current:
                    current.text += "\n" + page.text
                    current.source_refs.append(SourceRef(filename=parsed.original_filename or parsed.filename,
                        page=page.page_number, anchor=page.source_anchor or f"{parsed.filename}, p. {page.page_number}"))
                continue
            if current and matches[0].start() > 0:
                prefix=page.text[:matches[0].start()].strip()
                if prefix:
                    current.text += "\n"+prefix
                    current.source_refs.append(SourceRef(filename=parsed.original_filename or parsed.filename,
                        page=page.page_number,anchor=page.source_anchor or f"{parsed.filename}, p. {page.page_number}",fragment=prefix))
            for index, match in enumerate(matches):
                end = matches[index+1].start() if index+1<len(matches) else len(page.text)
                text=page.text[match.start():end].strip()
                ref=SourceRef(filename=parsed.original_filename or parsed.filename,page=page.page_number,
                    anchor=page.source_anchor or f"{parsed.filename}, p. {page.page_number}",fragment=text)
                options=re.findall(r"(?:^|\s)([A-H][.、．)]\s*.*?)(?=\s+[A-H][.、．)]|$)",text,re.S)
                kind="multiple_choice" if len(options)>=2 else "programming" if re.search(r"def |print\(|编写程序|代码",text) else "short_answer"
                signature=question_signature(text)
                current=ObservedQuestion(question_id="observed-"+stable_hash(signature)[:20],text=text,
                    source_refs=[ref],question_type=kind,options=options,
                    completeness="CANDIDATE_COMPLETE" if len(text)>25 and (not options or len(options)>=4) else "REQUIRES_REVIEW")
                model.observed_questions.append(current)
    # Recompute identities after cross-page continuation; deduplicate again.
    unique={}
    for question in model.observed_questions:
        signature=question_signature(question.text)
        if signature in unique:
            unique[signature].duplicate_sources.extend(question.source_refs)
        else:
            question.question_id="observed-"+stable_hash(signature)[:20]
            unique[signature]=question
    model.observed_questions=list(unique.values())
    model.observed_type_counts=dict(Counter(q.question_type for q in model.observed_questions))
    if not model.observed_questions:
        model.warnings.append("NO_VERIFIED_PAST_QUESTIONS; do not infer historical counts")
    return model


def exam_blueprint(exam_model, exam_type, question_count=6):
    counts=exam_model.observed_type_counts
    if not counts:
        kinds=["programming"] if exam_type=="programming" else ["short_answer"]
        return {"question_count":question_count,"type_targets":{kind:question_count//len(kinds) for kind in kinds},"basis":"INFERRED","observed_count":0}
    total=sum(counts.values())
    targets={kind:question_count*count//total for kind,count in counts.items()}
    for kind in sorted(counts,key=counts.get,reverse=True)[:question_count-sum(targets.values())]:
        targets[kind]+=1
    return {"question_count":question_count,"type_targets":targets,"basis":"OBSERVED_DISTRIBUTION_NOT_PREDICTION","observed_count":total}
