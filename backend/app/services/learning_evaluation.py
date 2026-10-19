"""Comparable observable metrics. Unknown truth metrics remain null, never zero."""
from collections import Counter
from app.schemas.course_model import SourceRef
from app.schemas.review import ParsedFile,ParsedPage
from app.services.evidence_catalog import catalog_for,resolves
from app.services.exam_evidence import question_signature
from app.services.numeric_verification import verify


def source_files(manifest):
    grouped={}
    for item in manifest['source_data']:
        ref=item['source_ref'];name=ref['filename']
        grouped.setdefault(name,[]).append(ParsedPage(page_number=ref['page'],source='text_extract',text=item['text'],source_anchor=ref['anchor']))
    return [ParsedFile(filename=name,original_filename=name,path=name,file_type='.pdf',pages=pages,
        raw_text='\n'.join(page.text for page in pages)) for name,pages in grouped.items()]


def observable_metrics(payload,manifest,*,is_report=False):
    if is_report:
        report=payload.get('result',payload)
        root=report.get('overview',{}).get('artifacts',{})
        course=root.get('course_model',{})
        practice=root.get('question_bank',{}).get('questions',[])
        exams=root.get('mock_exam',{}).get('questions',[])
        cards=root.get('recall_cards',{}).get('cards',[])
        text=report.get('markdown','')
        state=payload.get('status','UNKNOWN')
    else:
        root=payload
        course=root.get('course_model',{})
        practice=root.get('practice_questions',[])
        exams=root.get('mock_exam',{}).get('questions',[])
        cards=root.get('active_recall_cards',[])
        text=root.get('canonical_markdown','')
        state='UNVALIDATED_CHAT_OUTPUT'
    practice=practice if isinstance(practice,list) else []
    exams=exams if isinstance(exams,list) else []
    cards=cards if isinstance(cards,list) else []
    entities=course.get('concepts',[])+course.get('relationships',[])+practice+exams+cards
    spans=catalog_for(source_files(manifest))
    references=[ref for item in entities if isinstance(item,dict) for ref in item.get('source_refs',[])]
    resolved=0
    for ref in references:
        try:resolved+=int(resolves(SourceRef.model_validate(ref),spans))
        except (ValueError,TypeError):pass
    questions=practice+exams
    complete=sum(bool(q.get('question','').strip() and q.get('answer','').strip() and q.get('explanation','').strip()) for q in questions)
    signatures=[question_signature(q.get('question','')) for q in questions]
    overlap=len({question_signature(q.get('question','')) for q in practice}&{question_signature(q.get('question','')) for q in exams})
    numeric=[q for q in questions if q.get('reference_expression') is not None]
    return {'state':state,'concepts':len(course.get('concepts',[])),'practice_questions':len(practice),'exam_questions':len(exams),'recall_cards':len(cards),
        'location_resolution_rate':resolved/len(references) if references else None,
        'reference_count':len(references),'citation_completeness':sum(bool(item.get('source_refs')) for item in entities)/len(entities) if entities else None,
        'structurally_answerable_rate':complete/len(questions) if questions else None,
        'duplicate_question_rate':(len(signatures)-len(set(signatures)))/len(signatures) if signatures else None,
        'practice_exam_overlap':overlap,'canonical_characters':len(text),
        'arithmetic_self_consistency_rate':sum(verify(q['reference_expression'],q.get('numeric_answer'),q.get('quantity_kind','number')) for q in numeric)/len(numeric) if numeric else None,
        'gold_knowledge_coverage':None,'factual_error_rate':None,'answer_correctness':None,'pedagogical_depth_human_rating':None,
        'diagnosis_quality_human_rating':None,'human_evaluation':'NOT_PERFORMED',
        'limitations':['Citation resolution does not establish entailment.','Complete answer fields do not establish correctness.','No independent human gold answers have been supplied.']}
