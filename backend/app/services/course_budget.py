"""Bound course reconstruction inputs and merge every batch without truncation."""
import re
from app.schemas.course_model import CourseModel
from app.services.llm_capabilities import estimate_tokens
from app.services.course_reconstruction import normalize_course


def reconstruct_course(payload,*,stage,make_prompt,input_budget,output_budget,validate):
    def pack(chunks):
        value={**payload,'chunks':chunks}
        key=lambda text:re.sub(r'\s+','',text or '').casefold()
        anchors={key(ref.get('anchor')) for chunk in chunks for ref in chunk.get('source_refs',[]) if isinstance(ref,dict) and ref.get('anchor')}
        value['primary_evidence']=[span for span in payload.get('primary_evidence',[]) if not anchors or key(span['source']['anchor']) in anchors]
        return value
    def fits(value):return estimate_tokens(make_prompt('course_model',value))<=input_budget
    if fits(payload):return stage('course_model',payload,output_budget,validate)
    batches=[];current=[]
    for chunk in payload['chunks']:
        if not fits(pack([chunk])):
            raise ValueError('COURSE_CONTEXT_EXCEEDED: one evidence batch exceeds budget; no chunk was silently omitted')
        if current and not fits(pack([*current,chunk])):batches.append(current);current=[]
        current.append(chunk)
    if current:batches.append(current)
    merged=CourseModel(course_title=payload.get('title',''))
    for index,batch in enumerate(batches):
        course=stage('course_model',pack(batch),output_budget,validate,f'course-batch-{index}')
        course=course.model_copy(deep=True)
        # Providers often restart IDs at c1 in each batch. Namespace before
        # merging so references cannot silently bind to another batch's c1.
        mapping={concept.id:f'b{index}:{concept.id}' for concept in course.concepts}
        formula_ids={item['id']:f'b{index}:{item["id"]}' for item in course.formulas if item.get('id')}
        example_ids={item['id']:f'b{index}:{item["id"]}' for item in course.examples if item.get('id')}
        for concept in course.concepts:
            concept.id=mapping[concept.id]
            concept.prerequisites=[mapping.get(key,key) for key in concept.prerequisites]
            concept.related_concepts=[mapping.get(key,key) for key in concept.related_concepts]
            concept.formula_refs=[formula_ids.get(key,key) for key in concept.formula_refs]
            concept.example_refs=[example_ids.get(key,key) for key in concept.example_refs]
        for entities,id_mapping in ((course.formulas,formula_ids),(course.examples,example_ids)):
            for item in entities:
                if item.get('id'):item['id']=id_mapping[item['id']]
                if isinstance(item.get('knowledge_ids'),list):
                    item['knowledge_ids']=[mapping.get(key,key) for key in item['knowledge_ids']]
        for relation in course.relationships:
            relation.source=mapping.get(relation.source,relation.source)
            relation.target=mapping.get(relation.target,relation.target)
        for field in ('concepts','relationships','chapters','formulas','examples','learning_objectives','exam_signals','uncertainties'):
            getattr(merged,field).extend(getattr(course,field))
        if merged.subject_domain=='other':merged.subject_domain=course.subject_domain
    merged.uncertainties.append('CROSS_BATCH_RELATION_COVERAGE_UNVERIFIED: all batch entities retained; additional global dependencies are not invented')
    return normalize_course(merged)
