from datetime import date, datetime, timezone, timedelta
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.main import app
from app.routers import review_projects as projects
from app.services import generation_store as store
from app.services.learning_assessment import assess_answer,update_mastery,recommend
from app.services.course_reconstruction import normalize_course,subject_profile
from app.services.exam_evidence import observed_exam_model,page_kind,exam_blueprint
from app.schemas.course_model import CourseModel
from app.schemas.review import ParsedFile,ParsedPage,GenerateReviewResponse,ReviewReport,GeneratedExamQuestion


def test_normalization_merges_aliases_only_with_matching_meaning():
    model=CourseModel.model_validate({'concepts':[
        {'id':'a','name':'条件概率','aliases':['conditional probability'],'definition':'Probability given an event'},
        {'id':'b','name':'conditional probability','definition':'Probability given an event'},
        {'id':'c','name':'条件概率','definition':'A different unsupported meaning'}]})
    result=normalize_course(model)
    assert len(result.concepts)==2 and result.concept_id_aliases['a']==result.concept_id_aliases['b']
    assert result.uncertainties
    assert normalize_course(model).concepts[0].id==result.concepts[0].id
    original=CourseModel.model_validate({'concepts':[{'id':'random-first','name':'条件概率','definition':'First wording','source_refs':[{'document_id':'stable-source','page':1,'anchor':'course.pdf, p. 1'}]}]})
    rephrased=original.model_copy(deep=True)
    rephrased.concepts[0].id='random-next';rephrased.concepts[0].definition='Rephrased explanation'
    assert normalize_course(original).concepts[0].id==normalize_course(rephrased).concepts[0].id


def test_observed_exam_dedup_and_no_fake_answer_or_probability():
    def source(name):
        text='1. Explain the meaning of conditional probability with its denominator condition.\n2. Describe independence and its difference from mutual exclusion.'
        return ParsedFile(filename=name,file_type='.txt',path=name,raw_text=text,pages=[ParsedPage(page_number=1,text=text,source='text_extract',source_anchor=f'{name}, p. 1')])
    model=observed_exam_model([source('a'),source('b')],{'a':'past_exam','b':'past_exam'})
    assert len(model.observed_questions)==2
    assert model.observed_questions[0].duplicate_sources
    assert all(q.answer_status=='UNVERIFIED' for q in model.observed_questions)
    assert exam_blueprint(model,'closed_book')['observed_count']==2
    assert page_kind('学号 姓名\n答题纸\n1 2 3 4')=='answer_sheet'


def test_subjective_assessment_does_not_invent_a_grade():
    result=assess_answer({'answer':'A membrane regulates transport','scoring_rubric':['membrane','regulates transport']},'membrane')
    assert result['score'] is None and result['needs_self_review']
    assert result['missing_points']==['regulates transport']
    assert assess_answer({'answer':'reference'},'learner',self_score=.5)['assessed_by']=='learner_self_report'


def test_duplicate_exam_stems_keep_their_distinct_continuations_and_sources():
    def source(name, ending):
        pages=[ParsedPage(page_number=1,text='1. Explain this calculation using the data on the next page.',source='text_extract',source_anchor=f'{name}, p. 1'),
               ParsedPage(page_number=2,text=ending+'\n2. Explain the conditions that make the calculation valid.',source='text_extract',source_anchor=f'{name}, p. 2')]
        return ParsedFile(filename=name,path=name,file_type='.pdf',pages=pages,raw_text='\n'.join(p.text for p in pages))
    model=observed_exam_model([source('a','Parameter is 10.'),source('b','Parameter is 20.')],{'a':'past_exam','b':'past_exam'})
    first=[q for q in model.observed_questions if 'next page' in q.text]
    assert len(first)==2
    assert all(len(q.source_refs)==2 for q in first)
    assert first[0].question_id!=first[1].question_id
    from app.services.exam_evidence import question_signature
    assert question_signature('1. print(A)')!=question_signature('1. print(a)')
    assert question_signature('1. if True:\n    print(1)')!=question_signature('1. if True:\nprint(1)')


