"""Qualify frozen correction facts through the current source authority."""
import json

from backend.recognition import RecognitionConflict, RecognitionError, WorkScope
from backend.recognition.product_draft_dependencies import ProductDraftDependencyError, product_draft_source
from core.ai_kernel import validate_turn_request
from core.document_engine import SQLiteDocumentRepository
from ..source_egress import SourceEgressService, _frozen_packet_authority
from ..source_snapshot import _closure_identity
from ..source_graph import SourceGraph, validate_graph
from ..research_packets import authority_stores
from ..research_reads import product_read_sources
from ..transaction_records import TransactionRecords
from ..workspace_contracts import _now
from .policies import get
from .privacy import is_private_project, privacy_revision
from .turn_requests import freeze_product_turn
from .insights import source_documents
from .learning_events import _outcome_ready
from .outcome_corrections import COLLECTION as OUTCOMES, _changed, _time
from .task_divisions import validate_items


def corrections(records, project):
    consumed = {identity for row in records.list('v2_consolidation_inputs')
                if row.payload.get('project_id') == project for identity in row.payload.get('event_ids', [])}
    result = []
    for row in records.list('v2_correction_events'):
        payload = row.payload
        if payload.get('project_id') != project or row.object_id in consumed:
            continue
        collection = 'recognitions' if payload.get('object_kind') == 'recognition' else 'recognition_candidates'
        owner = records.read(collection, payload.get('object_id', ''))
        if (owner is None or owner.payload.get('scope') != {'user_id': 'local-user', 'project_id': project}
                or owner.revision < payload.get('object_revision', 0)
                or (collection == 'recognitions' and owner.payload.get('state') != 'active')
                or is_private_project(records, project)):
            continue
        pref = records.read('recognition_recall_preferences', owner.object_id)
        if pref and pref.payload.get('state') == 'forgotten' and pref.payload.get('by', 'user') == 'user':
            continue
        refs = list(payload.get('source_refs', []))
        if not refs or any(ref.get('project_id') != project for ref in refs):
            continue
        if collection == 'recognitions':
            refs.append({'type': 'recognition', 'id': owner.object_id, 'revision': owner.revision, 'project_id': project})
        try:
            snapshot = SourceEgressService(records).snapshot(WorkScope('local-user', project),
                [{key: ref[key] for key in ('type', 'id', 'revision')} for ref in refs])
            SourceEgressService(records).require(snapshot, 'generation')
            docs = source_documents(records, WorkScope('local-user', project),
                [ref['id'] for ref in refs if ref['type'] == 'experience'],
                [ref['id'] for ref in refs if ref['type'] == 'recognition'])
            forgotten = False
            for identity in docs:
                pref = records.read('v2_document_recall', identity)
                doc = records.read('documents', identity)
                forgotten |= (not doc or doc.payload.get('status') == 'archived' or
                    bool(pref and pref.payload.get('state') == 'forgotten' and pref.payload.get('by', 'user') == 'user'))
            if forgotten or not any(node['type'] in {'original_item', 'original_source'} for node in snapshot['nodes']):
                continue
        except RecognitionError:
            continue
        result.append({'event_id': row.object_id, 'type': payload['type'], 'before': payload['before'],
                       'after': payload['after'], **({'turn_id': payload['turn_id']} if payload.get('turn_id') else {}),
                       '_row': row, '_owner': owner, '_collection': collection, '_snapshot': snapshot,
                       '_refs': refs, '_documents': sorted(docs)})
    return get('consolidate')(None, operation='events', events=result)


def validate(records, events):
    for event in events:
        if (records.read('v2_correction_events', event['event_id']) != event['_row'] or
                records.read(event['_collection'], event['_owner'].object_id) != event['_owner']):
            raise RecognitionConflict('consolidation_correction_changed')
        project = event['_row'].payload['project_id']
        SourceEgressService(records).validate_snapshot(WorkScope('local-user', project), event['_snapshot'])
        SourceEgressService(records).require(event['_snapshot'], 'generation')
        # Re-check scope, privacy, source owners and manual forgetting at dispatch.
        current = {row['event_id']: row for row in corrections(records, project)}
        if event['event_id'] not in current:
            raise RecognitionConflict('consolidation_correction_unavailable')


