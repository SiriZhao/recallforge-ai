"""Offline measurement of private real runs; never sends a network request."""
import argparse
import json
import sqlite3
import hashlib
from pathlib import Path
from app.services.learning_evaluation import observable_metrics
from app.services.generation_pipeline import _coerce_stage_json
from app.services.stage_checkpoints import stable_hash
from app.services.learning_evaluation import source_files
from app.services.evidence_catalog import catalog_for, support_trace
from app.schemas.teaching_quality import UnitCritique


def audit_cached_claims(payload, manifest):
    """Replay only the current quotation gate. Never relabel a cached run READY.

    Counts are intentionally separate from factual truth and efficacy metrics.
    No private text/quotes are returned by this audit.
    """
    report=payload.get('result',payload)
    reviews=report.get('overview',{}).get('unit_quality',[])
    latest={}
    for review in reviews:
        unit=review.get('unit_id')
        if unit and (unit not in latest or review.get('attempt',0)>=latest[unit].get('attempt',0)):
            latest[unit]=review
    summary={'audit_version':'exact-source-quote-v1','cached_units':len(latest),
        'units_passing_quote_gate':0,'supported_claims':0,'locator_only_claims':0,
        'unresolved_or_invalid_claims':0,'claims_with_extraction_warnings':0,
        'factual_truth':'NOT_EVALUATED','full_pipeline_rerun':False}
    spans=catalog_for(source_files(manifest))
    for review in latest.values():
        try:critic=UnitCritique.model_validate(review)
        except (ValueError,TypeError):
            summary['unresolved_or_invalid_claims']+=1
            continue
        passes=True
        for claim in critic.claims:
            traces=[support_trace(ref,spans) for ref in claim.source_refs]
            if claim.basis=='SUPPORTED':summary['supported_claims']+=1
            if any(trace['extraction_warnings'] for trace in traces):
                summary['claims_with_extraction_warnings']+=1
            if claim.basis=='INSUFFICIENT' or (claim.basis!='GENERATED' and (not traces or any(t['status']=='UNRESOLVED' for t in traces))):
                summary['unresolved_or_invalid_claims']+=1;passes=False
            elif claim.basis=='SUPPORTED' and any(t['status']=='LOCATOR_ONLY' for t in traces):
                summary['locator_only_claims']+=1;passes=False
        summary['units_passing_quote_gate']+=int(passes)
    summary['status']='AUDITED' if latest else 'NO_CACHED_CRITIC'
    return summary


def evaluate(private_dir,subject):
    manifest=json.loads((private_dir/f'{subject}-manifest.json').read_text(encoding='utf-8'))
    baseline_response=json.loads((private_dir/f'{subject}-chat.json').read_text(encoding='utf-8'))
    baseline=_coerce_stage_json(baseline_response['choices'][0]['message']['content'])
    revised=private_dir/f'{subject}-chat-materialized.json'
    if revised.exists():
        materialized=json.loads(revised.read_text(encoding='utf-8'))
        if materialized.get('source_hash')==manifest['source_hash']:baseline=materialized['output']
    engine=json.loads((private_dir/f'{subject}-recallforge.json').read_text(encoding='utf-8'))
    report={'subject':subject,'source_hash':manifest['source_hash'],'model':manifest['model'],
        'baseline':observable_metrics(baseline,manifest),'recallforge':observable_metrics(engine,manifest,is_report=True),
        'comparison_status':'EXPLORATORY_NOT_EFFICACY_PROOF','human_blind_ratings':'NOT_PERFORMED','superiority_claim':False,'arms':{}}
    report['cached_engine_quote_audit']=audit_cached_claims(engine,manifest)
    with sqlite3.connect(private_dir/'paid-ledger.sqlite3') as db:
        rows=db.execute('SELECT arm,status,reserve,charge,request_json,response_json,latency FROM evaluation_calls').fetchall()
    report['cumulative_upper_usd']=sum(row[3] if row[3] is not None else row[2] for row in rows)
    report['uncertain_requests']=sum(row[1]!='completed' for row in rows)
    scope=subject+'-'+manifest['source_hash'][:12]
    for arm,status,reserve,charge,request,response,latency in rows:
        if not arm.startswith(scope):continue
        item=report['arms'].setdefault(arm,{'calls':0,'input_tokens':0,'output_tokens':0,'upper_usd':0,'latency_seconds':0,'truncated_responses':0,'returned_models':[]})
        item['calls']+=1;item['upper_usd']+=charge if charge is not None else reserve;item['latency_seconds']+=latency or 0
        if response:
            data=json.loads(response);usage=data.get('usage',{})
            item['input_tokens']+=usage.get('prompt_tokens',0);item['output_tokens']+=usage.get('completion_tokens',0)
            item['truncated_responses']+=int(data['choices'][0].get('finish_reason')=='length')
            item['returned_models']=sorted(set(item['returned_models']+[data.get('model','UNKNOWN')]))
    # Check the engine's persisted source identity rather than assume that an old
    # latest-result file belongs to a newer sample manifest.
    expected_files={item['source_ref']['filename'] for item in manifest['source_data']}
    actual_files={item['filename'] for item in engine['result']['overview'].get('materials',[])}
    report['source_filename_match']=expected_files==actual_files
    expected_hashes={stable_hash(file.model_dump(mode='json')) for file in source_files(manifest)}
    actual_hashes={file.get('content_hash') for file in engine['result']['overview'].get('materials',[])}
    report['source_content_match']=expected_hashes==actual_hashes
    if not report['source_content_match']:
        report['comparison_status']='SOURCE_MISMATCH_DO_NOT_COMPARE'
    report['immutable_model_revision_verified']=False
    report['fairness_note']='Shared model alias and equal per-arm caps; actual token/call usage differs. Earlier pilot retries and schema changes are included. No budget-normalized advantage is claimed.'
    destination=private_dir/f'{subject}-quality-report.json'
    destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    # Blinded artifacts are local-only; mapping is deliberately separate.
    swap=int(hashlib.sha256(manifest['source_hash'].encode()).hexdigest(),16)%2
    texts=[baseline.get('canonical_markdown',''),engine['result'].get('markdown','')]
    mapping={}
    for label,index in zip(['A','B'],[swap,1-swap]):
        (private_dir/f'{subject}-blind-{label}.md').write_text(texts[index],encoding='utf-8')
        mapping[label]='chat' if index==0 else 'recallforge'
    (private_dir/f'{subject}-blind-mapping.json').write_text(json.dumps(mapping),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--private-dir',type=Path,default=Path('runtime/evaluation'))
    parser.add_argument('--subject',required=True,choices=['probability','python','botany','organic'])
    args=parser.parse_args()
    evaluate(args.private_dir,args.subject)
