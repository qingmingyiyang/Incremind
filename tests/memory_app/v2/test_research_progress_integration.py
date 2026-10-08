"""Real coordinator/frozen-store integration and HTTP failure recovery."""
from types import SimpleNamespace

import pytest

from backend.memory_app.v2 import _LazyOrganization
from backend.memory_app.v2.task_do import TaskDo
from tests.memory_app.v2.research_fixture import research_request
from tests.backend.unit.api.test_agent_coordinator import _durable_coordinator
from tests.memory_app.v2.test_workbench_do import env
from tests.memory_app.v2.test_workbench_do_agents import Organization, install


def real_organization(tmp_path):
    turns, store, runtime, runner, profiles, coordinator = _durable_coordinator(tmp_path)
    from backend.api.agent_organization_runtime import AgentOrganizationRuntime
    from core.ai_kernel.agent_dispatch_store import SQLiteAgentDispatchStore
    organization_runtime = AgentOrganizationRuntime(coordinator=coordinator,
        dispatch_store=SQLiteAgentDispatchStore(tmp_path / 'ai-turns.sqlite3'),
        run_store=store, request_loader=turns.get_request)
    original = research_request('receipt-a', 'project-a', '研究')
    organization_runtime.start(original, agent_turn_mode=True)
    frozen = turns.get_request(original['turn_id'])
    state = SimpleNamespace(
        agent_organization_runtime=organization_runtime, ai_turn_store=turns,
        ai_runtime=SimpleNamespace(receipt_for=lambda identity: SimpleNamespace(status='running', current_sequence=0),
                                   events_after=turns.events_after),
        agent_runtime_composition=SimpleNamespace(coordinator=coordinator, profiles=profiles,
                                                   request_loader=turns.get_request))
    return _LazyOrganization(SimpleNamespace(state=state)), frozen, coordinator


@pytest.mark.parametrize('method', ['topology', 'usage'])
def test_real_coordinator_progress_reads_frozen_scope_and_privacy(tmp_path, method):
    organization, request, coordinator = real_organization(tmp_path)
    # Verify the real coordinator rejects the original omitted arguments.
    from backend.api.agent_coordinator import AgentCoordinatorError
    with pytest.raises(AgentCoordinatorError, match='scope drifted'):
        coordinator.list(parent_turn_id=request['turn_id'], project_id='project-a')
    result = (organization.usage(request['turn_id'], 'project-a', descendants=True)
              if method == 'usage' else organization.topology(request['turn_id'], 'project-a'))
    assert result == []
    assert organization.read(request['turn_id'], 'project-a')['status'] == 'running'


def test_progress_rejects_wrong_project_before_coordinator(tmp_path):
    organization, request, _ = real_organization(tmp_path)
    with pytest.raises(RuntimeError, match='research_scope_changed'):
        organization.topology(request['turn_id'], 'other')


@pytest.mark.parametrize('boundary', ['read', 'topology'])
def test_repeated_progress_failure_persists_failed_receipt_and_thread_remains_200(env, boundary):
    client, models = env
    org = Organization()
    def unavailable(*args):
        raise RuntimeError('private diagnostic must not reach receipt')
    setattr(org, boundary, unavailable)
    install(client, org)
    records = client.app.state.recognition_records
    research = TaskDo(records, models, client.app.state.task_drafts, org, org.read, org.topology)
    receipt, state = research.initial('receipt-a', 'project-a', '研究', None)
    receipt['experts'] = [{'role': '研究员', 'state': 'running'}]
    with records.begin() as tx:
        tx.put('v2_threads', 'thread-a', {'project_id':'project-a', 'title':'研究', 'updated_at':'2026-10-02'}, expected_revision=0)
        tx.put('v2_turns', 'receipt-a', {'project_id':'project-a', 'thread_id':'thread-a', 'intent':'do',
               'user_text':'研究', 'created_at':'2026-10-02', 'receipt':{'do':receipt}}, expected_revision=0)
        tx.put('v2_task_executions', 'receipt-a', state, expected_revision=0)
        tx.commit()
    for _ in range(3):
        client.portal.call(research.advance, 'receipt-a')
        response = client.get('/api/v2/workbench/threads/thread-a?project_id=project-a')
        assert response.status_code == 200, response.text
    saved = records.read('v2_turns', 'receipt-a').payload['receipt']['do']
    assert saved['state'] == 'failed'
    assert saved['error'] == 'task_progress_failed'
    assert all(row['state'] == 'failed' for row in saved['experts'])
    assert 'private diagnostic' not in str(saved)
    revision = records.read('v2_task_executions', 'receipt-a').revision
    for _ in range(2):
        response = client.get('/api/v2/workbench/threads/thread-a?project_id=project-a')
        assert response.status_code == 200, response.text
        assert response.json()['turns'][0]['receipt']['do']['state'] == 'failed'
    assert records.read('v2_task_executions', 'receipt-a').revision == revision
    assert not records.list('recognition_tasks')
    assert not models.calls


