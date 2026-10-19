"""Bounded fan-in for course synthesis; never truncate an oversized input."""
from app.services.llm_capabilities import estimate_tokens


def synthesize(payload, *, stage, make_prompt, input_budget, output_budget, validate):
    current = list(payload["unit_drafts"])
    base = {key: value for key, value in payload.items() if key != "unit_drafts"}
    def fits(items):
        return estimate_tokens(make_prompt("course_synthesis", {**base, "unit_drafts": items})) <= input_budget
    for level in range(8):
        if fits(current):
            return stage("course_synthesis", {**base, "unit_drafts": current}, output_budget, validate)
        batches, batch = [], []
        for unit in current:
            if not fits([unit]):
                raise ValueError("SYNTHESIS_BUDGET_EXCEEDED: one unit plus course context exceeds input budget; no source text was truncated")
            if batch and not fits([*batch, unit]):
                batches.append(batch)
                batch = []
            batch.append(unit)
        if batch:
            batches.append(batch)
        reduced = []
        for index, group in enumerate(batches):
            result = stage("course_synthesis", {**base, "unit_drafts": group, "synthesis_scope": "intermediate",
                "instruction": "Synthesize these units into a concise evidence-linked intermediate document. Retain conditions, formulas, knowledge IDs and uncertainties; do not invent coverage."}, output_budget, validate, f"synthesis-{level}-{index}")
            reduced.append({"unit_id": f"synthesis-{level}-{index}", "markdown": result["canonical_markdown"],
                "covered_units": [unit.get("unit_id") for unit in group]})
        if estimate_tokens(str(reduced)) >= estimate_tokens(str(current)):
            raise ValueError("SYNTHESIS_NOT_REDUCED: intermediate synthesis did not reduce context")
        current = reduced
    raise ValueError("SYNTHESIS_BUDGET_EXCEEDED: maximum synthesis depth reached")