def test_schema_leaf_corrections_cannot_remove_knowledge():
    from app.services.schema_corrections import apply_corrections
    original={'concepts':[{'id':'a'}],'relationships':[{'type':'bad'}]}
    errors=[{'loc':['relationships',0,'type']}]
    corrected=apply_corrections(original,errors,{'corrections':[{'path':['relationships',0,'type'],'value':'depends_on'}]})
    assert corrected['concepts']==original['concepts'] and original['relationships'][0]['type']=='bad'
    with pytest.raises(ValueError):
        apply_corrections(original,errors,{'corrections':[{'path':['concepts'],'value':[]}]})


def test_production_stage_repairs_only_bad_schema_leaf(monkeypatch):
    from tests.test_learning_engine_p0 import GroundedProvider,run
    class BadRelationProvider(GroundedProvider):
        def generate_stage(self,stage,prompt,config,*,max_output_tokens):
            import json
            if stage=='schema_repair':
                self.calls.append((stage,json.loads(prompt)['payload']))
                return {'corrections':[{'path':['relationships',0,'type'],'value':'depends_on'}]}
            value=super().generate_stage(stage,prompt,config,max_output_tokens=max_output_tokens)
            if stage=='course_model':
                value['relationships']=[{'source':'membrane','target':'membrane','type':'related_concepts'}]
            return value
    provider=BadRelationProvider()
    result,_=run(monkeypatch,provider)
    assert len(result.report.overview['course_model']['concepts'])==1
    assert [stage for stage,_ in provider.calls].count('schema_repair')==1


def test_numeric_verifier_checks_arithmetic_without_executing_code():
    from app.services.numeric_verification import verify,calculate
    assert verify('comb(5,2)*0.5**5',.3125,'probability')
    assert not verify('1/2',.75,'probability')
    assert not verify('2',2,'probability')
    for expression in ["__import__('os').system('echo unsafe')",'2**1000000','factorial(1000000)']:
        with pytest.raises((ValueError,TypeError)):
            calculate(expression)
    numeric=assess_answer({'answer':'0.5','reference_expression':'1/2','numeric_answer':.5,'quantity_kind':'probability'},'2/4')
    assert numeric['score']==1 and numeric['assessed_by']=='deterministic_arithmetic'
    assert assess_answer({'answer':'答案：B','question_type':'单选题','options':['A. First','B. Second']},'(B)')['score']==1


def test_unit_scopes_reject_objective_ids_and_preserve_prerequisites():
    from app.schemas.course_model import StudyBlueprint
    from app.services.unit_scope import validate_plan_ids,course_view
    from pydantic import ValidationError
    course=CourseModel.model_validate({'concepts':[{'id':'a','name':'Base'},{'id':'b','name':'Advanced','prerequisites':['a']},{'id':'c','name':'Other'}]})
    with pytest.raises(ValidationError):
        validate_plan_ids(StudyBlueprint(units=[{'title':'Advanced','knowledge_ids':['learning-objective-1']}]),course)
    scoped=course_view(course.model_dump(),{'knowledge_ids':['b']})
    assert {concept['id'] for concept in scoped['concepts']}=={'a','b'}
    assert scoped['unit_scope']['required_knowledge_ids']==['b']


def test_active_quality_is_not_increased_by_filler_or_question_count(monkeypatch):
    from tests.test_learning_engine_p0 import GroundedProvider,run
    from app.services.llm_quality import validate_report_quality
    result,_=run(monkeypatch,GroundedProvider())
    result.report.overview['unit_statuses']=['NEEDS_REPAIR']
    before=validate_report_quality(result.report).quality_score
    result.report.markdown+='\nGeneric study advice. '*1000
    result.report.mock_exam.questions*=10
    assert validate_report_quality(result.report).quality_score==before


def test_mastery_and_schedule_respond_to_actual_feedback():
    now=datetime(2030,1,1,tzinfo=timezone.utc)
    failed=update_mastery(None,0,'hard',now)
    success=update_mastery(None,1,'hard',now)
    assert failed['mastery']<success['mastery']
    repeated=update_mastery(success,1,'hard',now+timedelta(days=1))
    assert repeated['interval_days']>success['interval_days']
    same_day=update_mastery(success,1,'hard',now+timedelta(minutes=1))
    assert same_day['interval_days']==success['interval_days']
    concepts=[{'id':'a','name':'Foundations','importance':50,'difficulty':50},
              {'id':'b','name':'Advanced','importance':95,'difficulty':50,'prerequisites':['a']}]
    plan=recommend(concepts,{'a':failed,'b':success},{'exam_date':'2030-01-02','daily_minutes':20,'target_score':'90'},today=date(2030,1,1))
    assert plan['items'][0]['knowledge_id']=='a'
    assert sum(item['estimated_minutes'] for item in plan['items'])<=20