def freeze_adapter(events, *, verified_events=(), review_events=(), project=None, now=None):
    def freeze(kind, **kwargs):
        rendered = set()
        def load(item):
            own = [event for event in events if event['event_id'] not in rendered and any(ref['type'] == item['type'] and ref['id'] == item['id']
                    and ref['revision'] == item['revision'] for ref in event['_refs'])]
            rendered.update(event['event_id'] for event in own)
            payload = {'material': {'type': item['type'], 'id': item['id'], 'revision': item['revision'],
                        'text': item['payload'].get('content', '')},
                       'corrections': [{key: event[key] for key in ('event_id', 'type', 'before', 'after')}
                                       for event in own]}
            return json.dumps(payload, ensure_ascii=False)
        options = {**kwargs, 'load_text': load}
        if verified_events or review_events:
            def feedback():
                parts = []
                if verified_events:
                    validate_consumer_outcomes(kwargs['records'], verified_events, project=project, now=now)
                    original = [{key: value for key, value in event.items()
                        if key not in {'_roots', '_root_owners'}} for event in verified_events]
                    parts.append(verified_feedback(kwargs['records'], original, project=project, now=now))
                if review_events:
                    from .signal_review_feedback import review_feedback
                    parts.append(review_feedback(kwargs['records'], review_events, project=project, now=now))
                return '\n'.join(parts)
            options['load_verified_feedback'] = feedback
        return freeze_product_turn(kind, **options)
    return freeze


def original_count(records, snapshot):
    from ..source_graph import SourceGraph
    from ..original_sources import original
    graph = SourceGraph()
    graph.snapshot(snapshot)
    identities = set()
    for node in graph.result()['nodes']:
        if node.get('type') not in {'original_item', 'original_source'}:
            continue
        scope = WorkScope(**node['scope'])
        kind, identity = node['type'], node['id']
        if kind == 'original_source':
            row = original(records, scope, kind, identity)
            parents = row.payload.get('provenance', {}).get('source_refs', []) if row.payload.get('provenance') else []
            if parents:
                # The existing original owner validates a confirmation alias.
                kind, identity = parents[0]['type'], parents[0]['id']
        identities.add((scope.project_id, kind, identity))
    return len(identities)


def selected_source_count(records, project, events):
    """Count only the selected events' qualified original-source closure."""
    refs = {(ref['type'], ref['id'], ref['revision']):
            {key: ref[key] for key in ('type', 'id', 'revision')}
            for event in events for ref in event['_refs']}
    if not refs:
        return 0
    snapshot = SourceEgressService(records).snapshot(WorkScope('local-user', project), list(refs.values()))
    return original_count(records, snapshot)


def _remember(owners, collection, row):
    if row is None:
        raise RecognitionConflict('consolidation_outcome_owner_unavailable')
    owners[(collection, row.object_id)] = row
    return row


def _outcome_document(records, owners, project, identity):
    row = _remember(owners, 'documents', records.read('documents', identity))
    pref = _remember_optional(records, owners, 'v2_document_recall', identity)
    if (row.payload.get('project_id') != project or row.payload.get('status') == 'archived'
            or pref and pref.payload.get('state') == 'forgotten' and pref.payload.get('by', 'user') == 'user'):
        raise RecognitionConflict('consolidation_outcome_document_unavailable')
    return row


def _remember_optional(records, owners, collection, identity):
    row = records.read(collection, identity)
    owners[(collection, identity)] = row
    return row


def _outcome_history(records, owners, project, identity, revision):
    current = _outcome_document(records, owners, project, identity)
    if type(revision) is not int or not 1 <= revision <= current.revision:
        raise RecognitionConflict('consolidation_outcome_history_unavailable')
    key = f'{identity}~r{revision}'
    _remember(owners, 'document_revisions', records.read('document_revisions', key))
    _remember(owners, 'document_markdown', records.read('document_markdown', key))
    documents = SQLiteDocumentRepository(records)
    historical, markdown = documents.revision(identity, revision), documents.markdown(identity, revision=revision)
    if (historical is None or historical.get('document_id') != identity
            or historical.get('revision') != revision or not isinstance(markdown, str)):
        raise RecognitionConflict('consolidation_outcome_history_unavailable')
    return historical, markdown


def _outcome_root(records, owners, project, identity, birth, turn):
    scope = WorkScope('local-user', project)
    bound = product_draft_source(records, scope, identity, birth)
    if bound is None or bound.revisions['task_execution_id'] != turn:
        raise RecognitionConflict('consolidation_outcome_root_unavailable')
    for collection, key in (
            ('v2_turns', turn), ('v2_task_executions', turn),
            ('v2_task_draft_operations', bound.revisions['task_draft_operation_id'])):
        _remember(owners, collection, records.read(collection, key))
    _outcome_history(records, owners, project, identity, birth)
    return bound.request


