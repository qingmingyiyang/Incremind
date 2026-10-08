from copy import deepcopy

from tests.backend.unit.api.test_agent_organization_e2e import (
    _organization, _request, _cluster_proposal, _converge_child,
)


def test_two_parallel_items_then_dependent_item_use_real_permits(tmp_path):
    composition, runner, organization = _organization(tmp_path)
    request = _request(suffix='task-dependencies')
    request['desired_outcome'] = 'project.task'
    started = organization.start(request, agent_turn_mode=True)
    steward = composition.request_loader(started['steward']['turn_id'])
    proposal = _cluster_proposal()
    first = proposal['assignments'][0]
    proposal['assignments'] = []
    for index in range(3):
        item = deepcopy(first)
        item['assignment_id'] = f'item-{index}'
        item['task'] = f'Complete part {index}'
        item['division'] = {'goal':item['task'], 'deliverable':f'Draft {index}',
                            'depends_on':['item-0', 'item-1'] if index == 2 else []}
        proposal['assignments'].append(item)
    composition.coordinator.plan(parent_turn_id=steward['turn_id'],
        operation_id='op-dependencies', project_id='project-alpha',
        scope=steward['scope'], privacy=steward['privacy'], arguments=proposal)
    _converge_child(composition, started['steward']['run_id'])
    organization.on_terminal(steward['turn_id'])
    def workers():
        return [run for run in composition.store.list_runs(project_id='project-alpha',
            parent_run_id=started['main']['run_id']) if run.profile_id != 'steward.scheduler']
    initial = workers()
    assert len(initial) == 2
    for index, run in enumerate(initial):
        _converge_child(composition, run.run_id)
        organization.on_terminal(run.turn_id)
        assert len(workers()) == (2 if index == 0 else 3)
    for run in initial:
        assert runner.submitted.count(run.turn_id) == 1
    last = next(run for run in workers() if run.run_id not in {item.run_id for item in initial})
    text = composition.request_loader(last.turn_id)['input']['text']
    assert '主智能体转交的依赖结果' in text
    assert all(run.run_id in text for run in initial)
