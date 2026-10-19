from tests.eval_learning_quality import audit_cached_claims


def test_cached_quote_audit_never_invents_truth_or_uses_superseded_critic():
    ref={'filename':'synthetic.pdf','page':1,'anchor':'synthetic.pdf, p. 1'}
    manifest={'source_data':[{'source_ref':ref,'text':'A variable is case-sensitive.'}]}
    claim={'claim':'Variable case matters','basis':'SUPPORTED','source_refs':[ref]}
    bad={'unit_id':'one','attempt':0,'verdict':'READY','claims':[claim]}
    payload={'result':{'overview':{'unit_quality':[bad]}}}
    audit=audit_cached_claims(payload,manifest)
    assert audit['locator_only_claims']==1 and audit['units_passing_quote_gate']==0
    good={**bad,'attempt':1,'claims':[{**claim,'source_refs':[{**ref,'fragment':'case-sensitive'}]}]}
    payload['result']['overview']['unit_quality'].append(good)
    audit=audit_cached_claims(payload,manifest)
    assert audit['cached_units']==1 and audit['units_passing_quote_gate']==1
    assert audit['factual_truth']=='NOT_EVALUATED' and audit['full_pipeline_rerun'] is False
    assert payload['result']['overview']['unit_quality'][0]['claims'][0]['source_refs'][0].get('fragment') is None


def test_nonmatching_quote_remains_invalid_and_empty_critic_is_not_success():
    ref={'filename':'synthetic.pdf','page':1,'anchor':'synthetic.pdf, p. 1'}
    manifest={'source_data':[{'source_ref':ref,'text':'print("a  b")'}]}
    claim={'claim':'Output','basis':'SUPPORTED','source_refs':[{**ref,'fragment':'print("a b")'}]}
    payload={'overview':{'unit_quality':[{'unit_id':'one','verdict':'READY','claims':[claim]}]}}
    audit=audit_cached_claims(payload,manifest)
    assert audit['unresolved_or_invalid_claims']==1 and audit['units_passing_quote_gate']==0
    assert audit_cached_claims({},manifest)['status']=='NO_CACHED_CRITIC'
