import json
from app.schemas.review import ParsedFile,ParsedPage,DocumentStructure,DocumentSection
from app.schemas.course_model import CourseModel
from app.services.generation_pipeline import build_chapter_sources
from app.services.chunking import split_semantic_chunks
from app.services.course_budget import reconstruct_course
from app.services.llm_capabilities import estimate_tokens
from app.services.learning_evaluation import observable_metrics


def test_same_page_sections_keep_preamble_and_do_not_duplicate_whole_pages():
    text='Intro unique\n## Chapter 1 Alpha\nAlpha unique\n## Chapter 2 Beta\nBeta unique'
    parsed=ParsedFile(filename='course.txt',file_type='.txt',path='course.txt',raw_text=text,
        pages=[ParsedPage(page_number=1,text=text,source='text_extract',source_anchor='course.txt, p. 1')],
        document_structure=DocumentStructure(document_id='doc',filename='course.txt',file_type='.txt',title='Course',page_count=1,
            sections=[DocumentSection(section_id=str(i),title=title,page_start=1,page_end=1,source_anchor='course.txt, p. 1') for i,title in enumerate(['Chapter 1 Alpha','Chapter 2 Beta'])]))
    chapters=build_chapter_sources([parsed])
    combined='\n'.join(chapter.text for chapter in chapters)
    assert len(chapters)==2
    assert all(combined.count(term)==1 for term in ['Intro unique','Alpha unique','Beta unique'])


def test_continuation_chunks_keep_their_current_page_anchor():
    text='[course.pdf, p. 1]\n'+'\n\n'.join('First page paragraph. '*10 for _ in range(8))
    text+='\n\n[course.pdf, p. 2]\n'+'\n\n'.join('Second page paragraph. '*10 for _ in range(8))
    chunks=split_semantic_chunks(text,max_chars=300,overlap_chars=0)
    assert len(chunks)>5 and all(chunk.source_anchors for chunk in chunks)
    assert all('[course.pdf, p. 2]' in chunk.source_anchors for chunk in chunks if 'Second page' in chunk.text)


def test_batched_course_reconstruction_preserves_every_chunk_and_namespaces_ids():
    chunks=[{'chunk_id':str(i),'semantic_summary':f'Unique{i} '+'explanation '*80,'source_refs':[{'anchor':f'doc, p. {i+1}'}]} for i in range(8)]
    consumed=[]
    def prompt(name,payload):return json.dumps(payload)
    def stage(name,payload,limit,validate,identity='course'):
        assert estimate_tokens(prompt(name,payload))<=650
        consumed.extend(chunk['chunk_id'] for chunk in payload['chunks'])
        return validate({'concepts':[{'id':f'c{i}','name':chunk['chunk_id'],'definition':'Meaning '+chunk['chunk_id']} for i,chunk in enumerate(payload['chunks'])]})
    course=reconstruct_course({'title':'Long course','chunks':chunks,'primary_evidence':[]},stage=stage,make_prompt=prompt,
        input_budget=650,output_budget=1000,validate=CourseModel.model_validate)
    assert consumed==[str(i) for i in range(8)]
    assert len(course.concepts)==8 and len({concept.id for concept in course.concepts})==8
    assert any('CROSS_BATCH' in warning for warning in course.uncertainties)


def test_metrics_keep_unknown_truth_unknown_and_count_unresolved_citations():
    manifest={'source_data':[{'source_ref':{'filename':'synthetic.pdf','anchor':'synthetic.pdf, p. 1','page':1},'text':'A membrane controls transport.'}]}
    output={'course_model':{'concepts':[{'name':'Membrane','source_refs':[{'anchor':'invented.pdf, p. 9'}]}]},
        'practice_questions':[{'question':'Explain transport','answer':'Reference','explanation':'Solution'}],
        'mock_exam':{'questions':[{'question':'Explain transport','answer':'Reference','explanation':'Solution'}]},'active_recall_cards':[],'canonical_markdown':'Body'}
    metrics=observable_metrics(output,manifest)
    assert metrics['location_resolution_rate']==0
    assert metrics['practice_exam_overlap']==1
    assert metrics['answer_correctness'] is None and metrics['factual_error_rate'] is None
