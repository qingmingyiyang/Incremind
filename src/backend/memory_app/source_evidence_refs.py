"""Existing evidence-reference parsing shared by read and retained-source owners."""
from collections.abc import Mapping
from urllib.parse import urlsplit, unquote
from backend.recognition import RecognitionConflict
from .original_sources import resolve, original, document_roots

READS = frozenset(('memory.recall','source.evidence.read','workbench.question.answer',
                  'memory.candidate.evidence.read','project_skill.evidence.read'))


def _source_refs(value):
    if not isinstance(value,list):
        raise RecognitionConflict('research evidence references are unavailable')
    result=[]
    for ref in value:
        if isinstance(ref,str):
            result.append(ref)
        elif isinstance(ref,Mapping) and isinstance(ref.get('source_id'),str):
            locator=ref.get('locator')
            kind='document' if locator=='document:summary' else 'source'
            if isinstance(locator,str) and locator.startswith('document://'):
                if locator!='document://' + ref['source_id']:
                    raise RecognitionConflict('research document reference identity is invalid')
                kind='document'
            result.append(kind + '://' + ref['source_id'])
        else:
            raise RecognitionConflict('research evidence reference is invalid')
    return result


def evidence_refs(capability, output):
    result=output.get('result')
    if capability == 'source.evidence.read':
        if not isinstance(result,Mapping) or not isinstance(result.get('source_id'),str):
            raise RecognitionConflict('research source evidence is invalid')
        refs=['source://' + result['source_id']]
        baseline=result.get('document_baseline')
        if baseline:
            if not isinstance(baseline,Mapping) or not isinstance(baseline.get('document_id'),str):
                raise RecognitionConflict('research document evidence is invalid')
            refs.append('document://' + baseline['document_id'])
        # The source's own material references must all resolve as well.
        refs.extend(_source_refs(result.get('source_refs',[])))
        return refs
    if capability == 'memory.recall':
        if not isinstance(result,list):
            raise RecognitionConflict('research recall evidence is invalid')
        refs=[]
        for item in result:
            if not isinstance(item,Mapping):
                raise RecognitionConflict('research recall evidence is invalid')
            sources=_source_refs(item.get('source_refs',[]))
            if not sources:
                raise RecognitionConflict('research recalled material has no source authority')
            refs.extend(sources)
        return refs
    if capability == 'memory.candidate.evidence.read':
        if not isinstance(result,Mapping):
            raise RecognitionConflict('research candidate evidence is invalid')
        refs=_source_refs(result.get('refs',[]))
        if not refs:
            raise RecognitionConflict('research candidate evidence has no source authority')
        return refs
    if capability == 'workbench.question.answer':
        content=result.get('content') if isinstance(result,Mapping) else None
        items=content.get('evidence_items') if isinstance(content,Mapping) else None
        if not isinstance(items,list):
            raise RecognitionConflict('research workbench evidence is invalid')
        refs=[]
        for item in items:
            if not isinstance(item,Mapping):
                raise RecognitionConflict('research workbench evidence is invalid')
            selected=[item[key] for key in ('target_ref','ref') if item.get(key)]
            selected.extend(_source_refs(item.get('source_refs',[])))
            if not selected:
                raise RecognitionConflict('research workbench material has no source authority')
            refs.extend(selected)
        return refs
    if capability == 'project_skill.evidence.read':
        model=result.get('model_input') if isinstance(result,Mapping) else None
        evidence=model.get('untrusted_project_evidence') if isinstance(model,Mapping) else None
        items=evidence.get('items') if isinstance(evidence,Mapping) else None
        if not isinstance(items,list):
            raise RecognitionConflict('research project evidence is invalid')
        refs=[]
        for item in items:
            if not isinstance(item,Mapping):
                raise RecognitionConflict('research project evidence is invalid')
            kind,identity=item.get('kind'),item.get('object_id')
            if kind in ('source','document') and isinstance(identity,str):
                refs.append(kind + '://' + identity)
            elif kind in ('published_memory','current_project_skill'):
                sources=_source_refs(item.get('source_refs',[]))
                if kind == 'published_memory' and not sources:
                    raise RecognitionConflict('research memory material has no source authority')
                refs.extend(sources)
            else:
                raise RecognitionConflict('research project evidence type is invalid')
        current=model.get('current_project_skill')
        if isinstance(current,Mapping):
            for rule in current.get('output_rules',[]):
                if not isinstance(rule,Mapping): raise RecognitionConflict('research rule is invalid')
                refs.extend(_source_refs(rule.get('source_refs',[])))
        for item in items:
            for rule in item.get('output_rules',[]):
                if not isinstance(rule,Mapping): raise RecognitionConflict('research rule is invalid')
                refs.extend(_source_refs(rule.get('source_refs',[])))
        return refs
    raise RecognitionConflict('research evidence capability is invalid')


def roots_for(reader, scope, refs):
    roots={}
    for ref in refs:
        if not isinstance(ref,str):
            raise RecognitionConflict('research evidence reference is invalid')
        url=urlsplit(ref)
        if url.scheme in ('source','document','workspace'):
            kind,identity=url.scheme,url.netloc
        elif url.scheme == 'crp' and url.netloc == 'default':
            parts=[unquote(part) for part in url.path.split('/') if part]
            if len(parts)!=2 or parts[0] not in ('sources','documents','recognitions','recognition_experiences'):
                raise RecognitionConflict('research evidence reference is unresolved')
            kind={'sources':'source','documents':'document','recognitions':'recognition',
                  'recognition_experiences':'experience'}[parts[0]]
            identity=parts[1].removesuffix('.json') if kind=='document' else parts[1]
        else:
            raise RecognitionConflict('research evidence reference is unresolved')
        if kind=='document':
            doc=reader.read('documents',identity)
            if doc is None or doc.payload.get('project_id')!=scope.project_id:
                raise RecognitionConflict('research document is unavailable')
            bound=document_roots(reader,scope,doc.payload.get('source_refs',[]))
            if not bound:
                raise RecognitionConflict('research document has no original authority')
        elif kind in ('recognition','experience'):
            collection='recognitions' if kind=='recognition' else 'recognition_experiences'
            row=reader.read(collection,identity)
            if row is None:
                raise RecognitionConflict('research source is unavailable')
            bound=((kind,identity,row.revision),)
        else:
            typed=resolve(reader,scope,identity,kind=kind)
            bound=((*typed,original(reader,scope,*typed).revision),)
        for typed,identity,revision in bound:
            roots[(typed,identity)]=revision
    return [{'type':kind,'id':identity,'revision':revision} for (kind,identity),revision in sorted(roots.items())]

