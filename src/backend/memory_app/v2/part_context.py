"""Frozen references to admitted originals and completed answer source facts."""
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass

from backend.recognition import RecognitionConflict, WorkScope
from ..source_egress import SourceEgressService
from ..part_context_binding import COLLECTION, bind_context
from ..transaction_records import TransactionRecords
from .privacy import privacy_revision
from .followup import validate_history
from .policies import get
from .route_reuse import dependency_parent
from ..route_reuse_binding import explicit_source_ref, validate_explicit_source
from .route_source_statements import statement_source, remembered_source

PREFIX = '本次输入的依赖上下文（没有资料编号，不要为这些内容生成编号引用）：\n'
ATTACHMENT_PREFIX = get('attachment_context', version='@1')()


def _json_value(value):
    if isinstance(value, WorkScope):
        return {'user_id':value.user_id, 'project_id':value.project_id}
    if is_dataclass(value):
        return _json_value(asdict(value))
    if isinstance(value, dict):
        return {key:_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def freeze_answer_authority(plan, history, parent, turn_id):
    history = deepcopy(history)
    for dependency in history.get('dependencies', []):
        dependency['preferences'] = [[list(key), value] for key, value in dependency['preferences'].items()]
    return _json_value({'parent':parent, 'turn_id':turn_id, 'project_id':plan['project_id'], 'target':plan['target'],
        'chosen':[{key:value for key, value in candidate.items()
                   if key != 'windows' or candidate['kind'] == 'search'} for candidate in plan['chosen']],
        'profile':plan['profile'], 'history':history,
        'overview':getattr(plan.get('overview_guard'), 'frozen_binding', None),
        'bookshelf':getattr(plan.get('bookshelf_guard'), 'frozen_binding', None),
        'search':getattr(plan.get('search_guard'), 'frozen_binding', None)})


def _answer_facts(query, project, identity, parent):
    from ..kernel.answer_turns import RESULT_KIND
    store = query.answer_turns.application.state.ai_turn_store
    request = store.get_request(identity)
    saved = store.get_immutable_payload(identity, RESULT_KIND)
    events = tuple(store.events_after(identity))
    public = query.records.read('v2_turns', identity)
    if (not request or request['desired_outcome'] != 'project.answer'
            or request['scope']['project_id'] != project or not saved
            or not events or events[-1]['type'] != 'turn.completed' or public is None
            or request['input']['text'] != public.payload.get('user_text')
            or saved[1].get('receipt', {}).get('ask', {}).get('answer') != public.payload.get('receipt', {}).get('ask', {}).get('answer')):
        raise RecognitionConflict('part answer authority unavailable')
    facts = saved[1].get('multipart_authority')
    if not facts or facts['parent'] != parent or facts['project_id'] != project or facts['turn_id'] != identity:
        raise RecognitionConflict('part answer authority unavailable')
    query.validate_answer_request(query.models, request)
    return facts


def _validate_answer_facts(query, facts):
    plan = deepcopy(facts)
    plan['scope'] = WorkScope('local-user', facts['project_id'])
    for candidate in plan['chosen']:
        candidate['scope'] = WorkScope(**candidate['scope'])
        if candidate['kind'] == 'search' and 'windows' in candidate:
            from core.search_and_recall.evidence_windows import EvidenceWindow
            candidate['windows'] = tuple(EvidenceWindow(**window) for window in candidate['windows'])
    if facts['overview'] is not None:
        from .overviews import validate_navigation_binding
        plan['overview_guard'] = lambda: validate_navigation_binding(query, facts['overview'])
    if facts['bookshelf'] is not None:
        from .bookshelf import validate_bookshelf_binding
        plan['bookshelf_guard'] = lambda: validate_bookshelf_binding(query, facts['bookshelf'], turn_id=facts['turn_id'])
    if facts.get('search') is not None:
        from .search import validate_search_binding
        plan['search_guard'] = lambda: validate_search_binding(query, facts['search'], plan)
    history = deepcopy(facts['history'])
    for dependency in history.get('dependencies', []):
        dependency['preferences'] = {tuple(key):value for key, value in dependency['preferences']}
        dependency['snapshots'] = [(WorkScope(**scope), snapshot) for scope, snapshot in dependency['snapshots']]
        for candidate in dependency['chosen']:
            candidate['scope'] = WorkScope(**candidate['scope'])
    # 让原搜索输入闭包也复验历史来源，保持首答的完整 Source 依赖。
    plan['history_guard'] = lambda: validate_history(query, facts['project_id'], history, facts['target'])
    query.validate_ask_plan(plan)


def validate_answer_authority(query, project, identity, parent, expected=None):
    """Reuse the first answer's immutable source facts for every later wire."""
    facts = _answer_facts(query, project, identity, parent)
    if expected is not None and facts != expected:
        raise RecognitionConflict('part answer authority changed')
    _validate_answer_facts(query, facts)
    return facts


def prepare_context(records, query, project, parent, dependencies, *, recipient=None):
    if not dependencies:
        return None
    if query.origin_admission is not None:
        return _prepare_shared_context(records, query, project, parent, dependencies, recipient=recipient)
    owner = query.records if isinstance(records, TransactionRecords) else records
    authority = SourceEgressService(owner, reader=records if isinstance(records, TransactionRecords) else None)
    scope = WorkScope('local-user', project)
    originals, answers, texts, refs, explicit = [], [], [], [], []
    parent_row = records.read('v2_turns', parent)
    attachments = bool(parent_row and parent_row.payload.get('attachment_item_ids'))
    titles = []
    for identity in dependencies:
        row = records.read('v2_turns', identity)
        if row is None or parent_row is None:
            raise RecognitionConflict('part dependency scope changed')
        original_parent = dependency_parent(records, parent, identity, source_owner=owner)
        if row.payload['project_id'] != project and row.payload['intent'] == 'remember':
            reuse = records.read('v2_route_correction_reuses', parent)
            operation = records.read('v2_route_correction_operations', reuse.payload['operation_id']) if reuse else None
            if operation is None or operation.payload['source_origin']['kind'] != 'personal':
                raise RecognitionConflict('part statement origin unavailable')
            material = remembered_source(records, row)
            explicit.append({'kind': 'remember', 'turn_id': identity, 'revision': row.revision,
                'project_id': row.payload['project_id'], 'namespace': operation.payload['source_origin']['namespace'],
                'ref': material['ref'], 'content': material['content'], 'text': material['text'],
                'snapshot': authority.snapshot(WorkScope('local-user', row.payload['project_id']), material['refs'])})
            texts.append(material['text'])
            refs.append(material['ref'])
            continue
        if row.payload['project_id'] != project and row.payload['intent'] != 'inspiration':
            raise RecognitionConflict('part dependency scope changed')
        if row.payload['intent'] == 'remember':
            item = row.payload['receipt']['remember']['item_id']
            span = row.payload['user_text']
            source = records.read('workspace_items', item)
            if attachments and item in parent_row.payload['attachment_item_ids']:
                snapshot = authority.snapshot_original_content(scope, item)
                originals.append(snapshot)
                titles.append(source.payload.get('original_name') or source.payload.get('filename') or source.payload['title'])
                texts.append(source.payload['source_text'])
                refs.append({'kind':'source', 'object_id':item, 'uri':f'crp://default/workspace/{item}'})
                continue
            binding = None
            if source.payload['input_kind'] != 'text':
                parent_row = records.read('v2_turns', original_parent)
                if parent_row is None:
                    raise RecognitionConflict('part dependency original parent unavailable')
                parent_text = parent_row.payload['user_text']
                start = parent_text.index(span)
                binding = {'child_id':identity, 'parent_id':original_parent, 'parent_text':parent_text,
                           'start':start, 'end':start + len(span)}
            originals.append(authority.snapshot_original_content(scope, item, span=span, input_binding=binding))
            texts.append(span)
            refs.append({'kind':'source', 'object_id':item, 'uri':f'crp://default/workspace/{item}'})
        elif row.payload['intent'] == 'ask':
            facts = _answer_facts(query, project, identity, original_parent)
            answers.append({'id':identity, 'revision':row.revision,
                'question':row.payload['user_text'], 'answer':row.payload['receipt']['ask']['answer'],
                'authority':facts})
            texts.append(row.payload['receipt']['ask']['answer'])
            refs.append({'kind':'session_event', 'object_id':identity, 'uri':f'crp://default/v2_turns/{identity}'})
        elif row.payload['intent'] == 'inspiration':
            reuse = records.read('v2_route_correction_reuses', parent)
            operation = records.read('v2_route_correction_operations', reuse.payload['operation_id']) if reuse else None
            if operation is None or operation.payload['source_origin']['kind'] != 'personal':
                raise RecognitionConflict('part statement origin unavailable')
            statement = statement_source(records, row, source_owner=query.records)
            reference = explicit_source_ref(identity)
            explicit.append({'kind': 'inspiration', 'turn_id': identity, 'revision': row.revision,
                'project_id': row.payload['project_id'], 'namespace': operation.payload['source_origin']['namespace'],
                'ref': reference, 'snapshot': authority.snapshot(
                    WorkScope('local-user', row.payload['project_id']), statement['refs']), 'text': statement['text']})
            texts.append(statement['text'])
            refs.append(reference)
        else:
            raise RecognitionConflict('part dependency intent changed')
    context = {'project_id':project, 'parent':parent, 'originals':originals, 'answers':answers,
               'privacy_revision':privacy_revision(records), 'refs':refs,
               'text':PREFIX + '\n\n'.join(texts), 'target':query.ask_target()}
    if explicit:
        context['explicit_sources'] = explicit
    if attachments:
        context['attachment_titles'] = titles
        context['text'] = attachment_text(context)
    from ..route_correction_scopes import correction_parent_id
    if correction_parent_id(parent):
        context['recipient'] = recipient
    validate_context(records, query, context)
    return context


def attachment_text(context):
    blocks = []
    for title, snapshot in zip(context['attachment_titles'], context['originals']):
        windows = snapshot['content']['windows']
        blocks.append(f"[{title}](crp://default/workspace/{snapshot['id']})\n" + '\n…\n'.join(window['text'] for window in windows))
    blocks.extend(answer['answer'] for answer in context['answers'])
    blocks.extend(source['text'] for source in context.get('explicit_sources', []))
    return ATTACHMENT_PREFIX + '\n\n'.join(blocks)


def fit_attachment_context(records, query, context, question, fits):
    if not context or 'attachment_titles' not in context or fits(context):
        return context
    from .attachments import allocate_attachment_windows
    authority, scope = SourceEgressService(records), WorkScope('local-user', context['project_id'])
    def project(selections):
        current = deepcopy(context)
        for snapshot, selection in zip(current['originals'], selections):
            snapshot['content']['windows'] = [asdict(window) for window in selection.windows]
        current['text'] = attachment_text(current)
        return current
    selections = allocate_attachment_windows(
        [original['content']['identity']['source_text'] for original in context['originals']], question,
        titles=context['attachment_titles'], admission=lambda values: fits(project(values)))
    if selections is None:
        raise RecognitionConflict('attachment dependency exceeds input budget')
    selected = project(selections)
    for index, (original, selection) in enumerate(zip(context['originals'], selections)):
        selected['originals'][index] = authority.snapshot_original_content(scope, original['id'],
            windows=[asdict(window) for window in selection.windows])
    selected['text'] = attachment_text(selected)
    if not fits(selected):
        raise RecognitionConflict('attachment dependency exceeds input budget')
    validate_context(records, query, selected)
    return selected


def validate_context(records, query, context):
    if context.get('scope_kind') == 'shared':
        return _validate_shared_context(records, query, context)
    if query.origin_admission is not None:
        raise RecognitionConflict('part_shared_context_namespace_unavailable')
    if privacy_revision(records) != context['privacy_revision']:
        raise RecognitionConflict('part dependency privacy changed')
    project = context['project_id']
    owner = query.records if isinstance(records, TransactionRecords) else records
    reader = records if isinstance(records, TransactionRecords) else None
    _validate_parent(records, context, owner_records=owner)
    authority = SourceEgressService(owner, reader=reader)
    for source in context.get('explicit_sources', []):
        validate_explicit_source(owner, context, source, statement_source, reader=reader)
    for original in context['originals']:
        authority.validate_original_content(WorkScope('local-user', project), original, 'generation')
    for answer in context['answers']:
        dependency_parent(records, context['parent'], answer['id'], source_owner=owner)
        row = records.read('v2_turns', answer['id'])
        if (row is None or row.revision != answer['revision'] or row.payload.get('project_id') != project
                or row.payload['user_text'] != answer['question']
                or row.payload['receipt']['ask']['answer'] != answer['answer']):
            raise RecognitionConflict('part answer binding changed')
        facts = _answer_facts(query, project, answer['id'], dependency_parent(records, context['parent'], answer['id'], source_owner=owner))
        if facts != answer['authority']:
            raise RecognitionConflict('part answer authority changed')
        _validate_answer_facts(query, facts)


def _validate_parent(records, context, child_id=None, *, owner_records=None):
    parent = records.read('v2_turns', context['parent'])
    from ..route_correction_scopes import correction_parent_id, planned_child_scope
    if correction_parent_id(context['parent']):
        recipient = context.get('recipient')
        if not isinstance(recipient, str) or child_id is not None and child_id != recipient:
            raise RecognitionConflict('part dependency recipient changed')
        proof = planned_child_scope(owner_records if owner_records is not None else records,
            context['parent'], recipient, reader=records if isinstance(records, TransactionRecords) else None)
        if proof['target']['project_id'] != context['project_id']:
            raise RecognitionConflict('part dependency scope changed')
        return
    if (parent is None or parent.payload.get('intent') != 'multi'
            or parent.payload.get('project_id') != context['project_id']):
        raise RecognitionConflict('part dependency parent changed')
    if child_id is not None:
        child = records.read('v2_turns', child_id)
        if child is not None:
            dependency_parent(records, context['parent'], child_id, source_owner=owner_records)


def validate_bound_context(records, models, request, query=None):
    marker = f"crp://default/{COLLECTION}/{request['turn_id']}"
    row = records.read(COLLECTION, request['turn_id'])
    if not any(ref.get('uri') == marker for ref in request['input']['refs']):
        if row is not None:
            raise RecognitionConflict('part dependency marker changed')
        return
    if (row is None or row.revision != 1 or row.payload['input_refs'] != request['input']['refs']
            or row.payload['project_id'] != request['scope']['project_id']):
        raise RecognitionConflict('part dependency binding changed')
    if query is None:
        raise RecognitionConflict('part dependency query authority unavailable')
    # ASK 首次发送时公开子任务尚未入库，父任务已持久保存预分配成员。
    session = request.get('session_id', '')
    if request.get('desired_outcome') == 'project.answer':
        child_id = request['turn_id']
    elif request.get('desired_outcome') == 'project.task' and session.startswith('session-'):
        child_id = session[len('session-'):]
    else:
        raise RecognitionConflict('part dependency recipient unavailable')
    owner = query.execution_owner.records if query.origin_admission is not None else query.records
    _validate_parent(records, row.payload['context'], child_id, owner_records=owner)
    child = records.read('v2_turns', child_id)
    intent = 'ask' if request['desired_outcome'] == 'project.answer' else 'do'
    if child is not None and (child.payload.get('intent') != intent
            or intent == 'do' and child.payload.get('receipt', {}).get('do', {}).get('kernel_turn_id') != request['turn_id']):
        raise RecognitionConflict('part dependency recipient changed')
    validate_context(records, query, row.payload['context'])
    return row.payload['context']


@contextmanager
def _shared_context_reader(records, query):
    from .material_origin_readers import qualified_record_read
    from .shared_auxiliary import _qualified_commit, current_qualified_commit_scope
    from .shared_rendered_preparation import _query_current
    admission = query.origin_admission
    if admission is None or query.execution_owner.records is not admission.destination.records:
        raise RecognitionConflict('part_shared_context_owner_changed')
    current = current_qualified_commit_scope()
    if isinstance(records, TransactionRecords):
        if current is None or current._admission is not admission:
            raise RecognitionConflict('part_shared_context_source_scope_required')
        current._actor_reader(records)
        _query_current(query)
        yield records
        current._validate()
        return
    if records is not admission.destination.records:
        raise RecognitionConflict('part_shared_context_owner_changed')
    _query_current(query)
    with _qualified_commit(admission) as qualification:
        with qualified_record_read(records, validate=qualification._validate) as actual:
            view = TransactionRecords(actual)
            qualification._actor_reader(view)
            yield view
            qualification._actor_reader(view)


def _shared_answer(query, records, row, parent):
    from core.ai_kernel.product_admission import admission_identity
    from .material_origin_context import _entry
    from .shared_history import read_model_history_answer
    actual = read_model_history_answer(query, row.object_id, row.revision, reader=records)
    if actual['public'] != row:
        raise RecognitionConflict('part_shared_answer_public_changed')
    _validate_shared_answer_inputs(query, records, actual, set())
    answer = actual['facts']['answer']
    if not isinstance(answer.get('answer'), str) or not answer['answer'].strip():
        raise RecognitionConflict('part_shared_answer_body_unavailable')
    return {'id': row.object_id, 'revision': row.revision, 'parent': parent,
        'question': actual['facts']['question'], 'answer': answer['answer'], 'no_match': bool(answer.get('no_match')),
        'authority': _entry(actual), 'request_identity': admission_identity(actual['facts']['request']),
        'result_ref': actual['facts']['result_ref'],
        'inputs': [{'kind': item['kind'], 'input_ref': item['input_ref'], 'binding_ref': item['binding_ref'],
            'model_request_id': item['model_request_id'], 'material_proof': deepcopy(item['payload']['material_proof'])}
            for item in actual['model_inputs']]}


def _validate_shared_answer_inputs(query, records, actual, seen, validated=None):
    from ..context_adapter import format_recognition_content
    from ..source_egress import recognition_service
    from .material_origin_context import _entry
    from .followup import _answer, _history_text
    from .material_origin_sql import _validate_commit_sql, _validate_primary_body
    from .model_material_coverage import frozen_primary_plan, validate_primary_guards
    from .primary_wire_messages import PrimaryWireProjection
    from .shared_auxiliary import current_qualified_commit_scope
    from .shared_history import read_model_history_answer
    from .shared_task_reads import _origin_reader
    from .material_origin_admission import BoundMaterialOrigins
    from .material_origin_privacy import freeze_origin_materials
    identity = actual['snapshot'].turn_id
    validated = set() if validated is None else validated
    if identity in seen or len(seen) >= 32:
        raise RecognitionConflict('part_shared_answer_history_cycle')
    if identity in validated:
        return
    seen = {*seen, identity}
    qualification = current_qualified_commit_scope()
    if qualification is None or qualification._admission is not query.origin_admission:
        raise RecognitionConflict('part_shared_context_source_scope_required')
    qualification._actor_reader(records)
    connections = {query.source_namespace: qualification._source_reader.connection,
        query.origin_admission.personal['namespace']: records.connection}
    for item in actual['model_inputs']:
        proof = item['payload']['material_proof']
        selection = proof['selection']
        origins = BoundMaterialOrigins(query.origin_admission, query.origin_admission.lease, actual['origin'].ref)
        allowed, current_privacy, _ = freeze_origin_materials(origins, query.models,
            query.origin_admission.lease.project_id, selection['origin_materials'],
            local_only=query.ask_target()['execution_location'] == 'local', reader=records)
        if (len(allowed) != len(selection['origin_materials'])
                or query.ask_target()['execution_location'] == 'remote' and not current_privacy['allow_remote']):
            raise RecognitionConflict('part_shared_answer_current_privacy_changed')
        if query.ask_target()['execution_location'] == 'remote':
            authority = SourceEgressService(query.execution_owner.records, reader=records)
            for snapshot in proof['privacy']['source_snapshots']:
                authority.require(snapshot, 'generation')
        _validate_commit_sql(selection['sql_identities'], connections, qualification._validate)
        frozen, guards, history = frozen_primary_plan(query, actual['facts']['request'], item['logical'], store=actual['snapshot'])
        PrimaryWireProjection(actual['snapshot'], actual['facts']['request'],
            lambda logical: frozen_primary_plan(query, actual['facts']['request'], logical, store=actual['snapshot'])[0]
        ).validate(item['logical'], item['payload']['messages'], invocation_key=item['invocation_key'], require_projected=True)
        validate_primary_guards(query, frozen, guards, {})
        for material in selection['origin_materials']:
            owner, source, authority_owner = _origin_reader(query, records, material)
            _validate_primary_body(query, owner, source, material, frozen,
                authority_owner=authority_owner, strict_excerpt=True)
            if material['project_id'] == 'me':
                service = recognition_service(source, source_owner=authority_owner)
                entry = service.get_recognition(scope=WorkScope('local-user', 'me'), recognition_id=material['id'])
                profiles = [value for value in frozen['profile']['items'] if value['id'] == material['id']]
                if (not entry.authorized or len(profiles) != 1 or entry.revision != material['revision']
                        or profiles[0]['content'] != format_recognition_content(entry.retrieval_projection(), profile=True)):
                    raise RecognitionConflict('part_shared_answer_profile_changed')
        personal = selection['personal_context']
        if personal.get('part_context_ref') is not None:
            raise RecognitionConflict('part_shared_answer_nested_context_unavailable')
        if (not isinstance(history.get('turns'), list) or _history_text(history['turns']) != history.get('text', '')
                or frozen.get('history', '') != history.get('text', '')):
            raise RecognitionConflict('part_shared_answer_history_changed')
        expected, histories = {}, {}
        for previous in history['turns']:
            prior = read_model_history_answer(query, previous['id'], previous['revision'], reader=records)
            if previous != {'id': prior['public'].object_id, 'revision': prior['public'].revision,
                    'question': prior['facts']['question'], 'answer': _answer(prior['facts']['answer']['answer'])}:
                raise RecognitionConflict('part_shared_answer_history_changed')
            expected[prior['public'].object_id] = _entry(prior)
            histories[prior['public'].object_id] = prior
        for dependency in history.get('dependencies', []):
            if (not isinstance(dependency, dict) or set(dependency) !=
                    {'turns', 'chosen', 'snapshots', 'preferences', 'multipart', 'clarifications'}):
                raise RecognitionConflict('part_shared_answer_history_changed')
            for previous in dependency['turns']:
                prior = read_model_history_answer(query, previous['id'], previous['revision'], reader=records)
                value = _entry(prior)
                if prior['public'].object_id in expected and expected[prior['public'].object_id] != value:
                    raise RecognitionConflict('part_shared_answer_history_changed')
                expected[prior['public'].object_id] = value
                histories[prior['public'].object_id] = prior
        if personal != {'owner_user_id': query.origin_admission.destination.owner_user_id,
                'namespace_id': query.origin_admission.destination.namespace,
                'history': list(expected.values()), 'part_context_ref': None}:
            raise RecognitionConflict('part_shared_answer_history_changed')
        for prior in histories.values():
            _validate_shared_answer_inputs(query, records, prior, seen, validated)
    validated.add(identity)
    qualification._actor_reader(records)


def _shared_materials(answers):
    materials = []
    for answer in answers:
        for item in answer['inputs']:
            for material in item['material_proof']['selection']['origin_materials']:
                if material not in materials:
                    materials.append(deepcopy(material))
    return materials


def _prepare_shared_context(records, query, project, parent, dependencies, *, recipient=None):
    from ..route_correction_scopes import correction_parent_id
    with _shared_context_reader(records, query) as reader:
        if project != query.origin_admission.lease.project_id:
            raise RecognitionConflict('part_shared_context_scope_changed')
        context = {'scope_kind': 'shared', 'project_id': project, 'parent': parent, 'originals': [], 'answers': [],
            'actor_namespace': query.origin_admission.personal['namespace'], 'shared_answers': [], 'shared_materials': [],
            'privacy_revision': privacy_revision(reader), 'refs': [], 'target': query.ask_target()}
        if correction_parent_id(parent):
            context['recipient'] = recipient
        _validate_parent(reader, context, owner_records=query.execution_owner.records)
        texts = []
        for identity in dependencies:
            row = reader.read('v2_turns', identity)
            if row is None or row.payload.get('project_id') != project:
                raise RecognitionConflict('part_shared_context_scope_changed')
            original_parent = dependency_parent(reader, parent, identity, source_owner=query.execution_owner.records)
            if row.payload.get('intent') != 'ask':
                raise RecognitionConflict('part_shared_context_namespace_unavailable')
            answer = _shared_answer(query, reader, row, original_parent)
            context['shared_answers'].append(answer)
            texts.append(answer['answer'])
            context['refs'].append({'kind': 'session_event', 'object_id': identity,
                'uri': 'crp://' + query.origin_admission.personal['namespace'] + '/v2_turns/' + identity})
        context['shared_materials'] = _shared_materials(context['shared_answers'])
        context['text'] = PREFIX + '\n\n'.join(texts)
        _validate_shared_context(reader, query, context)
        return context


def _validate_shared_context(records, query, context):
    with _shared_context_reader(records, query) as reader:
        from ..route_correction_scopes import correction_parent_id
        fields = {'scope_kind', 'project_id', 'parent', 'originals', 'answers', 'actor_namespace',
            'shared_answers', 'shared_materials', 'privacy_revision', 'refs', 'target', 'text'}
        if correction_parent_id(context.get('parent')):
            fields.add('recipient')
        if (set(context) != fields or context.get('scope_kind') != 'shared'
                or not isinstance(context.get('shared_answers'), list)
                or context.get('project_id') != query.origin_admission.lease.project_id
                or context.get('actor_namespace') != query.origin_admission.personal['namespace']
                or context.get('privacy_revision') != privacy_revision(reader) or context.get('target') != query.ask_target()
                or context.get('originals') != [] or context.get('answers') != []
                or context.get('explicit_sources') or context.get('attachment_titles')):
            raise RecognitionConflict('part_shared_context_changed')
        _validate_parent(reader, context, owner_records=query.execution_owner.records)
        answers, refs, seen = [], [], set()
        for answer in context['shared_answers']:
            if answer['id'] in seen:
                raise RecognitionConflict('part_shared_answer_identity_repeated')
            seen.add(answer['id'])
            row = reader.read('v2_turns', answer['id'])
            if row is None or row.revision != answer['revision']:
                raise RecognitionConflict('part_shared_answer_public_changed')
            parent = dependency_parent(reader, context['parent'], answer['id'], source_owner=query.execution_owner.records)
            current = _shared_answer(query, reader, row, parent)
            if current != answer:
                raise RecognitionConflict('part_shared_answer_authority_changed')
            if context['target']['execution_location'] == 'remote':
                authority = SourceEgressService(query.execution_owner.records, reader=reader)
                for item in answer['inputs']:
                    for snapshot in item['material_proof']['privacy']['source_snapshots']:
                        authority.require(snapshot, 'generation')
            answers.append(current)
            refs.append({'kind': 'session_event', 'object_id': answer['id'],
                'uri': 'crp://' + context['actor_namespace'] + '/v2_turns/' + answer['id']})
        if (context['shared_materials'] != _shared_materials(answers) or context['refs'] != refs
                or context['text'] != PREFIX + '\n\n'.join(answer['answer'] for answer in answers)):
            raise RecognitionConflict('part_shared_context_body_changed')
