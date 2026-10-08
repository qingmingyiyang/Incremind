"""Bind expert text to durable completed research and its body-free read proofs."""
from collections.abc import Mapping
from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.kernel_read_facts import authority_facts
from .research_reads import read_dependencies, validate_reads
from .original_sources import source_store
from .source_snapshot import _closure_identity


def authority_stores(records):
    path=source_store(records).root / 'ai-turns.sqlite3'
    return authority_facts(path)


def terminal(turns,identity,project):
    request=turns.get_request(identity)
    if (request is None or request.get('scope',{}).get('project_id')!=project
            or not str(request.get('operation_id','')).startswith('op-research-')):
        raise RecognitionConflict('research terminal scope is invalid')
    completed=[event for event in turns.events_after(identity,after_sequence=0)
               if event.get('type') in ('turn.completed','turn.failed','turn.cancelled')]
    if not completed or completed[-1]['type']!='turn.completed':
        raise RecognitionConflict('research has not completed')
    event=completed[-1]
    summary=event.get('data',{}).get('summary')
    if not isinstance(summary,str):
        raise RecognitionConflict('research summary is unavailable')
    return event['sequence'],summary[:6000]


def capture_research_packet(records,scope,identity,brief,*,authority):
    turns,agents=authority_stores(records)
    sequence,current=terminal(turns,identity,scope.project_id)
    if brief!=current:
        raise RecognitionConflict('research brief is not bound to the completed turn')
    proofs=read_dependencies(records,turns,agents,identity,scope.project_id)
    validate_reads(records,scope,proofs,authority=authority)
    return {'turn_id':identity,'terminal_sequence':sequence,'reads':proofs}


def validate_research_packet(records,scope,packet,*,authority,remote=False,inherit=False):
    prefix='以下是专家团队的研究结论，仅供参考；与资料冲突时以资料为准：\n'
    found=[message['content'][len(prefix):] for message in packet.get('messages',[])
           if isinstance(message,Mapping) and isinstance(message.get('content'),str)
           and message['content'].startswith(prefix)]
    if len(found)>1:
        raise RecognitionConflict('research packet contains multiple briefs')
    brief=found[0] if found else packet.get('expert_brief','')
    bound=packet.get('research_sources')
    if not brief:
        if bound is not None:
            raise RecognitionConflict('empty research brief has a source binding')
        return ()
    if not isinstance(bound,Mapping):
        if remote or inherit:
            raise RecognitionConflict('research brief source binding is unavailable')
        return ()
    turns,agents=authority_stores(records)
    sequence,current=terminal(turns,bound.get('turn_id'),scope.project_id)
    if sequence!=bound.get('terminal_sequence') or current.strip()!=brief.strip():
        raise RecognitionConflict('research terminal binding changed')
    proofs=read_dependencies(records,turns,agents,bound['turn_id'],scope.project_id)
    if proofs!=bound.get('reads'):
        raise RecognitionConflict('research read binding changed')
    for proof in proofs:
        for document in proof.get('documents',[]):
            row=records.read('documents',document['id'])
            if row is None or row.revision!=document['revision']:
                raise RecognitionConflict('research document evidence changed')
    research_style_sources(records,packet,authority=authority)
    if not inherit:
        validate_reads(records,scope,proofs,authority=authority,remote=remote)
    roots={}
    for proof in proofs:
        snapshot=proof.get('source_egress')
        if snapshot is None: continue
        if inherit:
            fresh=authority.snapshot(scope,snapshot['roots'])
            if {_closure_identity(n) for n in fresh['nodes']}!={_closure_identity(n) for n in snapshot['nodes']}:
                raise RecognitionConflict('research original material changed')
        for root in snapshot['roots']:
            roots[(root['type'],root['id'])]=root['revision']
    return tuple((kind,identity,revision) for (kind,identity),revision in sorted(roots.items()))


def research_style_sources(records, packet,*,authority):
    bound=packet.get('research_sources')
    if not isinstance(bound,Mapping): return []
    result=[]
    for proof in bound.get('reads',[]):
        for frozen in proof.get('style_sources',[]):
            own=WorkScope(frozen['scope']['user_id'],frozen['scope']['project_id'])
            current=authority.snapshot(own,frozen['roots'])
            if {_closure_identity(n) for n in current['nodes']}!={_closure_identity(n) for n in frozen['nodes']}:
                raise RecognitionConflict('research style original changed')
            result.append(current)
    return result
