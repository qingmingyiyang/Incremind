from copy import deepcopy
from dataclasses import replace

import pytest

from backend.memory_app.kernel.task_division_authority import frozen_division_capabilities
from backend.security.ai_tool_execution_boundary import AIToolExecutionBoundary
from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from tests.backend.unit.security.test_task_draft_boundary import draft
from tests.backend.unit.api.test_agent_organization_e2e import (
    _organization, _request, _cluster_proposal, _converge_child,
)


def _frozen_worker(tmp_path, capabilities):
    composition, _, organization = _organization(tmp_path)
    request = _request(suffix='draft-authority')
    request['desired_outcome'] = 'project.task'
    request['capability_policy']['allowed'].append('document.draft.propose')
    started = organization.start(request, agent_turn_mode=True)
    steward = composition.request_loader(started['steward']['turn_id'])
    proposal = _cluster_proposal()
    proposal['assignments'][0].update(profile_id='subagent.worker',
        capability_ids=capabilities)
    composition.coordinator.plan(parent_turn_id=steward['turn_id'],
        operation_id='op-task-draft-plan', project_id='project-alpha',
        scope=steward['scope'], privacy=steward['privacy'], arguments=proposal)
    _converge_child(composition, started['steward']['run_id'])
    organization.on_terminal(steward['turn_id'])
    worker = next(run for run in composition.store.list_runs(project_id='project-alpha',
        parent_run_id=started['main']['run_id']) if run.profile_id == 'subagent.worker')
    frozen = composition.request_loader(worker.turn_id)
    return composition, started, frozen


def test_real_frozen_permit_controls_production_boundary(tmp_path):
    composition, started, frozen = _frozen_worker(tmp_path, ['document.draft.propose'])
    query = lambda value: frozen_division_capabilities(composition, value)
    assert query(frozen) == ('document.draft.propose',)
    assert query(composition.request_loader(started['main']['turn_id'])) == ()
    drifted = deepcopy(frozen)
    drifted['capability_policy']['allowed'].append('memory.candidate.propose.write')
    assert query(drifted) == ()
    boundary = AIToolExecutionBoundary(ProjectBoundaryProfileStore(tmp_path), division_capabilities=query)
    assert boundary.evaluate(frozen, draft(), {'arguments':{}}).outcome == 'allow'


@pytest.mark.parametrize('capabilities', [
    ['memory.recall', 'document.draft.propose'],
    ['document.draft.propose', 'memory.recall'],
])
def test_mixed_read_and_draft_permit_is_independent_of_capability_order(tmp_path, capabilities):
    composition, _, frozen = _frozen_worker(tmp_path, capabilities)
    query = lambda value: frozen_division_capabilities(composition, value)
    assert set(query(frozen)) == {'memory.recall', 'document.draft.propose'}
    boundary = AIToolExecutionBoundary(ProjectBoundaryProfileStore(tmp_path), division_capabilities=query)
    assert boundary.evaluate(frozen, draft(), {'arguments':{}}).outcome == 'allow'


@pytest.mark.parametrize('capabilities', [
    ('document.draft.propose',),
    ('memory.recall', 'document.draft.propose', 'source.evidence.read'),
])
def test_assignment_with_missing_or_extra_capability_cannot_authorize_draft(tmp_path, monkeypatch, capabilities):
    composition, _, frozen = _frozen_worker(tmp_path, ['document.draft.propose', 'memory.recall'])
    get_assignment = composition.dispatch_store.get_assignment

    def inconsistent_assignment(*args, **kwargs):
        assignment = get_assignment(*args, **kwargs)
        return replace(assignment, capability_ids=capabilities) if assignment else None

    monkeypatch.setattr(composition.dispatch_store, 'get_assignment', inconsistent_assignment)
    query = lambda value: frozen_division_capabilities(composition, value)
    assert query(frozen) == ()
    boundary = AIToolExecutionBoundary(ProjectBoundaryProfileStore(tmp_path), division_capabilities=query)
    assert boundary.evaluate(frozen, draft(), {'arguments':{}}).outcome != 'allow'
