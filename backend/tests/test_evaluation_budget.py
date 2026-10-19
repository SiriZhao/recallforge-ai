import httpx
import pytest
from app.services.evaluation_budget import EvaluationClient, EvaluationBudgetError


def test_cached_response_does_not_spend_twice(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":100,"completion_tokens":20},"model":"fixed-version"})
    client=EvaluationClient(tmp_path/'ledger.db',key='fake-only',transport=httpx.Client(transport=httpx.MockTransport(handler)))
    for _ in range(2):
        assert client.chat('a',[{'role':'user','content':'test'}],100)['model']=='fixed-version'
    assert len(calls)==1 and client.summary()['requests']==1
    assert client.summary()['accounted_upper_usd'] < 0.001


def test_unknown_usage_stops_further_paid_calls(tmp_path):
    calls=[]
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={"choices":[{"message":{"content":"ok"}}]})
    client=EvaluationClient(tmp_path/'ledger.db',key='fake-only',transport=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(EvaluationBudgetError,match='Missing usage'):
        client.chat('a',[{'role':'user','content':'test'}],100)
    with pytest.raises(EvaluationBudgetError,match='Uncertain prior billing'):
        client.chat('b',[{'role':'user','content':'test2'}],100)
    assert len(calls)==1 and client.summary()['uncertain_requests']==1


def test_reservation_prevents_overspend_before_network(tmp_path):
    client=EvaluationClient(tmp_path/'ledger.db',key='fake-only',limit_usd=0.01)
    with pytest.raises(EvaluationBudgetError,match='limit'):
        client.chat('a',[{'role':'user','content':'test'}],10000)
    assert client.summary()['requests']==0


def test_stage_provider_never_exceeds_requested_output_contract():
    from app.services.evaluation_budget import BudgetedStageProvider
    class Client:
        model='fake'
        def chat(self,arm,messages,max_tokens,**kwargs):
            assert max_tokens==1000 and kwargs['fit_output_budget']
            return {'choices':[{'finish_reason':'stop','message':{'content':'{}'}}]}
    assert BudgetedStageProvider(Client(),'fake-arm').generate_stage('course_model','{}',None,max_output_tokens=1000)=='{}'


def test_remaining_output_fits_without_resetting_global_or_arm_budget(tmp_path):
    import json
    requested=[]
    def handler(request):
        body=json.loads(request.content);requested.append(body['max_tokens'])
        return httpx.Response(200,json={'choices':[{'message':{'content':'{}'}}],
            'usage':{'prompt_tokens':10,'completion_tokens':body['max_tokens']}})
    client=EvaluationClient(tmp_path/'ledger.db',key='fake',transport=httpx.Client(transport=httpx.MockTransport(handler)))
    client.chat('same-arm',[{'role':'user','content':'first'}],29000)
    messages=[{'role':'user','content':'last'}]
    with pytest.raises(EvaluationBudgetError,match='Common per-arm'):
        client.chat('same-arm',messages,8192)
    client.chat('same-arm',messages,8192,fit_output_budget=True)
    assert requested==[29000,1000]
    with pytest.raises(EvaluationBudgetError,match='Insufficient remaining'):
        client.chat('same-arm',[{'role':'user','content':'new'}],8192,fit_output_budget=True)
    # The original exact request remains cached even with no arm tokens left.
    client.chat('same-arm',[{'role':'user','content':'first'}],29000)
    assert len(requested)==2 and client.summary()['requests']==2
