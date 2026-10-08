"""Qualify explicit review decisions against immutable ASK and source owners."""
import json
from datetime import datetime, timezone

from backend.recognition import RecognitionConflict, RecognitionError, RecognitionService, WorkScope
from core.document_engine import SQLiteDocumentRepository
from ..context_adapter import format_recognition_content
from .budget import _source_texts, user_text
from .followup import _answer, _history_text
from ..kernel.receipt_projection import kernel_call_groups
from ..original_sources import document_roots, original, resolve, source_store
from ..research_packets import authority_stores
from ..source_egress import SourceEgressService, validate_product_draft_source
from ..source_graph import SourceGraph, validate_graph
from ..privacy_state import privacy_revision, is_private_project
from .insights import source_documents
from .learning_events import _signal_decision_project
from .policies import get, version


_LAYERS = {'L3': 'insight', 'L2': 'summary', 'L1': 'note', 'L0': 'source'}


def _clock(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    if parsed is None:
        parsed = datetime.now(timezone.utc)
    if not isinstance(parsed, datetime) or parsed.tzinfo is None:
        raise RecognitionConflict('review_feedback_time_invalid')
    return parsed


def _remember(records, owners, collection, identity):
    row = records.read(collection, identity)
    if row is None:
        raise RecognitionConflict('review_feedback_owner_unavailable')
    key = collection, identity
    if key in owners and owners[key] != row:
        raise RecognitionConflict('review_feedback_owner_changed')
    owners[key] = row
    return row


def _scope(row, project):
    return row.payload.get('scope') == {'user_id': 'local-user', 'project_id': project}


def _ask(records, turns, groups, identity, project, owners, proofs, memo, visited=frozenset()):
    if identity in visited or len(proofs) + len(visited) >= get('review').feedback_history_turns:
        raise RecognitionConflict('review_feedback_history_unavailable')
    if identity in memo:
        return memo[identity]
    visited = visited | {identity}
    row = _remember(records, owners, 'v2_turns', identity)
    request = turns.get_request(identity)
    terminal = [event for event in turns.events_after(identity)
        if event.get('type') in {'turn.completed', 'turn.failed', 'turn.cancelled'}]
    result = turns.get_immutable_payload(identity, 'product-answer-result-v2')
    wire = turns.get_immutable_payload(identity, 'answer-model-input-answer')
    if (request is None or request.get('turn_id') != identity
            or request.get('scope', {}).get('project_id') != project
            or request.get('desired_outcome') != 'project.answer'
            or request.get('operation_id') != 'answer-' + identity
            or not terminal or terminal[-1]['type'] != 'turn.completed'
            or result is None or wire is None):
        raise RecognitionConflict('review_feedback_ask_unavailable')
    group = next((group for group in groups if group['turn_id'] == identity
        and group['project_id'] == project and group['request'] == request), None)
    if (group is None or group.get('answer') != result[1] or group.get('answer_input') != wire[1]
            or not any(call.get('turn_id') == identity and call.get('model_call_purpose') == 'primary'
                and call.get('status') == 'completed' for call in group['calls'])
            or wire[1].get('purpose') != 'primary' or not wire[1].get('messages')):
        raise RecognitionConflict('review_feedback_primary_unavailable')
    question = request.get('input', {}).get('text')
    answer = result[1].get('receipt', {}).get('ask')
    public = row.payload.get('receipt', {}).get('ask')
    if (row.payload.get('intent') != 'ask' or row.payload.get('project_id') != project
            or row.payload.get('by') == 'admin' or not isinstance(question, str)
            or row.payload.get('user_text') != question or not isinstance(answer, dict)
            or not isinstance(answer.get('answer'), str) or not isinstance(public, dict)
            or public.get('answer') != answer['answer']
            or public.get('egress_receipt_id') != answer.get('egress_receipt_id')):
        raise RecognitionConflict('review_feedback_ask_binding_changed')
    egress = _remember(records, owners, 'workspace_ask_receipts', answer['egress_receipt_id'])
    # Original owner creates started r1 once, then completes r2 once. Post-completion
    # edits lose the only existing revision-bound lifecycle proof and must defer.
    if (egress.revision != 2 or egress.payload.get('id') != answer['egress_receipt_id']
            or egress.payload.get('project_id') != project or egress.payload.get('status') != 'completed'):
        raise RecognitionConflict('review_feedback_egress_unavailable')
    chosen, sources = result[1].get('chosen'), egress.payload.get('sources')
    context = answer.get('context', {}).get('entries', [])
    numbered = [entry for entry in context if not entry.get('persona')]
    if not isinstance(chosen, list) or not isinstance(sources, list) or len(numbered) != len(chosen):
        raise RecognitionConflict('review_feedback_sources_unavailable')
    # Profile sources belong to me. No same-project retained artifact is proved here.
    if len(sources) != len(chosen) or any(entry.get('persona') for entry in context):
        raise RecognitionConflict('review_feedback_cross_scope_deferred')
    refs, documents, framed = [], set(), []
    scope = WorkScope('local-user', project)
    for candidate, source, entry in zip(chosen, sources, numbered):
        own, layer = candidate['entry'], _LAYERS[candidate['layer']]
        identity = (own['id'] if layer == 'insight' else own['document_id']
            if layer in {'note', 'summary'} else own.get('item_id') or own['source_id'])
        if (candidate.get('project_id') != project or entry.get('project_id', project) != project
                or candidate.get('persona') or (entry.get('layer'), entry.get('id')) != (layer, identity)
                or type(source.get('revision')) is not int or source['revision'] < 1
                or not isinstance(source.get('windows'), list)
                or any(set(window) != {'start', 'end'} or type(window['start']) is not int
                    or type(window['end']) is not int or not 0 <= window['start'] <= window['end']
                    for window in source['windows'])):
            raise RecognitionConflict('review_feedback_source_binding_changed')
        if layer == 'insight':
            owner = _remember(records, owners, 'recognitions', identity)
            if (source.get('kind') != 'recognition' or source.get('id') != identity
                    or owner.revision != source['revision'] or not _scope(owner, project)
                    or owner.payload.get('state') != 'active'):
                raise RecognitionConflict('review_feedback_recognition_changed')
            recognition_service = RecognitionService(records, product_draft_validator=validate_product_draft_source)
            recognition = (recognition_service.get_recognition(scope=scope, recognition_id=owner.object_id)
                if hasattr(records, '_connect') else recognition_service._qualified_recognition(records, owner))
            if (recognition is None or recognition.id != owner.object_id or recognition.scope != scope
                    or recognition.revision != owner.revision):
                raise RecognitionConflict('review_feedback_recognition_changed')
            qualified = recognition.retrieval_projection() if recognition.authorized else None
            if qualified is None or source['windows'] != [{'start': 0, 'end': len(qualified['content'])}]:
                raise RecognitionConflict('review_feedback_window_changed')
            excerpt = format_recognition_content(qualified)
            refs.append({'type': 'recognition', 'id': identity, 'revision': owner.revision, 'project_id': project})
        elif source.get('kind') == 'document':
            document = _remember(records, owners, 'documents', source['id'])
            if (source['id'] != own.get('document_id') or document.payload.get('project_id') != project
                    or document.payload.get('revision') != source['revision']
                    or document.payload.get('status') == 'archived'):
                raise RecognitionConflict('review_feedback_document_changed')
            documents.add(document.object_id)
            if layer == 'source' and source.get('coordinate_space') == 'workspace_source_text_v1':
                item = _remember(records, owners, 'workspace_items', own['item_id'])
                if item.payload.get('project_id') != project or item.revision != source.get('item_revision'):
                    raise RecognitionConflict('review_feedback_original_changed')
                roots = [('original_item', item.object_id, item.revision)]
                text = item.payload.get('source_text')
            elif layer in {'note', 'summary'}:
                roots = document_roots(records, scope, document.payload.get('source_refs', []))
                if source.get('coordinate_space') != 'document_markdown_v1':
                    raise RecognitionConflict('review_feedback_source_space_unavailable')
                text = SQLiteDocumentRepository(records).markdown(document.object_id, revision=source['revision'])
            else:
                raise RecognitionConflict('review_feedback_source_space_unavailable')
            excerpt = _window_text(text, source['windows'])
            refs.extend({'type': kind, 'id': identity, 'revision': revision, 'project_id': project}
                for kind, identity, revision in roots)
        elif layer == 'source' and source.get('kind') == 'source':
            if source.get('id') != own.get('source_id') or identity != own.get('source_id'):
                raise RecognitionConflict('review_feedback_source_binding_changed')
            kind, identity = resolve(records, scope, source['id'], kind='source')
            value = original(records, scope, kind, identity)
            if value.revision != source['revision']:
                raise RecognitionConflict('review_feedback_original_changed')
            if source.get('coordinate_space') != 'source_content_v1':
                raise RecognitionConflict('review_feedback_source_space_unavailable')
            body = source_store(records).read('sources', identity)
            metadata = body.get('metadata')
            text = (metadata.get('content_snapshot') or metadata.get('content') or '') if isinstance(metadata, dict) else ''
            excerpt = _window_text(text, source['windows'])
            refs.append({'type': kind, 'id': identity, 'revision': value.revision, 'project_id': project})
        else:
            raise RecognitionConflict('review_feedback_source_space_unavailable')
        framed.append({'id': identity, 'title': entry['title'], 'excerpt': excerpt})
    compose = request.get('policy_versions', {}).get('compose')
    if not isinstance(compose, str):
        raise RecognitionConflict('review_feedback_compose_unavailable')
    fragments = get('compose', version=compose)(_source_texts, framed, operation='sources')
    history, ancestors = [], answer.get('trace', [])
    ancestors = ancestors[0].get('history_turn_ids', []) if ancestors else []
    if (not isinstance(ancestors, list) or len(ancestors) > 3
            or len({item['id'] for item in ancestors}) != len(ancestors)):
        raise RecognitionConflict('review_feedback_history_unavailable')
    for ancestor in ancestors:
        previous = _remember(records, owners, 'v2_turns', ancestor['id'])
        if (set(ancestor) != {'id', 'revision'} or type(ancestor['revision']) is not int
                or previous.revision != ancestor['revision']
                or previous.payload.get('project_id') != project
                or not row.payload.get('thread_id')
                or previous.payload.get('thread_id') != row.payload['thread_id']):
            raise RecognitionConflict('review_feedback_history_changed')
        previous_question, previous_answer, inherited_refs, inherited_docs, _ = _ask(
            records, turns, groups, ancestor['id'], project, owners, proofs, memo, visited)
        history.append({'question': previous_question, 'answer': _answer(previous_answer)})
        refs.extend(inherited_refs); documents.update(inherited_docs)
    count = next((part['count'] for part in answer.get('context', {}).get('parts', [])
        if part.get('key') == 'history'), None)
    if count != len(ancestors):
        raise RecognitionConflict('review_feedback_history_unavailable')
    # Exact original composition includes the final filtered history in its saved order.
    expected = get('compose', version=compose)(user_text, fragments, question, _history_text(history))
    sent = [message['content'] for message in wire[1]['messages']
        if message.get('role') == 'user' and isinstance(message.get('content'), str)]
    if not sent or sent[-1] != expected:
        raise RecognitionConflict('review_feedback_wire_material_changed')
    proof = {'request': request, 'result': result, 'wire': wire, 'terminal': terminal[-1],
        'primary_calls': [call for call in group['calls'] if call.get('model_call_purpose') == 'primary']}
    proofs[row.object_id] = proof
    memo[row.object_id] = question, answer['answer'], refs, documents, proof
    return memo[row.object_id]


def _window_text(text, windows):
    if (not isinstance(text, str) or not windows or any(not window['start'] < window['end'] <= len(text)
            for window in windows)):
        raise RecognitionConflict('review_feedback_window_unavailable')
    return '\n…\n'.join(text[window['start']:window['end']] for window in windows)


def review_corrections(records, project, *, now=None, local_only=False):
    """Explicit confirmations survive recording OFF/clear; missing proof defers."""
    if type(local_only) is not bool:
        raise ValueError('review_feedback_local_only_invalid')
    clock, scope = _clock(now), WorkScope('local-user', project)
    consumed = {identity for row in records.list('v2_consolidation_inputs')
        if row.payload.get('project_id') == project for identity in row.payload.get('event_ids', [])}
    try:
        turns, _ = authority_stores(records)
    except RecognitionError:
        return []
    groups = kernel_call_groups(source_store(records).root.parent, project=project, remote_only=False,
        records=records, include_answer_input=True)
    output = []
    for row in records.list('v2_signal_decisions'):
        event_id = 'signal-decision:' + row.object_id
        try:
            if (_signal_decision_project(records, row.payload) != project or event_id in consumed
                    or _clock(row.payload['at']) > clock or not local_only and is_private_project(records, project)):
                continue
            epoch = privacy_revision(records)
            owners = {('v2_signal_decisions', row.object_id): row}
            questions, answers, refs, docs, proofs, memo = [], [], [], set(), {}, {}
            for identity in row.payload['turn_ids']:
                question, answer, source_refs, documents, proof = _ask(records, turns, groups, identity, project, owners, proofs, memo)
                questions.append(question); answers.append(answer); refs.extend(source_refs); docs.update(documents)
                proofs[identity] = proof
            authority, graph, snapshots = SourceEgressService(records), SourceGraph(), []
            refs = {(ref['type'], ref['id'], ref['revision']): ref for ref in refs}
            if refs:
                snapshot = authority.snapshot(scope, [{key: ref[key] for key in ('type', 'id', 'revision')}
                    for ref in refs.values()])
                if not local_only:
                    authority.require(snapshot, 'generation')
                graph.snapshot(snapshot); snapshots.append(snapshot)
            recognitions, experiences = set(), set()
            for node in graph.result()['nodes']:
                if node.get('kind') != 'material':
                    continue
                if node['scope'] != {'user_id': 'local-user', 'project_id': project}:
                    raise RecognitionConflict('review_feedback_cross_scope_deferred')
                if node['type'] in {'recognition', 'experience'}:
                    collection = 'recognitions' if node['type'] == 'recognition' else 'recognition_experiences'
                    owner = _remember(records, owners, collection, node['id'])
                    if owner.revision != node['source_revision'] or not _scope(owner, project):
                        raise RecognitionConflict('review_feedback_ancestor_changed')
                    (recognitions if node['type'] == 'recognition' else experiences).add(node['id'])
            docs.update(source_documents(records, scope, experiences, recognitions))
            for identity in docs:
                document = _remember(records, owners, 'documents', identity)
                if document.payload.get('project_id') != project or document.payload.get('status') == 'archived':
                    raise RecognitionConflict('review_feedback_document_changed')
                pref = records.read('v2_document_recall', identity)
                owners[('v2_document_recall', identity)] = pref
                if pref and pref.payload.get('state') == 'forgotten' and pref.payload.get('by', 'user') == 'user':
                    raise RecognitionConflict('review_feedback_source_forgotten')
            for identity in recognitions:
                pref = records.read('recognition_recall_preferences', identity)
                owners[('recognition_recall_preferences', identity)] = pref
                if pref and pref.payload.get('state') == 'forgotten' and pref.payload.get('by', 'user') == 'user':
                    raise RecognitionConflict('review_feedback_source_forgotten')
            # Existing document experiences are reuse candidates, never created here.
            for experience in records.list('recognition_experiences'):
                if not _scope(experience, project):
                    continue
                source_refs = experience.payload.get('provenance', {}).get('source_refs', [])
                if any(ref.get('type') == 'document' and ref.get('id') in docs
                        and records.read('documents', ref['id']).payload.get('revision') == ref.get('revision')
                        for ref in source_refs):
                    experiences.add(experience.object_id)
                    owners[('recognition_experiences', experience.object_id)] = experience
            retained = [{'type': 'recognition', 'id': identity, 'revision': owners[('recognitions', identity)].revision}
                for identity in sorted(recognitions)] + [
                {'type': 'experience', 'id': identity, 'revision': owners[('recognition_experiences', identity)].revision}
                for identity in sorted(experiences)]
            if refs and not retained:
                raise RecognitionConflict('review_feedback_retention_unavailable')
            if retained:
                retained_snapshot = authority.snapshot(scope, retained)
                if not local_only:
                    authority.require(retained_snapshot, 'generation')
                existing = {(node['type'], node['id'], node['source_revision']) for node in retained_snapshot['nodes']}
                required = {(node['type'], node['id'], node['source_revision']) for snapshot in snapshots
                    for node in snapshot['nodes'] if node['type'] in {'original_item', 'original_source'}}
                if not required <= existing:
                    raise RecognitionConflict('review_feedback_retention_incomplete')
                graph.snapshot(retained_snapshot); snapshots.append(retained_snapshot)
            before, after = get('review').feedback(row.payload['kind'], questions, answers)
            if privacy_revision(records) != epoch:
                raise RecognitionConflict('review_feedback_privacy_changed')
            output.append({'event_id': event_id, 'type': 'answer_miss', 'before': before, 'after': after,
                'turn_ids': list(row.payload['turn_ids']), '_row': row, '_owners': owners,
                '_refs': [{**ref, 'project_id': project} for ref in retained], '_snapshots': snapshots,
                '_source_graph': validate_graph(graph.result(), 'local-user'), '_privacy_revision': epoch,
                '_local_only': local_only, '_review_version': version('review'),
                '_source_experience_ids': sorted(experiences), '_source_recognition_ids': sorted(recognitions),
                '_documents': sorted(docs), '_ask_proofs': proofs})
        except (RecognitionError, KeyError, TypeError, ValueError, AttributeError):
            continue
    return output


def validate_review_corrections(records, events, *, project, now=None):
    events = tuple(events)
    if len({event['event_id'] for event in events}) != len(events) or len({event['_local_only'] for event in events}) > 1:
        raise RecognitionConflict('review_feedback_identity_changed')
    current = {event['event_id']: event for event in review_corrections(records, project, now=now,
        local_only=events[0]['_local_only'])} if events else {}
    if any(current.get(event['event_id']) != event for event in events):
        raise RecognitionConflict('review_feedback_changed')


def review_key(events):
    return [[event['event_id'], event['_row'].revision,
        [[collection, identity, row.revision if row else None]
            for (collection, identity), row in sorted(event['_owners'].items())],
        event['_source_graph'], event['_privacy_revision'], event['_local_only'], event['_review_version'],
        [[identity, proof['result'][0], proof['wire'][0], proof['terminal']['sequence']]
            for identity, proof in sorted(event['_ask_proofs'].items())]] for event in events]


def review_feedback(records, events, *, project, now=None):
    events = tuple(events)
    validate_review_corrections(records, events, project=project, now=now)
    return json.dumps({'corrections': [{key: event[key] for key in ('event_id', 'type', 'before', 'after', 'turn_ids')}
        for event in events]}, ensure_ascii=False)
