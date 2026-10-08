"""真实产品 planner 不将精确外部执行任务转成模型分工。"""
from copy import deepcopy

import pytest

from backend.api.model_routing_snapshot_authority import TurnModelRoutingSnapshotAuthority
from backend.memory_app.kernel.task_planner import ProductTaskPlanner
from backend.memory_app.turn_routing import SNAPSHOT_KIND
from core.ai_kernel import SQLiteAITurnStore, validate_turn_request
from tests.rebuild.test_external_task_template import external_request
from tests.rebuild.test_product_turn_kinds import request
from tests.memory_app.test_final_task_draft_authority import authority


class Models:
    """只隔离外部模型配置和传输，不替换 planner 或 routing authority。"""
    def __init__(self):
        self.reads = self.calls = 0

    def public(self):
        self.reads += 1
        return {'generation':{}}

    def complete_governed(self,*args,**kwargs):
        self.calls += 1
        raise AssertionError('external task must not call a model')


def planner(store=None,composition=None,models=None):
    return ProductTaskPlanner(models=models,store=store,composition=composition,
        guard=None,builder=None,fallback=None)


def test_valid_external_task_is_not_a_product_model_task():
    value = external_request()
    assert validate_turn_request(value) == value
    assert planner().handles(value) is False


def test_original_routing_authority_does_not_freeze_model_or_division_for_external_task(tmp_path):
    store = SQLiteAITurnStore(tmp_path/'turns.sqlite3')
    models = Models()
    product = planner(store=store,models=models)
    routing = TurnModelRoutingSnapshotAuthority(object(),store,task_routing=product)
    value = external_request()
    assert validate_turn_request(value) == value
    store.claim_turn(value)
    route = routing.acquire(value,project_id=value['scope']['project_id'],
        project_profile_id='synthetic-profile',project_profile_revision=1,
        boundary_profile_id='synthetic-boundary',boundary_profile_revision=1,
        capability_ids=['external.task.execute'],skill_snapshot_revision=None)
    assert route is None
    assert models.reads == models.calls == 0
    assert store.get_immutable_payload(value['turn_id'],SNAPSHOT_KIND) is None
    assert store.get_immutable_payload(value['turn_id'],routing.snapshot_kind) is None
    assert store.events_after(value['turn_id']) == ()


@pytest.mark.parametrize('field,value', [('version',1),('version',True),('version',2.0),
    ('version',None),('policy',None),('cap',None),('capability_id','other.capability'),
    ('mode','other_mode')])
def test_only_the_exact_new_header_is_excluded(field,value):
    candidate = external_request()
    if field == 'version': candidate['execution_policy']['template_version'] = value
    elif field == 'policy': candidate['execution_policy'] = value
    elif field == 'cap': candidate['capability_request'] = value
    else: candidate['capability_request'][field] = value
    assert planner().handles(candidate) is True


def test_old_task_and_answer_classification_remain_original():
    task = request('project.task')
    before = deepcopy(task)
    assert planner().handles(task) is True
    assert task == before
    assert planner().handles(request('project.answer')) is False
    assert planner().handles(request('project.answer',template_version=2)) is False


def test_real_original_steward_and_profile_ancestry_are_unchanged(authority):
    composition,store,worker,_drafts,_provider = authority
    product = planner(store=store,composition=composition)
    steward = next(value for run in composition.store.list_runs(project_id='project-alpha')
        if (value := composition.request_loader(run.turn_id))['desired_outcome'] == 'agent.steward.plan')
    assert steward['desired_outcome'] == 'agent.steward.plan'
    assert product.handles(steward) is True
    root = product.profile_request(worker)
    assert root['desired_outcome'] == 'project.task'
    assert product.handles(root) is True