def test_exam_date_changes_which_topic_fits_the_daily_budget():
    concepts=[{'id':'exam-core','name':'Exam core','importance':95,'difficulty':50},
              {'id':'foundation','name':'Foundation','importance':55,'difficulty':50}]
    states={'exam-core':{'mastery':.5},'foundation':{'mastery':.25}}
    near=recommend(concepts,states,{'exam_date':'2030-01-02','daily_minutes':10,'target_score':'80'},today=date(2030,1,1))
    far=recommend(concepts,states,{'exam_date':'2030-02-01','daily_minutes':10,'target_score':'80'},today=date(2030,1,1))
    assert near['items'][0]['knowledge_id']=='exam-core'
    assert far['items'][0]['knowledge_id']=='foundation'


@pytest.fixture
def learning_client(tmp_path,monkeypatch):
    engine=create_engine('sqlite:///'+str(tmp_path/'workspace.db'),connect_args={'check_same_thread':False})
    monkeypatch.setattr(projects,'engine',engine)
    monkeypatch.setattr(projects,'SessionLocal',sessionmaker(bind=engine,expire_on_commit=False))
    monkeypatch.setenv('RECALLFORGE_JOB_DB',str(tmp_path/'jobs.db'))
    with TestClient(app) as client:
        def workspace():
            data=client.post('/api/review-projects/workspace',json={}).json()
            return {'X-Workspace-Id':data['workspace_id'],'X-Workspace-Secret':data['workspace_secret']}
        headers=workspace();other=workspace()
        project=client.post('/api/review-projects/projects',headers=headers,json={'course_name':'Synthetic','daily_minutes':30}).json()
        job,_=store.create_or_resume('owned-job',{'files':['fake.txt'],'project_id':project['id']})
        question=GeneratedExamQuestion(question_id='p1',question_type='multiple_choice',question='Which option follows the source?',options=['A. First','B. Second'],answer='A',knowledge_ids=['k1'],scoring_rubric=['First'])
        report=ReviewReport(title='Synthetic',summary='Source',practice_questions=[question],overview={'artifacts':{'course_model':{'concepts':[{'id':'k1','name':'Concept','importance':70,'difficulty':30}]}}})
        result=GenerateReviewResponse(review_report=report,markdown='# Source',export_format='md')
        store.update_job(job,status='completed',progress=100,message='done',result=result.model_dump(mode='json'))
        yield client,headers,other,job
    engine.dispose()


def test_api_attempts_persist_restore_and_are_idempotent_and_owned(learning_client):
    client,headers,other,job=learning_client
    endpoint=f'/api/review-projects/learning/jobs/{job}/attempts'
    payload={'item_id':'p1','kind':'practice','answer':'A','nonce':'unique-attempt'}
    assert client.post(endpoint,headers=other,json=payload).status_code==404
    first=client.post(endpoint,headers=headers,json=payload)
    assert first.status_code==200 and first.json()['assessment']['score']==1
    assert client.post(endpoint,headers=headers,json=payload).json()['attempt_id']==first.json()['attempt_id']
    assert client.post(endpoint,headers=headers,json={**payload,'answer':'B'}).status_code==409
    progress=client.get(f'/api/review-projects/learning/jobs/{job}/progress',headers=headers).json()
    assert len(progress['attempts'])==1 and progress['mastery']['k1']['attempts']==1
    assert progress['recommendation']['items']
    assert client.post(endpoint,headers=headers,json={**payload,'nonce':'exam','kind':'exam'}).status_code==404
    # Additive table initialization must not remove existing projects/attempts.
    projects.initialize_review_database()
    assert len(client.get('/api/review-projects/projects',headers=headers).json())==1
