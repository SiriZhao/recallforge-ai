import json
import pytest
from app.schemas.course_model import CourseModel,UnitDraft
from app.schemas.teaching_quality import UnitCritique
from app.services.repair_scope import apply_patches,localized_ranges,validate_unit_scope
from app.services.evidence_catalog import catalog_for
from tests.test_learning_engine_p0 import GroundedProvider,run,materials


def test_complete_long_critic_reaches_repair_and_is_rechecked(monkeypatch):
    class CompleteCritic(GroundedProvider):
        def generate_stage(self,stage,prompt,config,*,max_output_tokens):
            payload=json.loads(prompt)['payload']
            if stage=='unit_critic' and 'Corrected condition' not in payload['unit']['markdown']:
                self.calls.append((stage,payload))
                return {'verdict':'NEEDS_REPAIR','issues':['Detailed issue '+str(i)+'x'*120 for i in range(20)]+['FINAL_CRITICAL_CONDITION'],
                    'claims':[{'claim':'Condition needs repair','basis':'INSUFFICIENT'}]}
            if stage=='unit_repair':
                self.calls.append((stage,payload))
                assert payload['critique']['issues'][-1]=='FINAL_CRITICAL_CONDITION'
                assert len(payload['defect'])>1000
                return {**payload['unit'],'markdown':payload['unit']['markdown']+'\nCorrected condition'}
            return super().generate_stage(stage,prompt,config,max_output_tokens=max_output_tokens)
    provider=CompleteCritic();result,_=run(monkeypatch,provider)
    assert result.llm_status=='success'
    assert len([name for name,_ in provider.calls if name=='unit_critic'])==2
    assert result.report.overview['repair_audits'][0]['post_review']=='READY'


def test_minor_typo_uses_localized_patch_and_retains_other_unit(monkeypatch):
    class TypoProvider(GroundedProvider):
        def generate_stage(self,stage,prompt,config,*,max_output_tokens):
            payload=json.loads(prompt)['payload']
            if stage=='study_blueprint':
                value=super().generate_stage(stage,prompt,config,max_output_tokens=max_output_tokens)
                value['units'].append({**value['units'][0],'unit_id':'second','title':'Second unit'})
                return value
            if stage=='unit_draft':
                value=super().generate_stage(stage,prompt,config,max_output_tokens=max_output_tokens)
                if payload['unit'].get('unit_id')=='second':value['markdown']+='\nTypoo'
                return value
            if stage=='unit_critic' and 'Typoo' in payload['unit']['markdown']:
                self.calls.append((stage,payload));start=payload['unit']['markdown'].index('Typoo')
                return {'verdict':'NEEDS_REPAIR','claims':[{'claim':'Membrane controls transport','basis':'SUPPORTED','source_refs':[{'anchor':'synthetic.txt, p. 1'}]}],
                    'findings':[{'category':'wording','severity':'minor','message':'Typo','text_start':start,'text_end':start+5,'quote':'Typoo'}]}
            if stage=='unit_patch':
                self.calls.append((stage,payload));start,end=payload['allowed_ranges'][0]
                return {'patches':[{'start':start,'end':end,'replacement':'Typo'}]}
            return super().generate_stage(stage,prompt,config,max_output_tokens=max_output_tokens)
    provider=TypoProvider();result,_=run(monkeypatch,provider)
    assert result.llm_status=='success'
    units=result.report.overview['artifacts']['learning_material']['units']
    assert units[0]['markdown']=='## Membrane\nThe membrane controls transport [synthetic.txt, p. 1].'
    assert units[1]['markdown'].endswith('Typo')
    assert [name for name,_ in provider.calls].count('unit_patch')==1
    assert result.report.overview['repair_audits'][0]['sibling_units_unchanged']


def test_patch_cannot_change_outside_span_or_expand_to_a_new_lecture():
    original=UnitDraft(unit_id='u',title='Source',markdown='Membrane Typoo remains.')
    start=original.markdown.index('Typoo');ranges=[(start,start+5)]
    for patches in [[{'start':0,'end':2,'replacement':'Other'}],
                    [{'start':start,'end':start+5,'replacement':'x'*1000}],
                    [{'start':start,'end':start+5,'replacement':'Typo\nNew chapter'}]]:
        with pytest.raises(ValueError):apply_patches(original,{'patches':patches},ranges)


def test_scope_blocks_deleted_concepts_and_undisputed_sources():
    course=CourseModel.model_validate({'concepts':[{'id':'k','name':'Membrane'}]})
    original=UnitDraft(unit_id='u',title='Source',markdown='Membrane controls transport.',source_refs=[{'anchor':'synthetic.txt, p. 1'}])
    spans=catalog_for(materials());plan={'knowledge_ids':['k']}
    for change in [{'markdown':'A generic study tip.'},{'source_refs':[]}]:
        candidate=UnitDraft.model_validate({**original.model_dump(),**change})
        with pytest.raises(ValueError):validate_unit_scope(original,candidate,course,plan,None,spans)


def test_malicious_item_repair_cannot_change_learning_target(monkeypatch):
    class TargetSwap(GroundedProvider):
        def generate_stage(self,stage,prompt,config,*,max_output_tokens):
            if stage=='artifact_repair':
                payload=json.loads(prompt)['payload']
                return {**payload['item'],'answer':'Controls transport','learning_objective':'Memorize an easier label'}
            return super().generate_stage(stage,prompt,config,max_output_tokens=max_output_tokens)
    result,_=run(monkeypatch,TargetSwap(invalid_question=True))
    assert result.llm_status=='partial' and not result.report.practice_questions
    assert result.report.mock_exam.questions[0].question_id=='e1'