def _outcome_title(records, owners, identity, revision, birth, captured):
    # Historical Document rows retain Markdown, not titles. The original redo
    # writer's atomic event is the retained title fact after later user edits.
    current = owners[('documents', identity)]
    if current.revision == revision and current.payload.get('title') != captured:
        raise RecognitionConflict('consolidation_outcome_title_changed')
    if revision == birth:
        bound = product_draft_source(records, WorkScope('local-user', current.payload['project_id']), identity, birth)
        operation = owners[('v2_task_draft_operations', bound.revisions['task_draft_operation_id'])]
        if operation.payload['inputs']['title'] != captured:
            raise RecognitionConflict('consolidation_outcome_title_changed')


def _division_request(records, owners, project, payload):
    identity = payload['turn_id']
    row = _remember(owners, 'v2_task_divisions', records.read('v2_task_divisions', identity))
    turn = _remember(owners, 'v2_turns', records.read('v2_turns', identity))
    execution = _remember(owners, 'v2_task_executions', records.read('v2_task_executions', identity))
    sample, public, state = row.payload, turn.payload, execution.payload
    request = validate_turn_request(state['request'])
    receipt = public['receipt']['do']
    task = json.loads(request['input']['text'])
    if (not isinstance(receipt, dict) or not isinstance(task, dict)
            or sample.get('deleted') or sample.get('project_id') != project
            or sample.get('source_turn_id') != identity or sample.get('adjusted') is not True
            or sample.get('outcome') not in {'done', 'partial', 'failed'}
            or sample.get('outcome') != receipt.get('state')
            or public.get('project_id') != project or public.get('intent') != 'do'
            or state.get('project_id') != project or state.get('started') is not True
            or request['turn_id'] != receipt.get('kernel_turn_id')
            or request['scope'] != {'kind': 'project', 'project_id': project, 'series_id': None}
            or request['desired_outcome'] != 'project.task'
            or not isinstance(public.get('user_text'), str)
            or public['user_text'] != state.get('task_text') or public['user_text'] != sample.get('task_text')
            or task.get('task') != public['user_text']):
        raise RecognitionConflict('consolidation_outcome_division_unavailable')
    validate_items(sample['items'])
    # The adjustment owner has no parallel history table: its atomic facts form
    # the revision chain, anchored in the original public completion's goals.
    goals = [item['goal'] for item in receipt.get('division', [])] or [public['user_text']]
    revision = 1
    while revision < row.revision:
        matches = [event for event in records.list(OUTCOMES)
            if event.payload.get('kind') == 'division_adjust' and event.payload.get('project_id') == project
            and event.payload.get('turn_id') == identity and event.payload.get('division_from_revision') == revision]
        if len(matches) != 1:
            raise RecognitionConflict('consolidation_outcome_division_history_unavailable')
        fact = _remember(owners, OUTCOMES, matches[0]).payload
        if (not _outcome_ready(fact, None) or fact['division_to_revision'] != revision + 1
                or fact['before_goals'] != goals):
            raise RecognitionConflict('consolidation_outcome_division_history_unavailable')
        revision, goals = fact['division_to_revision'], fact['after_goals']
    if (payload['division_to_revision'] > revision or goals != [item['goal'] for item in sample['items']]
            or fact['created_at'] != sample.get('adjusted_at')):
        raise RecognitionConflict('consolidation_outcome_division_history_unavailable')
    return request


