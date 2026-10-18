"""Explainable assessment and scheduling. Uncertain answers require self review."""
import re
import math
from datetime import datetime, timezone, timedelta, date


ERROR_CATEGORIES = {"concept_not_understood", "condition_omitted", "concept_confusion", "calculation_error",
                    "formula_misuse", "experiment_principle", "misread_question", "incomplete_expression", "uncertain"}


def normalized_answer(value):
    return re.sub(r"\s+", "", value).casefold()


def assess_answer(item, answer, *, self_score=None, error_category=None, card=False):
    reference = item.get("back", "") if card else item.get("answer", "")
    result={"score":None,"assessed_by":"pending_self_review","correct_points":[],"missing_points":[],
        "errors":[],"uncertainty":"No reliable deterministic grading rule for this response.","needs_self_review":True,
        "suggestion":"Compare your answer with the rubric and correct the self assessment.","reference_answer":reference}
    options=item.get("options") or []
    kind=item.get("question_type","")
    if options and re.search(r"choice|选择",kind,re.I):
        labels=[chr(65+i) for i in range(len(options))]
        def choice(value):
            stripped=value.strip().upper().strip(".、．)")
            if stripped in labels:
                return stripped
            for i,option in enumerate(options):
                if normalized_answer(value)==normalized_answer(option):
                    return labels[i]
            return None
        correct,submitted=choice(reference),choice(answer)
        if correct is not None and submitted is not None:
            result.update(score=float(correct==submitted),assessed_by="deterministic_choice",needs_self_review=False,
                uncertainty="Score is relative to this item's reference; reference correctness is not independently guaranteed.")
            if correct!=submitted:
                result["errors"]=[{"category":"uncertain","basis":"Selected option differs from reference; cause needs learner review."}]
    if self_score is not None:
        if not 0 <= self_score <= 1:
            raise ValueError("Self score must be in [0,1]")
        if result["assessed_by"] != "deterministic_choice":
            result.update(score=self_score,assessed_by="learner_self_report",needs_self_review=False,
                uncertainty="Learner-reported rating, not automatic correctness verification.")
    rubric=item.get("scoring_rubric",[])
    for point in rubric:
        # Literal matches are displayed as review aids, not awarded marks.
        target="correct_points" if normalized_answer(point) in normalized_answer(answer) else "missing_points"
        result[target].append(point)
    if error_category:
        if error_category not in ERROR_CATEGORIES:
            raise ValueError("Unknown error category")
        result["errors"]=[{"category":error_category,"basis":"learner_reported"}]
    return result


def update_mastery(previous, score, difficulty, now=None):
    now=now or datetime.now(timezone.utc)
    prior=previous or {"weighted_score":0.0,"weight":0.0,"attempts":0,"interval_days":0}
    if score is None:
        return prior
    weight={"easy":0.8,"基础":0.8,"hard":1.3,"困难":1.3}.get(str(difficulty).casefold(),1.0)
    scored=prior["weighted_score"]+score*weight
    total=prior["weight"]+weight
    interval=min(30,max(1,prior.get("interval_days",0)*2)) if score>=0.8 else 1
    return {"weighted_score":scored,"weight":total,"attempts":prior["attempts"]+1,
        "mastery":(1+scored)/(2+total),"confidence":total/(total+5),"interval_days":interval,
        "last_reviewed":now.isoformat(),"next_review":(now+timedelta(days=interval)).isoformat(),
        "basis":"Beta(1,1) prior, difficulty-weighted observed/self-reported scores; doubling interval after success, one day after difficulty"}


def recommend(concepts, states, settings, *, today=None):
    today=today or date.today()
    try:
        days=max(1,(date.fromisoformat(settings.get("exam_date", ""))-today).days)
    except (ValueError,TypeError):
        days=None
    try:
        target=max(.5,min(.95,float(settings.get("target_score") or 80)/100))
    except ValueError:
        target=.8
    available=int(settings.get("daily_minutes",90))
    rows=[]
    for concept in concepts:
        state=states.get(concept["id"],{})
        mastery=state.get("mastery",.35 if settings.get("mastery_level")=="forgotten" else .5)
        if state.get("last_reviewed"):
            try:
                elapsed=max(0,(today-date.fromisoformat(state["last_reviewed"][:10])).days)
                half_life=max(2,2*state.get("interval_days",1))
                mastery=.2+(mastery-.2)*math.exp(-math.log(2)*elapsed/half_life)
            except (ValueError,TypeError):
                pass
        overdue=bool(state.get("next_review") and state["next_review"][:10]<=today.isoformat())
        prerequisites=[key for key in concept.get("prerequisites",[]) if states.get(key,{}).get("mastery",.35)<.6]
        cost=max(5,round(5+concept.get("difficulty",50)/10))
        weight=max(.1,concept.get("importance",50)/100)
        gap=max(.05,target-mastery)
        urgency=1+1/max(1,days) if days else 1
        priority=weight*gap*urgency*(1.5 if overdue else 1)/cost
        focus=bool(settings.get("focus") and any(term in concept.get("name","") for term in re.split(r"[,，;；\s]+",settings["focus"]) if term))
        if focus: priority*=1.25
        rows.append({"knowledge_id":concept["id"],"name":concept.get("name",concept["id"]),"priority":round(priority,6),
            "estimated_minutes":cost,"mastery":mastery,"evidence_count":state.get("attempts",0),"prerequisites":prerequisites,
            "reason":{"importance":weight,"mastery_gap":gap,"overdue":overdue,"days_remaining":days,"learner_focus":focus,
                      "exam_type":settings.get("exam_type","unknown"),"not_an_exam_probability":True}})
    rows.sort(key=lambda row:row["priority"],reverse=True)
    # Prefer unmet prerequisites when they exist in the actual course graph.
    by_id={row["knowledge_id"]:row for row in rows}
    ordered=[];seen=set()
    def visit(row,visiting):
        key=row["knowledge_id"]
        if key in seen or key in visiting:return
        for dependency in row["prerequisites"]:
            if dependency in by_id:visit(by_id[dependency],visiting|{key})
        seen.add(key);ordered.append(row)
    for row in rows:visit(row,set())
    chosen=[]
    for row in ordered:
        if row["estimated_minutes"]<=available:
            chosen.append(row);available-=row["estimated_minutes"]
    return {"items":chosen,"daily_minutes":int(settings.get("daily_minutes",90)),"unused_minutes":available,
        "method":"transparent priority heuristic; prerequisite ordering; no trained learning-gain claims"}
