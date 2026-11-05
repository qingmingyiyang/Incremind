from copy import deepcopy
from functools import partial
from inspect import getclosurevars
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from backend.recognition import WorkScope, RecognitionError
from backend.memory_app.source_egress import _part_dependency_sources
from backend.memory_app.v2.multipart import execute_parts
from backend.memory_app.v2.part_context import prepare_context, validate_context, validate_bound_context, bind_context
from backend.memory_app.v2.route import RoutePart, RoutePlan
from tools.rag_holdout import build_application, HttpObserver, CompletionObserver


@pytest.fixture(scope='module')
def completed_answer():
    root = Path('D:/CAQ-O1-20261025') / ('proof-' + uuid4().hex[:8])
    root.mkdir()
    output = root / 'evidence'
    output.mkdir()
    with HttpObserver(output, 'https://api.deepseek.com', 0, paid=False):
        app = build_application(root, {'generation': {'provider': 'openai', 'base_url': 'https://api.deepseek.com',
            'model': 'deepseek-chat', 'api_key': 'unused-no-wire', 'enabled': True, 'allow_remote': True}},
            None, root / 'models', CompletionObserver(output, 0))
        records = app.state.recognition_records
        prepared = getclosurevars(app.state.workbench_turn_execution.execute).nonlocals['execute_turn_prepared']
        execute = getclosurevars(prepared).nonlocals['execute_routed_part']
        with TestClient(app) as client:
            project = client.post('/api/v2/projects', json={'name': '公开空项目'}).json()['id']
            result = client.portal.call(partial(execute_parts, {'text': '有哪些已确认资料？'},
                RoutePlan(mode='manual', parts=[RoutePart(intent='ask', span='有哪些已确认资料？')]),
                records=records, instance=app.state.workbench_instance, execute=execute, project=project,
                scene=None, request_key=None, complete=app.state.workbench_turn_execution.complete))
            parent = records.read('v2_turns', result['turn']['id'])
            identity = parent.payload['part_turn_ids'][0]
            assert result['turn']['receipt']['parts'][0]['state'] == 'done', result['turn']['receipt']['parts']
            query = app.state.workspace_domains.query
            context = prepare_context(records, query, project, parent.object_id, [identity])
            yield records, WorkScope('local-user', project), context, query


@pytest.mark.parametrize('transactional', [False, True])
def test_completed_no_wire_answer_has_real_immutable_authority(completed_answer, transactional):
    records, scope, context, _ = completed_answer
    from backend.memory_app.transaction_records import TransactionRecords
    with records.begin() as reader:
        owner = TransactionRecords(reader) if transactional else records
        originals, snapshots = _part_dependency_sources(owner, reader, scope, context)
    assert originals == [] and snapshots == []


@pytest.mark.parametrize('field', ['question', 'answer', 'authority', 'parent'])
def test_public_or_context_changes_cannot_replace_completed_answer(completed_answer, field):
    records, scope, original, _ = completed_answer
    context = deepcopy(original)
    answer = context['answers'][0]
    if field in {'question', 'answer'}:
        answer[field] = '篡改内容'
    elif field == 'authority':
        answer['authority']['turn_id'] = 'forged'
    else:
        context['parent'] = 'forged-parent'
    with records.begin() as reader, pytest.raises(RecognitionError):
        _part_dependency_sources(records, reader, scope, context)


@pytest.mark.parametrize('field', ['question', 'answer'])
def test_public_answer_revision_cannot_replace_frozen_question_or_result(completed_answer, field):
    records, scope, original, _ = completed_answer
    context = deepcopy(original)
    answer = context['answers'][0]
    row = records.read('v2_turns', answer['id'])
    payload = deepcopy(row.payload)
    if field == 'question':
        payload['user_text'] = answer[field] = '篡改公开问题'
    else:
        payload['receipt']['ask']['answer'] = answer[field] = '篡改公开回答'
    with records.begin() as writer:
        changed = writer.put('v2_turns', row.object_id, payload, expected_revision=row.revision)
        writer.commit()
    answer['revision'] = changed.revision
    try:
        with records.begin() as reader, pytest.raises(RecognitionError):
            _part_dependency_sources(records, reader, scope, context)
    finally:
        with records.begin() as writer:
            restored = writer.put('v2_turns', row.object_id, row.payload, expected_revision=changed.revision)
            writer.commit()
        original['answers'][0]['revision'] = restored.revision


@pytest.mark.parametrize('change', ['project', 'intent', 'membership'])
def test_context_rechecks_real_multi_parent_before_sending(completed_answer, change):
    records, _, context, query = completed_answer
    row = records.read('v2_turns', context['parent'])
    payload = deepcopy(row.payload)
    payload.update({'project_id': 'other'} if change == 'project' else
        {'intent': 'remember'} if change == 'intent' else {'part_turn_ids': []})
    with records.begin() as writer:
        changed = writer.put('v2_turns', row.object_id, payload, expected_revision=row.revision)
        writer.commit()
    try:
        with pytest.raises(RecognitionError):
            validate_context(records, query, context)
    finally:
        with records.begin() as writer:
            writer.put('v2_turns', row.object_id, row.payload, expected_revision=changed.revision)
            writer.commit()


@pytest.mark.parametrize('kind', ['project.answer', 'project.task'])
def test_bound_input_rejects_another_recipient_from_frozen_session(completed_answer, kind):
    records, scope, context, query = completed_answer
    identity = 'unrelated-' + uuid4().hex
    request = {'turn_id': identity, 'session_id': 'session-unrelated-child', 'desired_outcome': kind,
               'scope': {'project_id': scope.project_id}, 'input': {'refs': []}}
    bind_context(records, request, context)
    with pytest.raises(RecognitionError):
        validate_bound_context(records, query.models, request, query)
