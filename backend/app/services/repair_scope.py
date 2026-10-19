"""Repair continuity invariants, separate from fallible semantic review."""
from difflib import SequenceMatcher
from app.services.stage_checkpoints import stable_hash
from app.services.evidence_catalog import resolves


def _mentioned(text,concept,domain):
    # This is an anti-deletion check, never an educational coverage score.
    sensitive=any(value in domain.casefold() for value in ('program','python','chem','math'))
    transform=(lambda value:value) if sensitive else str.casefold
    return any(transform(term) in transform(text) for term in [concept.name,*concept.aliases] if term.strip())


def localized_ranges(critique,text):
    findings=[finding for finding in critique.findings if finding.severity!='info'] if critique else []
    if not findings or critique.issues or critique.missing_knowledge_ids:return []
    if any(claim.basis=='INSUFFICIENT' for claim in critique.claims):return []
    if any(f.severity!='minor' or f.category!='wording' or f.text_start is None or f.text_end is None
           or not 0<=f.text_start<f.text_end<=len(text) or not f.quote or text[f.text_start:f.text_end]!=f.quote for f in findings):return []
    return [(f.text_start,f.text_end) for f in findings]


def apply_patches(original,raw,ranges):
    if not isinstance(raw,dict) or set(raw)!={'patches'} or not isinstance(raw['patches'],list) or not raw['patches']:
        raise ValueError('Localized repair requires only a nonempty patches collection')
    patches=[]
    for patch in raw['patches']:
        if not isinstance(patch,dict) or set(patch)!={'start','end','replacement'}:
            raise ValueError('Invalid patch contract')
        start,end,replacement=patch['start'],patch['end'],patch['replacement']
        if type(start)!=int or type(end)!=int or not isinstance(replacement,str) or not any(a<=start<end<=b for a,b in ranges):
            raise ValueError('Patch exceeds the critic-identified wording span')
        if len(replacement)>max(80,4*(end-start)) or replacement.count('\n')>original.markdown[start:end].count('\n'):
            raise ValueError('Minor wording patch attempts to expand into a broad rewrite')
        patches.append((start,end,replacement))
    patches.sort()
    if any(left[1]>right[0] for left,right in zip(patches,patches[1:])):raise ValueError('Overlapping repair patches')
    text=original.markdown
    for start,end,replacement in reversed(patches):text=text[:start]+replacement+text[end:]
    return original.model_copy(deep=True,update={'markdown':text,'status':'READY'})


def validate_unit_scope(original,candidate,course,plan,critique,spans):
    if candidate.unit_id!=original.unit_id:raise ValueError('Repair changed stable unit identity')
    required=set(plan.get('knowledge_ids',[]))
    protected=[concept for concept in course.concepts if concept.id in required and _mentioned(original.markdown,concept,course.subject_domain)]
    lost=[concept.id for concept in protected if not _mentioned(candidate.markdown,concept,course.subject_domain)]
    if lost:raise ValueError('Repair silently removed covered knowledge IDs: '+','.join(lost))
    if any(not resolves(ref,spans) for ref in candidate.source_refs):raise ValueError('Repair introduced unresolved unit source refs')
    disputed=[ref for f in (critique.findings if critique else []) if f.category=='citation' for ref in f.source_refs]
    def same_location(left,right):
        old=[span.source for span in spans if resolves(left,[span])]
        new=[span.source for span in spans if resolves(right,[span])]
        return any((a.document_id,a.page,a.slide)==(b.document_id,b.page,b.slide)
            and (left.block_id is None or a.block_id==b.block_id) for a in old for b in new)
    protected_refs=[ref for ref in original.source_refs if resolves(ref,spans) and not any(same_location(ref,bad) for bad in disputed)]
    for ref in protected_refs:
        if not any(same_location(ref,new) and (not ref.fragment or ref.fragment==new.fragment) for new in candidate.source_refs):
            raise ValueError('Repair dropped an undisputed resolvable citation')
    return {'unit_id':original.unit_id,'before_hash':stable_hash(original.model_dump(mode='json')),
        'after_hash':stable_hash(candidate.model_dump(mode='json')),'protected_knowledge_ids':[c.id for c in protected],
        'protected_source_count':len(protected_refs),'changed_text_regions':sum(tag!='equal' for tag,*_ in SequenceMatcher(None,original.markdown.splitlines(),candidate.markdown.splitlines()).get_opcodes()),'diff_granularity':'lines',
        'measurement':'Continuity and locator checks; factual consistency is separately re-reviewed, not proven by a text diff.'}
