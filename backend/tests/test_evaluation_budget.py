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
