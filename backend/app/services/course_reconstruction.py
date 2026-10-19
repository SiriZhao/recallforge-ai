"""Conservative entity normalization and shared subject-specific contracts."""
import re
import unicodedata
from collections import Counter
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
    groups, by_name, original_groups = [], {}, {}
    for concept in course.concepts:
        names = {normalized(name) for name in [concept.name, *concept.aliases] if name.strip()}
        definition = normalized(concept.definition)
        # Alias overlap alone is not proof of identical meaning. Require an
        # exact nonempty definition before merging across files.
        match = next((index for index,item in enumerate(groups) if definition and normalized(item.definition) == definition and names.intersection({normalized(n) for n in [item.name, *item.aliases]})), None)
        existing = groups[match] if match is not None else None
        if existing:
            original_groups.setdefault(concept.id,set()).add(match)
            existing.aliases = list(dict.fromkeys([*existing.aliases, concept.name, *concept.aliases]))
            existing.source_refs = list({ref.model_dump_json(): ref for ref in [*existing.source_refs, *concept.source_refs]}.values())
            existing.importance = max(existing.importance, concept.importance)
            continue
        original_groups.setdefault(concept.id,set()).add(len(groups))
        if normalized(concept.name) in by_name and by_name[normalized(concept.name)] != definition:
            course.uncertainties.append(f"CONCEPT_MEANING_CONFLICT:{concept.name}: distinct definitions retained; review source/version context")
        by_name[normalized(concept.name)] = definition
        groups.append(concept)
    identity_keys=[]
    for concept in groups:
        # Stable source identity survives paraphrased model definitions. Missing
        # provenance falls back to the definition; no unverifiable global merge.
        locations=sorted({(ref.document_id or ref.filename or ref.anchor.split(',')[0],ref.page or ref.slide or 0,normalized(ref.anchor)) for ref in concept.source_refs if ref.document_id or ref.filename or ref.anchor})
        names=sorted({normalized(name) for name in [concept.name,*concept.aliases] if name.strip()})
        identity_keys.append([course.subject_domain,names[0] if names else '',locations or normalized(concept.definition)])
    occurrences=Counter(stable_hash(key) for key in identity_keys)
    for concept,key in zip(groups,identity_keys):
        if occurrences[stable_hash(key)]>1:
            key=[*key,normalized(concept.definition),concept.id]
        concept.id='k-'+stable_hash(key)[:20]
    aliases={}
    for old_id,indices in original_groups.items():
        if len(indices)==1:aliases[old_id]=groups[next(iter(indices))].id
        else:course.uncertainties.append(f'AMBIGUOUS_PROVIDER_ID:{old_id}: relation references require repair')
    course.concepts = groups
    for concept in course.concepts:
        concept.prerequisites = list(dict.fromkeys(aliases.get(value,value) for value in concept.prerequisites))
        concept.related_concepts = list(dict.fromkeys(aliases.get(value,value) for value in concept.related_concepts))
    seen = {}
    relations = []
    for relation in course.relationships:
        relation.source = aliases.get(relation.source,relation.source)
        relation.target = aliases.get(relation.target,relation.target)
        key = (relation.source,relation.target,relation.type)
        if relation.source == relation.target:continue
        if key not in seen:
            seen[key]=relation;relations.append(relation)
        else:
            prior=seen[key]
            prior.source_refs=list({ref.model_dump_json():ref for ref in [*prior.source_refs,*relation.source_refs]}.values())
    course.relationships = relations
    course.concept_id_aliases = aliases
    return course
