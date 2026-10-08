"""Typed L0 bindings used by the existing source egress authority.

Workspace and imported Source IDs occupy different identity spaces. Only an
exact committed confirmation may alias them. No document body is rewritten.
"""
from collections.abc import Mapping
import re
from types import SimpleNamespace
from backend.recognition import RecognitionConflict, WorkScope
from backend.recognition.external_turn_facts import source_store

ORIGINAL_TYPES = ('original_item', 'original_source')


def original_content_identity(reader, scope, identity, span, *, input_binding=None):
    """Identity of an admitted original, separate from its processing state.

    SQLite items have no incarnation. An out-of-band identical delete/reinsert
    is outside this identity's guarantee; normal admissions have unique IDs and
    creation times. Generated titles and processing leases are not originals.
    """
    row = original(reader, scope, 'original_item', identity)
    payload = row.payload
    text = payload.get('source_text')
    if not isinstance(text, str) or not isinstance(span, str) or not span:
        raise RecognitionConflict('original input span is unavailable')
    if input_binding is None:
        if span not in text:
            raise RecognitionConflict('original input span is unavailable')
        start = text.index(span)
        coordinates = {'coordinate_space':'workspace_source_text_v1', 'start':start, 'end':start + len(span)}
    else:
        child = reader.read('v2_turns', input_binding['child_id'])
        parent = reader.read('v2_turns', input_binding['parent_id'])
        if (child is None or parent is None or child.payload.get('project_id') != scope.project_id
                or parent.payload.get('project_id') != scope.project_id
                or child.payload.get('parent_turn_id') != parent.object_id
                or child.payload.get('user_text') != span
                or child.payload.get('item_id') != identity
                or parent.payload.get('user_text') != input_binding['parent_text']
                or parent.payload['user_text'][input_binding['start']:input_binding['end']] != span):
            raise RecognitionConflict('original input turn coordinates changed')
        coordinates = {'coordinate_space':'child_turn_user_text_v1', 'input_binding':input_binding, 'span':span}
    fields = ('id', 'project_id', 'input_kind', 'created_at', 'source_text',
              'original_path', 'original_name', 'filename', 'url')
    scene = reader.read('v2_scene_assignments_item', identity)
    return {'identity':{field:payload.get(field) for field in fields},
            'scene':dict(scene.payload) if scene else None,
            **coordinates}


def original(reader, scope, kind, identity):
    if kind == 'original_item':
        row = reader.read('workspace_items', identity)
        if row is None or row.payload.get('project_id') != scope.project_id:
            raise RecognitionConflict('original is unavailable in this work scope')
        return SimpleNamespace(revision=row.revision, payload={**row.payload, 'scope':{
            'user_id':scope.user_id, 'project_id':scope.project_id}, 'state':'active'})
    store = source_store(reader)
    with store.locked('sources',identity):
        body = store.read('sources',identity)
        if not body or body.get('id') != identity or body.get('project_id','default') != scope.project_id:
            raise RecognitionConflict('original is unavailable in this work scope')
        provenance = None
        if body.get('identity_method') == 'workspace_confirmation':
            binding = alias(reader,scope,identity,body)
            parent = original(reader,scope,*binding)
            provenance = {'source_refs':[{'type':binding[0],'id':binding[1],'revision':parent.revision}]}
        return SimpleNamespace(revision=store.revision('sources',identity), payload={**body,
            'scope':{'user_id':scope.user_id,'project_id':scope.project_id},'state':'active',
            '_original_incarnation':store.incarnation('sources',identity),
            'provenance':provenance})


def alias(reader, scope, identity, body):
    if body.get('identity_method') != 'workspace_confirmation':
        return 'original_source',identity
    operation_id = body.get('confirmation_operation_id')
    operation = reader.read('workspace_confirmation_operations',operation_id) if isinstance(operation_id,str) else None
    item_id = body.get('workspace_item_id')
    item = reader.read('workspace_items',item_id) if isinstance(item_id,str) else None
    if (operation is None or item is None or operation.payload.get('state') != 'committed'
            or operation.payload.get('source_id') != identity
            or operation.payload.get('source_payload') != dict(body)
            or operation.payload.get('workspace_item_id') != item_id
            or item.payload.get('source_id') != identity
            or item.payload.get('document_id') != operation.payload.get('document_id')
            or item.payload.get('project_id') != scope.project_id
            or item.payload.get('status') != 'confirmed'):
        raise RecognitionConflict('original confirmation binding is unavailable')
    return 'original_item',item_id