def test_thread_progress_with_real_coordinator_stays_200(env, tmp_path):
    client, models = env
    org, request, _ = real_organization(tmp_path)
    install(client, org)
    records = client.app.state.recognition_records
    research = TaskDo(records, models, client.app.state.task_drafts, org, org.read, org.topology)
    receipt, state = research.initial('real-receipt', 'project-a', '研究', None)
    receipt['kernel_turn_id'] = request['turn_id']
    state.update(request=request, started=True)
    with records.begin() as tx:
        tx.put('v2_threads', 'real-thread', {'project_id':'project-a', 'title':'研究', 'updated_at':'2026-10-02'}, expected_revision=0)
        tx.put('v2_turns', 'real-receipt', {'project_id':'project-a', 'thread_id':'real-thread', 'intent':'do',
               'user_text':'研究', 'created_at':'2026-10-02', 'receipt':{'do':receipt}}, expected_revision=0)
        tx.put('v2_task_executions', 'real-receipt', state, expected_revision=0)
        tx.commit()
    response = client.get('/api/v2/workbench/threads/real-thread?project_id=project-a')
    assert response.status_code == 200, response.text
    saved = response.json()['turns'][0]['receipt']['do']
    assert saved['state'] == 'running'
    assert 'error_code' not in saved
    assert not models.calls


@pytest.mark.asyncio
async def test_successful_read_resets_consecutive_failure_budget(tmp_path):
    from core.storage_provider import SQLiteStructuredRecordStore
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    org = Organization()
    org.calls.append({})
    healthy = org.read
    def unavailable(*args):
        raise RuntimeError('unavailable')
    research = TaskDo(records, None, None, org, unavailable, org.topology)
    receipt, state = (
        {'task_id':None, 'state':'running', 'experts':[], 'kernel_turn_id':'research-a'},
        {'project_id':'project-a', 'request':{'turn_id':'research-a'}, 'started':True})
    with records.begin() as tx:
        tx.put('v2_turns','reset-a', {'receipt':{'do':receipt}},expected_revision=0)
        tx.put('v2_task_executions','reset-a',state,expected_revision=0)
        tx.commit()
    for _ in range(2): await research.advance('reset-a')
    research.reader = healthy
    await research.advance('reset-a')
    research.reader = unavailable
    for _ in range(2): await research.advance('reset-a')
    assert records.read('v2_turns','reset-a').payload['receipt']['do']['state'] == 'running'
    await research.advance('reset-a')
    assert records.read('v2_turns','reset-a').payload['receipt']['do']['state'] == 'failed'


def test_real_coordinator_still_rejects_privacy_drift(tmp_path):
    organization, request, coordinator = real_organization(tmp_path)
    from backend.api.agent_coordinator import AgentCoordinatorError
    with pytest.raises(AgentCoordinatorError, match='privacy drifted'):
        coordinator.list(parent_turn_id=request['turn_id'], project_id='project-a',
                         scope=request['scope'], privacy={**request['privacy'], 'allow_remote':False})