def _outcome_sources(records, owners, project, requests, local_only):
    authority, graph, refs, snapshots = SourceEgressService(records), SourceGraph(), {}, []
    turns, agents = authority_stores(records)
    for request in requests:
        proofs, read_snapshots = product_read_sources(records, turns, agents, request, project)
        for proof in proofs:
            graph.add(proof, root=True)
        privacy = request['privacy']
        materials, frozen_sources = privacy['material_refs'], privacy['source_snapshots']
        if len(materials) != len(frozen_sources):
            raise RecognitionConflict('consolidation_outcome_sources_unavailable')
        for material, frozen in zip(materials, frozen_sources):
            if (set(material) != {'type', 'id', 'revision', 'project_id'} or material['type'] != 'recognition'
                    or frozen['scope'] != {'user_id': 'local-user', 'project_id': material['project_id']}
                    or frozen['roots'] != [{key: material[key] for key in ('type', 'id', 'revision')}]
                    or type(material['revision']) is not int):
                raise RecognitionConflict('consolidation_outcome_sources_unavailable')
        for frozen in (*frozen_sources, *read_snapshots):
            scope = WorkScope(**frozen['scope'])
            if scope.user_id != 'local-user' or scope.project_id not in {project, 'me'}:
                raise RecognitionConflict('consolidation_outcome_source_scope_unavailable')
            roots = [(root['type'], root['id'], root['revision']) for root in frozen['roots']]
            ceiling = _frozen_packet_authority(scope, {'source_egress': frozen}, roots)
            snapshot = authority.snapshot(scope, frozen['roots'])
            if {_closure_identity(node) for node in ceiling['nodes']} != {
                    _closure_identity(node) for node in snapshot['nodes']}:
                raise RecognitionConflict('consolidation_outcome_source_closure_changed')
            if not local_only:
                authority.require(ceiling, 'generation')
                authority.require(snapshot, 'generation')
            if snapshot not in snapshots:
                snapshots.append(snapshot)
            graph.snapshot(snapshot)
            for kind, identity, revision in roots:
                ref = {'type': kind, 'id': identity, 'revision': revision, 'project_id': scope.project_id}
                refs[(scope.project_id, kind, identity, revision)] = ref
    current = validate_graph(graph.result(), 'local-user')
    for node in current['nodes']:
        if node['kind'] != 'material':
            continue
        scope = WorkScope(**node['scope'])
        if node['type'] == 'recognition':
            pref = _remember_optional(records, owners, 'recognition_recall_preferences', node['id'])
            if pref and pref.payload.get('state') == 'forgotten' and pref.payload.get('by', 'user') == 'user':
                raise RecognitionConflict('consolidation_outcome_source_forgotten')
        docs = source_documents(records, scope,
            [node['id']] if node['type'] == 'experience' else [],
            [node['id']] if node['type'] == 'recognition' else [])
        for identity in docs:
            _outcome_document(records, owners, scope.project_id, identity)
    return [refs[key] for key in sorted(refs)], snapshots, current


def outcomes(records, project, *, now=None, local_only=False):
    """Read verified user feedback; local qualification grants no model route."""
    if type(local_only) is not bool:
        raise RecognitionConflict('consolidation_outcome_mode_invalid')
    with records.begin() as tx:
        reader = TransactionRecords(tx)
        if not local_only and is_private_project(reader, project):
            return []
        epoch, instant = privacy_revision(reader), _time(_now() if now is None else now)
        consumed = set()
        for row in reader.list('v2_consolidation_inputs'):
            if row.payload.get('project_id') == project:
                identities = row.payload.get('event_ids', [])
                if not isinstance(identities, list) or any(not isinstance(item, str) for item in identities):
                    return []  # An unknown consumption record cannot grant a second use.
                consumed.update(identities)
        result = []
        for row in reader.list(OUTCOMES):
            payload, identity = row.payload, 'outcome:' + row.object_id
            if payload.get('project_id') != project or identity in consumed or not _outcome_ready(payload, instant):
                continue
            owners, requests = {}, []
            try:
                if payload['kind'] == 'division_adjust':
                    requests.append(_division_request(reader, owners, project, payload))
                    before, after = (json.dumps(payload[key], ensure_ascii=False) for key in ('before_goals', 'after_goals'))
                else:
                    requests.append(_outcome_root(reader, owners, project, payload['document_id'],
                        payload['birth_revision'], payload['turn_id']))
                    _, text = _outcome_history(reader, owners, project, payload['document_id'], payload['from_revision'])
                    policy = get('outcome_correction', version=payload['policy_version'])
                    if payload['kind'] == 'outcome_edit':
                        _, new_text = _outcome_history(reader, owners, project, payload['document_id'], payload['to_revision'])
                        before, after, changed = _changed(text, new_text, policy(operation='limits')['edit_side_chars'])
                        if not changed or (before, after) != (payload['before'], payload['after']):
                            continue
                    else:
                        requests.append(_outcome_root(reader, owners, project, payload['new_document_id'],
                            payload['new_birth_revision'], payload['new_turn_id']))
                        _, new_text = _outcome_history(reader, owners, project, payload['new_document_id'], payload['to_revision'])
                        limit = policy(operation='limits')['outcome_side_chars']
                        before, after = text[:limit], new_text[:limit]
                        if (before, after) != (payload['before'], payload['after']):
                            continue
                        _outcome_title(reader, owners, payload['document_id'], payload['from_revision'],
                            payload['birth_revision'], payload['before_title'])
                        _outcome_title(reader, owners, payload['new_document_id'], payload['to_revision'],
                            payload['new_birth_revision'], payload['after_title'])
                refs, snapshots, graph = _outcome_sources(reader, owners, project, requests, local_only)
            except (RecognitionError, ProductDraftDependencyError, KeyError, TypeError, ValueError):
                continue
            result.append({'event_id': identity, 'type': payload['kind'], 'turn_id': payload['turn_id'],
                'before': before, 'after': after,
                **({key: payload[key] for key in ('before_title', 'after_title', 'new_turn_id')}
                    if payload['kind'] == 'outcome_redo' else {}),
                '_row': row, '_owners': owners, '_refs': refs, '_snapshots': snapshots,
                '_source_graph': graph, '_privacy_revision': epoch, '_local_only': local_only})
        if privacy_revision(reader) != epoch:
            raise RecognitionConflict('consolidation_outcome_privacy_changed')
        return get('consolidate')(None, operation='events', events=result)


