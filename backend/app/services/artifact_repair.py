"""Bounded, item-local repair; validated siblings are immutable inputs."""
from copy import deepcopy

from app.services.artifact_validation import build_artifacts
from app.services.stage_checkpoints import CheckpointWriteError


def repair_artifacts(raw, *, course, blueprint, parsed_files, repair, max_items=4):
    result = deepcopy(raw)
    attempts = []

    def check(value):
        return build_artifacts(course, blueprint, [], "", value, parsed_files)

    initial = check(result)
    # One attempt per rejected item; never add filler to reach a quota.
    for rejected in initial.quality_report.quarantined:
        collection, index = rejected.get("collection"), rejected.get("index")
        if collection not in {"practice_questions", "mock_exam", "recall_cards"} or not isinstance(index, int):
            continue
        if len(attempts) >= max_items:
            break
        key = "active_recall_cards" if collection == "recall_cards" and "active_recall_cards" in result else collection
        items = result.get(key, [])
        if isinstance(items, dict):
            items = items.get("cards" if collection == "recall_cards" else "questions", [])
        if not isinstance(items, list) or index >= len(items):
            continue
        before = check(result)
        prior_rejections = {(r.get("collection"), r.get("index")) for r in before.quality_report.quarantined}
        if (collection,index) not in prior_rejections:continue
        def validate(candidate):
            if not isinstance(candidate, dict):
                raise ValueError("Repair must return one item object")
            original=items[index]
            if isinstance(original,dict):
                known={concept.id for concept in course.concepts}
                resolve=lambda values:{course.concept_id_aliases.get(value,value) for value in values if isinstance(value,str)}
                protected=resolve(original.get('knowledge_ids',[])) & known
                if not protected.issubset(resolve(candidate.get('knowledge_ids',[]))):
                    raise ValueError('Repair removed valid knowledge IDs from the learning objective')
                objective=original.get('learning_objective')
                if isinstance(objective,str) and objective.strip() and candidate.get('learning_objective')!=objective:
                    raise ValueError('Repair changed the original learning objective')
            trial = deepcopy(result)
            target = trial[key]
            if isinstance(target, dict):
                target = target["cards" if collection == "recall_cards" else "questions"]
            target[index] = candidate
            remaining = {(r.get("collection"), r.get("index")) for r in check(trial).quality_report.quarantined}
            if (collection, index) in remaining or not remaining.issubset(prior_rejections):
                raise ValueError("Repair is invalid or displaces a validated sibling")
            return candidate
        record = {"collection": collection, "index": index, "reason": rejected["reason"], "status": "FAILED"}
        attempts.append(record)
        try:
            items[index] = validate(repair(collection, index, deepcopy(items[index]), rejected["reason"], before, validate))
            record["status"] = "REPAIRED"
        except CheckpointWriteError:
            raise
        except Exception:
            # Keep original rejected content for diagnosis; do not replace with templates.
            pass
    return result, attempts
