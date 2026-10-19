"""Explicit knowledge ownership for each teaching unit and its prerequisites."""
from copy import deepcopy
from pydantic import ValidationError


def scope_error(location, value, message):
    return ValidationError.from_exception_data('UnitScope',[{'type':'value_error','loc':tuple(location),'input':value,'ctx':{'error':ValueError(message)}}])


def validate_plan_ids(blueprint, course):
    known={concept.id for concept in course.concepts}
    for index,unit in enumerate(blueprint.units):
        unit.knowledge_ids=[course.concept_id_aliases.get(key,key) for key in unit.knowledge_ids]
        if any(key not in known for key in unit.knowledge_ids):
            raise scope_error(('units',index,'knowledge_ids'),unit.knowledge_ids,'Use only supplied CourseModel concept IDs, not learning-objective IDs')
    return blueprint


def course_view(course_data, plan):
    required=set(plan.get('knowledge_ids',[]))
    if not required:return course_data
    by_id={concept['id']:concept for concept in course_data.get('concepts',[])}
    included=set(required)
    pending=list(required)
    while pending:
        concept=by_id.get(pending.pop(),{})
        for key in concept.get('prerequisites',[]):
            if key not in included:included.add(key);pending.append(key)
    view=deepcopy(course_data)
    view['concepts']=[concept for key,concept in by_id.items() if key in included]
    view['relationships']=[edge for edge in course_data.get('relationships',[]) if edge['source'] in included and edge['target'] in included]
    view['unit_scope']={'required_knowledge_ids':sorted(required),'prerequisite_ids':sorted(included-required),
        'coverage_rule':'Only required knowledge IDs determine missing coverage; other evidence is context, not extra required topics.'}
    return view
