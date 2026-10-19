"""Explicit learner constraints; scheduling is deterministic, teaching remains LLM-authored."""
from datetime import date


MATERIAL_POLICIES = {
    "syllabus": "Extract explicit scope, exclusions and teacher requirements; record version and conflicts.",
    "past_exam": "Record only questions actually present, their source and explicit year; do not invent frequencies.",
    "answer_key": "Extract worked answers and scoring evidence; link to the matching exam and flag disagreement.",
    "textbook": "Extract concepts, formulas, conditions and theory with source locations.",
    "slides": "Extract teaching organization and explicit emphasis, without assuming exam frequency.",
    "notes": "Extract learning explanations; distinguish personal claims from source-supported facts.",
    "mistakes": "Record demonstrated errors as learner weakness signals, not general course facts.",
    "other": "Determine document purpose from evidence; preserve uncertainty.",
}


def learning_constraints(context: dict, *, today: date | None = None) -> dict:
    today = today or date.today()
    minutes = int(context.get("daily_minutes", 90))
    try:
        days = max(1, (date.fromisoformat(context.get("exam_date", "")) - today).days)
    except (ValueError, TypeError):
        days = {"one_day_sprint": 1, "three_day_sprint": 3, "seven_day_plan": 7}.get(context.get("study_goal"))
    # Allocation is an explicit starting policy, not an estimate of mastery.
    practice_fraction = 0.65 if context.get("mastery_level") in {"good", "familiar", "confident", "advanced"} else 0.35
    return {
        **context, "planning_date": today.isoformat(), "days_remaining": days,
        "total_minutes": days * minutes if days is not None else None,
        "daily_understanding_minutes": minutes - round(minutes * practice_fraction),
        "daily_practice_minutes": round(minutes * practice_fraction),
        "basis": "Time allocations are planning heuristics; mastery requires observed answers.",
    }
