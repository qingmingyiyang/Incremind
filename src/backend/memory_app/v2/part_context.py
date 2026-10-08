"""Frozen references to admitted originals and completed answer source facts."""
from copy import deepcopy
from dataclasses import asdict, is_dataclass

from backend.recognition import RecognitionConflict, WorkScope
from ..source_egress import SourceEgressService
from ..part_context_binding import COLLECTION, bind_context
from .privacy import privacy_revision
from .followup import validate_history

PREFIX = '本次输入的依赖上下文（没有资料编号，不要为这些内容生成编号引用）：\n'


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
    if (not request or request['desired_outcome'] != 'project.answer'
            or request['scope']['project_id'] != project or not saved
            or not events or events[-1]['type'] != 'turn.completed'):
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


def prepare_context(records, query, project, parent, dependencies):
    if not dependencies:
        return None
    authority, scope = SourceEgressService(records), WorkScope('local-user', project)
    originals, answers, texts, refs = [], [], [], []
    for identity in dependencies:
        row = records.read('v2_turns', identity)
        if row is None or row.payload.get('project_id') != project or row.payload.get('parent_turn_id') != parent:
            raise RecognitionConflict('part dependency scope changed')
        if row.payload['intent'] == 'remember':
            item = row.payload['receipt']['remember']['item_id']
            span = row.payload['user_text']
            source = records.read('workspace_items', item)
            binding = None
            if source.payload['input_kind'] != 'text':
                parent_row = records.read('v2_turns', parent)
                parent_text = parent_row.payload['user_text']
                start = parent_text.index(span)
                binding = {'child_id':identity, 'parent_id':parent, 'parent_text':parent_text,
                           'start':start, 'end':start + len(span)}
            originals.append(authority.snapshot_original_content(scope, item, span=span, input_binding=binding))
            texts.append(span)
            refs.append({'kind':'source', 'object_id':item, 'uri':f'crp://default/workspace/{item}'})
        elif row.payload['intent'] == 'ask':
            facts = _answer_facts(query, project, identity, parent)
            answers.append({'id':identity, 'revision':row.revision,
                'question':row.payload['user_text'], 'answer':row.payload['receipt']['ask']['answer'],
                'authority':facts})
            texts.append(row.payload['receipt']['ask']['answer'])
            refs.append({'kind':'session_event', 'object_id':identity, 'uri':f'crp://default/v2_turns/{identity}'})
        else:
            raise RecognitionConflict('part dependency intent changed')
    context = {'project_id':project, 'parent':parent, 'originals':originals, 'answers':answers,
               'privacy_revision':privacy_revision(records), 'refs':refs,
               'text':PREFIX + '\n\n'.join(texts), 'target':query.ask_target()}
    validate_context(records, query, context)
    return context


def validate_context(records, query, context):
    if privacy_revision(records) != context['privacy_revision']:
        raise RecognitionConflict('part dependency privacy changed')
    project = context['project_id']
    authority = SourceEgressService(records)
    for original in context['originals']:
        authority.validate_original_content(WorkScope('local-user', project), original, 'generation')
    for answer in context['answers']:
        row = records.read('v2_turns', answer['id'])
        if (row is None or row.revision != answer['revision'] or row.payload.get('project_id') != project
                or row.payload.get('parent_turn_id') != context['parent']
                or row.payload['user_text'] != answer['question']
                or row.payload['receipt']['ask']['answer'] != answer['answer']):
            raise RecognitionConflict('part answer binding changed')
        facts = _answer_facts(query, project, answer['id'], context['parent'])
        if facts != answer['authority']:
            raise RecognitionConflict('part answer authority changed')
        _validate_answer_facts(query, facts)


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
    validate_context(records, query, row.payload['context'])
    return row.payload['context']