def validate_outcomes(records, events, *, project, now=None):
    """Rebuild bodies and every owner binding before freezing, dispatch or commit."""
    WorkScope('local-user', project)
    events = tuple(events)
    if (any(event['_row'].payload['project_id'] != project or type(event['_local_only']) is not bool for event in events)
            or len({event['_local_only'] for event in events}) > 1
            or len({event['event_id'] for event in events}) != len(events)):
        raise RecognitionConflict('consolidation_outcome_scope_changed')
    current = {row['event_id']: row for row in outcomes(records, project,
        now=now, local_only=events[0]['_local_only'])} if events else {}
    for event in events:
        if current.get(event['event_id']) != event:
            raise RecognitionConflict('consolidation_outcome_changed')


def verified_feedback(records, events, *, project, now=None):
    """Render genuine domain facts only, never stage an original or experience."""
    events = tuple(events)
    validate_outcomes(records, events, project=project, now=now)
    return json.dumps({'outcomes': [{key: event[key] for key in (
        'event_id', 'type', 'turn_id', 'before', 'after', 'before_title', 'after_title', 'new_turn_id')
        if key in event} for event in events]}, ensure_ascii=False)


def consumer_outcomes(records, project, *, now=None, local_only=False):
    """Bind feedback to real birth artifacts without staging user actions as sources."""
    from .outcome_corrections import _root
    result = []
    with records.begin() as tx:
        reader, scope = TransactionRecords(tx), WorkScope('local-user', project)
        for event in outcomes(reader, project, now=now, local_only=local_only):
            payload, roots, owners = event['_row'].payload, [], {}
            try:
                if payload['kind'] == 'division_adjust':
                    turn = event['_owners'][('v2_turns', payload['turn_id'])]
                    receipt = turn.payload['receipt']['do']
                    if 'document_id' not in receipt:
                        continue
                    identity = receipt['document_id']
                    if identity is not None:
                        if not isinstance(identity, str) or not identity:
                            continue
                        bound = _root(reader, scope, identity)
                        if bound is None or bound.revisions['task_execution_id'] != payload['turn_id']:
                            continue
                        roots.append((identity, bound.revisions['document_revision']))
                    elif receipt['state'] == 'done' or event['_refs'] or event['_source_graph']['nodes']:
                        # A task with source parents cannot become a parentless insight.
                        continue
                else:
                    roots.append((payload['document_id'], payload['birth_revision']))
                    if payload['kind'] == 'outcome_redo':
                        roots.append((payload['new_document_id'], payload['new_birth_revision']))
                for identity, birth in roots:
                    bound = _root(reader, scope, identity)
                    if bound is None or bound.revisions['document_revision'] != birth:
                        raise RecognitionConflict('consolidation_outcome_birth_changed')
                    _outcome_root(reader, owners, project, identity, birth, bound.revisions['task_execution_id'])
                result.append({**event, '_roots': [{'id': identity, 'revision': birth} for identity, birth in roots],
                    '_root_owners': owners})
            except (RecognitionError, ProductDraftDependencyError, KeyError, TypeError, ValueError):
                continue
    return result


def validate_consumer_outcomes(records, events, *, project, now=None):
    events = tuple(events)
    original = [{key: value for key, value in event.items() if key not in {'_roots', '_root_owners'}}
                for event in events]
    validate_outcomes(records, original, project=project, now=now)
    current = {event['event_id']: event for event in consumer_outcomes(records, project,
        now=now, local_only=events[0]['_local_only'])} if events else {}
    if any(current.get(event['event_id']) != event for event in events):
        raise RecognitionConflict('consolidation_outcome_birth_changed')


def outcome_key(events):
    """Cache identity includes every captured owner, source revision and privacy epoch."""
    return [[event['event_id'], event['_row'].revision,
        [[collection, identity, row.revision if row else None] for (collection, identity), row
            in sorted({**event['_owners'], **event['_root_owners']}.items())],
        event['_roots'], event['_source_graph'], event['_privacy_revision'], event['_local_only']]
        for event in events]
