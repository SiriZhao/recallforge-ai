"""Conservative entity normalization and shared subject-specific contracts."""
import re
import unicodedata
from app.services.stage_checkpoints import stable_hash


SUBJECT_PROFILES = {
    "probability": {"focus": ["random variables and distributions", "formula conditions", "derivations", "worked calculations"], "checks": ["probabilities in [0,1]", "independence must be justified", "normalization and parameter domains"]},
    "programming": {"focus": ["code reading", "output prediction", "debugging", "implementation and test cases"], "checks": ["complete input and expected behavior", "language version", "code must not be executed without sandboxing"]},
    "botany": {"focus": ["structure and function", "classification", "comparisons", "mechanisms and experiments"], "checks": ["taxonomic context", "diagram evidence availability", "control and observation when applicable"]},
    "organic_chemistry": {"focus": ["molecular structure", "reaction conditions", "mechanism", "isomerism"], "checks": ["reactants and products specified", "temperature and catalysts where relevant", "do not infer missing structural diagrams"]},
    "general": {"focus": ["definitions", "conditions", "mechanisms", "comparison and application"], "checks": ["source support", "specific explanation", "answerability"]},
}


def subject_profile(title, parsed_files, exam_type="unknown"):
    sample = (title + " " + " ".join(p.raw_text[:8000] for p in parsed_files)).casefold()
    markers = {"probability": ("概率论", "probability", "随机变量"), "programming": ("python", "程序设计", "programming"),
               "botany": ("植物学", "botany"), "organic_chemistry": ("有机化学", "organic chemistry")}
    domain = "programming" if exam_type == "programming" else next((key for key, terms in markers.items() if any(term in sample for term in terms)), "general")
    return {"domain": domain, **SUBJECT_PROFILES[domain], "selection_basis": "course label/material terminology; not a learned classifier"}


def normalized(value):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value)).casefold()


def normalize_course(course):
    course = course.model_copy(deep=True)
    groups, by_name, aliases = {}, {}, {}
    for concept in course.concepts:
        names = {normalized(name) for name in [concept.name, *concept.aliases] if name.strip()}
        definition = normalized(concept.definition)
        # Alias overlap alone is not proof of identical meaning. Require an
        # exact nonempty definition before merging across files.
        existing = next((item for item in groups.values() if definition and normalized(item.definition) == definition and names.intersection({normalized(n) for n in [item.name, *item.aliases]})), None)
        if existing:
            aliases[concept.id] = existing.id
            existing.aliases = list(dict.fromkeys([*existing.aliases, concept.name, *concept.aliases]))
            existing.source_refs = list({ref.model_dump_json(): ref for ref in [*existing.source_refs, *concept.source_refs]}.values())
            existing.importance = max(existing.importance, concept.importance)
            continue
        old_id = concept.id
        meaning_key=definition or [sorted(ref.anchor for ref in concept.source_refs),old_id]
        concept.id = "k-" + stable_hash([course.subject_domain, normalized(concept.name), meaning_key])[:20]
        aliases[old_id] = concept.id
        if normalized(concept.name) in by_name and by_name[normalized(concept.name)] != definition:
            course.uncertainties.append(f"CONCEPT_MEANING_CONFLICT:{concept.name}: distinct definitions retained; review source/version context")
        by_name[normalized(concept.name)] = definition
        groups[concept.id] = concept
    course.concepts = list(groups.values())
    for concept in course.concepts:
        concept.prerequisites = list(dict.fromkeys(aliases.get(value,value) for value in concept.prerequisites))
        concept.related_concepts = list(dict.fromkeys(aliases.get(value,value) for value in concept.related_concepts))
    seen = set()
    relations = []
    for relation in course.relationships:
        relation.source = aliases.get(relation.source,relation.source)
        relation.target = aliases.get(relation.target,relation.target)
        key = (relation.source,relation.target,relation.type)
        if relation.source != relation.target and key not in seen:
            seen.add(key)
            relations.append(relation)
    course.relationships = relations
    course.concept_id_aliases = aliases
    return course
