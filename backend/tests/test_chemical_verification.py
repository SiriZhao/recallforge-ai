import pytest
from app.schemas.chemical_reaction import ChemicalReaction
from app.services.chemical_verification import atoms,check_reaction
from tests.test_learning_engine_p0 import GroundedProvider,run


def reaction(left,right):
    return ChemicalReaction(reactants=left,products=right)


def test_formula_parser_keeps_element_case_and_bounded_groups():
    assert atoms('Co')=={'Co':1}
    assert atoms('CO')=={'C':1,'O':1}
    assert atoms('Ca(OH)2')=={'Ca':1,'O':2,'H':2}
    assert atoms('CH3COOH')=={'C':2,'H':4,'O':2}
    for value in ['co','C6H5-R','CH₄','Fe3+','CuSO4.5H2O','C0','()','(CH3','C1001','('*12+'C'+')'*12]:
        with pytest.raises(ValueError):atoms(value)


def test_atom_and_explicit_charge_conservation_are_not_reaction_proofs():
    balanced=reaction([{'formula':'C2H6','coefficient':2},{'formula':'O2','coefficient':7}],
        [{'formula':'CO2','coefficient':4},{'formula':'H2O','coefficient':6}])
    checked=check_reaction(balanced)
    assert checked['status']=='BALANCED'
    assert checked['reaction_feasibility_verified'] is False
    wrong=balanced.model_copy(deep=True)
    wrong.products[1].coefficient=5
    assert check_reaction(wrong)['status']=='UNBALANCED'
    charged=reaction([{'formula':'Fe','charge':3}],[{'formula':'Fe','charge':2}])
    assert check_reaction(charged)['status']=='UNBALANCED'
    unsupported=reaction([{'formula':'RCl'}],[{'formula':'ROH'}])
    assert check_reaction(unsupported)['status']=='UNSUPPORTED'


def test_unbalanced_generated_question_is_quarantined_not_replaced_by_template(monkeypatch):
    class ChemicalProvider(GroundedProvider):
        def generate_stage(self,stage,prompt,config,*,max_output_tokens):
            value=super().generate_stage(stage,prompt,config,max_output_tokens=max_output_tokens)
            if stage=='optional_artifacts':
                value['practice_questions'][0]['chemical_reaction']={
                    'reactants':[{'formula':'H2'},{'formula':'O2'}],
                    'products':[{'formula':'H2O'}]}
            if stage=='artifact_repair':raise RuntimeError('No invented repair')
            return value
    result,_=run(monkeypatch,ChemicalProvider())
    assert result.llm_status=='partial'
    assert result.report.practice_questions==[]
    quality=result.report.overview['artifacts']['quality_report']
    assert any('chemical_reaction_unbalanced' in item['reason'] for item in quality['quarantined'])
    assert result.report.mock_exam.questions


def test_balanced_reaction_survives_legacy_projection_without_schema_migration(monkeypatch):
    class ChemicalProvider(GroundedProvider):
        def generate_stage(self,stage,prompt,config,*,max_output_tokens):
            value=super().generate_stage(stage,prompt,config,max_output_tokens=max_output_tokens)
            if stage=='optional_artifacts':
                value['practice_questions'][0]['chemical_reaction']={
                    'reactants':[{'formula':'H2','coefficient':2},{'formula':'O2'}],
                    'products':[{'formula':'H2O','coefficient':2}]}
            return value
    result,_=run(monkeypatch,ChemicalProvider())
    assert result.llm_status=='success'
    assert result.report.practice_questions[0].chemical_reaction.products[0].coefficient==2
    checks=result.report.overview['artifacts']['quality_report']['verification_checks']
    assert checks[0]['status']=='BALANCED'
    assert checks[0]['structural_identity_verified'] is False
