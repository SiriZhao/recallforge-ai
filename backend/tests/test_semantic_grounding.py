import json
import pytest
from app.schemas.course_model import CourseModel, SourceRef
from app.services.course_reconstruction import normalize_course
from app.services.evidence_catalog import catalog_for, resolves, support_trace
from tests.test_learning_engine_p0 import GroundedProvider, run, materials


@pytest.mark.parametrize('domain,name_a,name_b,definition_a,definition_b', [
    ('programming', 'Value', 'value', 'A variable', 'A variable'),
    ('organic_chemistry', 'Co', 'CO', 'A symbol', 'A symbol'),
    ('probability', 'P(A)', 'P(a)', 'An event probability', 'An event probability'),
    ('programming', 'print', 'print', 'print("a b")', 'print("ab")'),
    ('programming', 'if', 'if', 'if True:\n    print(1)', 'if True:\nprint(1)'),
    ('organic_chemistry', 'formula', 'formula', 'H₂O', 'H2O'),
])
def test_scientific_meanings_are_not_folded_together(domain,name_a,name_b,definition_a,definition_b):
    course=CourseModel(subject_domain=domain,concepts=[
        {'id':'a','name':name_a,'definition':definition_a},
        {'id':'b','name':name_b,'definition':definition_b}])
    result=normalize_course(course)
    assert len(result.concepts)==2
    assert result.concept_id_aliases['a']!=result.concept_id_aliases['b']
    assert course.concepts[0].id=='a'


def test_exact_scientific_duplicates_still_merge_with_old_id_mapping():
    course=CourseModel(subject_domain='programming',concepts=[
        {'id':identifier,'name':'print','definition':'print("a b")'} for identifier in ['a','b']])
    result=normalize_course(course)
    assert len(result.concepts)==1
    assert result.concept_id_aliases['a']==result.concept_id_aliases['b']
    again=normalize_course(result)
    assert again.concepts[0].id==result.concepts[0].id
    assert again.concept_id_aliases['a']==result.concepts[0].id


def test_quote_matching_does_not_erase_string_or_indentation_differences():
    spans=catalog_for(materials('print("a  b")\nif True:\n    print(1)'))
    ref=SourceRef(anchor='synthetic.txt, p. 1',fragment='print("a b")')
    assert not resolves(ref,spans)
    assert not resolves(ref.model_copy(update={'fragment':'if True:\nprint(1)'}),spans)
    assert support_trace(ref.model_copy(update={'fragment':''}),spans)['status']=='LOCATOR_ONLY'
    trace=support_trace(ref.model_copy(update={'fragment':'print("a  b")'}),spans)
    assert trace['status']=='QUOTED_SOURCE'
    assert trace['entailment']=='NOT_DETERMINISTICALLY_VERIFIED'


def test_ready_critic_with_only_page_locator_cannot_pass(monkeypatch):
    class LocatorCritic(GroundedProvider):
        def generate_stage(self,stage,prompt,config,*,max_output_tokens):
            result=super().generate_stage(stage,prompt,config,max_output_tokens=max_output_tokens)
            if stage=='unit_critic':
                result['claims'][0]['source_refs'][0].pop('fragment')
            if stage=='unit_repair':
                return json.loads(prompt)['payload']['unit']
            return result
    result,_=run(monkeypatch,LocatorCritic())
    assert result.llm_status=='partial'
    assert result.report.overview['artifacts']['learning_material']['units'][0]['status']=='NEEDS_REPAIR'


def test_validated_sources_reach_existing_question_and_card_fields(monkeypatch):
    result,_=run(monkeypatch,GroundedProvider())
    assert result.llm_status=='success'
    report=result.report
    for item in [*report.practice_questions,*report.mock_exam.questions,*report.anki_cards]:
        assert item.source_hint=='synthetic.txt, p. 1'
    assert 'not independently certified' in report.practice_questions[0].source_basis
    quality=report.overview['unit_quality'][0]
    assert quality['support_traces'][0]['sources'][0]['status']=='QUOTED_SOURCE'
