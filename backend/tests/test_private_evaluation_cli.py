"""Private trial orchestration uses synthetic input and no real network/key."""
import json
from types import SimpleNamespace
from app.cli import evaluate_courses as cli
from app.schemas.review import ReviewReport


def test_revision_regression_keeps_global_ledger_and_original_results(tmp_path,monkeypatch):
    from app.services import generation_pipeline
    manifest={'source_hash':'synthetic-content','objective':'Test concept',
        'source_data':[{'source_ref':{'filename':'synthetic.pdf','page':1,'anchor':'synthetic.pdf, p. 1'},'text':'A test concept.'}]}
    cli.save(tmp_path/'botany-manifest.json',manifest)
    cli.save(tmp_path/'botany-recallforge.json',{'original':'immutable'})
    cli.save(tmp_path/'botany-checkpoints.json',{'cached-stage':{'status':'completed','content':{'verified':True}}})
    ledgers=[]
    class Client:
        model='fake-model'
        def __init__(self,path,**kwargs):ledgers.append(path)
        def summary(self):return {'accounted_upper_usd':.1,'uncertain_requests':0}
    monkeypatch.setattr(cli,'EvaluationClient',Client)
    monkeypatch.setattr(cli,'dotenv_values',lambda _: {'DEEPSEEK_API_KEY':'fake-only'})
    monkeypatch.setattr('subprocess.check_output',lambda *a,**kw:'synthetic-revision')
    def generate(*args,**kwargs):
        assert kwargs['checkpoints']['cached-stage']['status']=='completed'
        assert args[0][0].raw_text=='A test concept.'
        return SimpleNamespace(report=ReviewReport(title='Test',summary='Synthetic'),llm_status='partial'),SimpleNamespace(llm_calls=0)
    monkeypatch.setattr(generation_pipeline,'generate_hierarchical_report',generate)
    cli.engine_regression(tmp_path,'botany')
    assert ledgers==[tmp_path/'paid-ledger.sqlite3']
    assert json.loads((tmp_path/'botany-recallforge.json').read_text())=={'original':'immutable'}
    result=tmp_path/'regressions'/'botany-synthetic-re'/'result.json'
    assert result.exists()