def resolve(reader, scope, identity, *, kind=None):
    if kind == 'workspace':
        original(reader,scope,'original_item',identity)
        return 'original_item',identity
    store = source_store(reader)
    with store.locked('sources',identity):
        body = store.read('sources',identity)
        if kind == 'source':
            original(reader,scope,'original_source',identity)
            return 'original_source',identity
        item = reader.read('workspace_items',identity)
        if item and body:
            binding = alias(reader,scope,identity,body)
            if binding != ('original_item',identity):
                raise RecognitionConflict('original identity is ambiguous')
            return binding
        if body:
            original(reader,scope,'original_source',identity)
            return alias(reader,scope,identity,body)
        original(reader,scope,'original_item',identity)
        return 'original_item',identity


def document_roots(reader, scope, refs, *, optional=False):
    if not isinstance(refs,(list,tuple)):
        raise RecognitionConflict('original references are invalid')
    result = {}
    workspace_ids = {ref.get('source_id') for ref in refs if isinstance(ref,Mapping)
                     and isinstance(ref.get('source_id'),str)
                     and ref.get('locator') == 'workspace://' + ref['source_id']}
    for ref in refs:
        if not isinstance(ref,Mapping) or not isinstance(ref.get('source_id'),str):
            raise RecognitionConflict('original references are invalid')
        identity, locator = ref['source_id'],ref.get('locator','')
        segment_of_workspace = (identity in workspace_ids and isinstance(locator,str)
                                and re.fullmatch(r'text:[0-9]+:[0-9]+',locator) is not None)
        kind = 'workspace' if locator == 'workspace://' + identity or segment_of_workspace else 'source'
        if isinstance(locator,str) and locator.startswith(('workspace://','source://')) and locator != ('workspace://' if kind=='workspace' else 'source://') + identity:
            raise RecognitionConflict('original reference identity is invalid')
        # Synthetic historical documents may have no surviving L0. If a policy
        # exists or a workspace locator is supplied, loss must fail closed.
        if optional and kind == 'source' and source_store(reader).read('sources',identity) is None:
            if reader.read('source_egress_original_source_policies',identity):
                raise RecognitionConflict('original is unavailable')
            continue
        bound = resolve(reader,scope,identity,kind=kind)
        result[bound] = original(reader,scope,*bound).revision
    return tuple((kind,identity,revision) for (kind,identity),revision in sorted(result.items()))


def all_originals(records):
    with records.begin() as reader:
        rows = [('original_item',row.object_id,row.payload) for row in reader.list('workspace_items')]
        for body in source_store(reader).list('sources'):
            identity=body.get('id')
            if not isinstance(identity,str):
                continue
            scope=WorkScope('local-user',body.get('project_id','default'))
            try:
                bound=resolve(reader,scope,identity,kind='source')
                rows.append((*bound,body))
            except RecognitionConflict:
                continue
    return rows


def resolve_turn_material(records, scope, item):
    """Resolve a material identity and its L0 closure through existing stores."""
    from copy import deepcopy
    from backend.memory_app.original_sources import original, document_roots

    if set(item) != {"type", "id", "revision", "project_id"}:
        raise RecognitionConflict("turn material must identify exactly one authoritative object")
    kind, identity = item["type"], item["id"]
    if item["project_id"] != scope.project_id or type(item["revision"]) is not int:
        raise RecognitionConflict("turn material identity is invalid")
    if kind in {"original_item", "original_source"}:
        row = original(records, scope, kind, identity)
        roots = [{"type": kind, "id": identity, "revision": row.revision}]
        ref_kind, collection = "source", "workspace" if kind == "original_item" else "sources"
    elif kind in {"document", "recognition", "experience"}:
        collection = {"document": "documents", "recognition": "recognitions", "experience": "recognition_experiences"}[kind]
        row = records.read(collection, identity)
        if row is None:
            raise RecognitionConflict("turn material is unavailable")
        project = row.payload.get("project_id") if kind == "document" else row.payload.get("scope", {}).get("project_id")
        if project != scope.project_id:
            raise RecognitionConflict("turn material is outside the requested scope")
        if kind == "document":
            from backend.recognition.document_filings import filing_experience, DocumentFilingError
            try:
                copied = filing_experience(records, scope, identity)
            except DocumentFilingError as error:
                raise RecognitionConflict(str(error)) from error
            roots = ([{'type': 'experience', 'id': copied.object_id, 'revision': copied.revision}]
                if copied is not None else [{"type": root_type, "id": root_id, "revision": revision}
                    for root_type, root_id, revision in document_roots(records, scope, row.payload.get("source_refs", []))])
            if not roots:
                raise RecognitionConflict("turn document has no original authority")
        else:
            roots = [{"type": kind, "id": identity, "revision": row.revision}]
        ref_kind = "document" if kind == "document" else "atom"
    else:
        raise RecognitionConflict("turn material type is invalid")
    if row.revision != item["revision"]:
        raise RecognitionConflict("turn material revision conflicted")
    return {**deepcopy(item), "payload": deepcopy(dict(row.payload)),
            "ref": {"kind": ref_kind, "object_id": identity, "uri": f"crp://default/{collection}/{identity}"}}, roots
