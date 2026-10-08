from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from threading import Event, Lock, Thread
from time import monotonic, sleep
from uuid import uuid5, NAMESPACE_URL

import pytest

from backend.api.ai_turn_runner import AITurnRunner
from backend.api.agent_capabilities import AgentCapabilityProvider, agent_capability_definition
from backend.api.agent_organization_runtime import AgentOrganizationRuntime
from backend.api.agent_runtime_composition import build_agent_runtime_composition
from backend.api.capability_admission import ReviewedCoreCapabilityRegistry, RuntimeCapabilityAdmission
from core.ai_kernel import ScopedCapabilityRegistry, SQLiteAITurnStore, SynchronousAIRuntime
from core.ai_kernel.dispatcher import SynchronousToolDispatcher, ToolDispatchRequest
from core.ai_kernel.capability_manifest import V1TurnPolicyCapabilityManifestResolver
from core.ai_tooling import tool_from_capability
from tests.backend.unit.api.test_agent_organization_e2e import _request, _cluster_proposal


class _Planner:
    def __init__(self):
        self.main_release = Event()
        self.steward_release = Event()
        self.experts_started = Event()
        self.intervals = {}
        self.entered = []
        self.lock = Lock()

    def plan(self, request, _events, _capabilities, _payloads, control):
        binding = request['agent_binding']
        self.entered.append(binding['profile_id'])
        if binding['role'] == 'main':
            while not self.main_release.wait(.01):
                control.checkpoint()
        elif binding['profile_id'] == 'steward.scheduler':
            self.steward_release.wait(5)
        else:
            started = monotonic()
            with self.lock:
                self.intervals[request['turn_id']] = [started, None]
                self.experts_started.set()
            deadline = started + 2
            while monotonic() < deadline:
                control.checkpoint()
                sleep(.01)
            with self.lock:
                self.intervals[request['turn_id']][1] = monotonic()
        return {'type': 'complete', 'summary': 'Verified synthetic conclusion', 'evidence_refs': []}


def _organization(tmp_path, *, max_workers=2, max_child_workers=4):
    store = SQLiteAITurnStore(tmp_path / '.rebuild-data' / 'ai-turns.sqlite3')
    registry = ScopedCapabilityRegistry()
    composition = build_agent_runtime_composition(
        runtime_root=tmp_path, session_store=store,
        registry=ReviewedCoreCapabilityRegistry(RuntimeCapabilityAdmission(registry)),
    )
    planner = _Planner()
    class ManifestResolver:
        def resolve(self, request, capabilities):
            manifest = V1TurnPolicyCapabilityManifestResolver().resolve(request, capabilities)
            ref = store.get_or_create_immutable_payload(request['turn_id'], 'synthetic-local-route', {'model': 'fake-local-planner'})
            return replace(manifest, model_routing_snapshot_ref=ref, model_routing_snapshot_revision='synthetic-v1')
    runtime = SynchronousAIRuntime(planner=planner, registry=registry, events=store, payloads=store,
        state=store, manifest_resolver=ManifestResolver())
    runner = AITurnRunner(runtime, max_workers=max_workers, max_child_workers=max_child_workers)
    composition.bind_runtime(runtime)
    composition.bind_runner(runner)
    organization = AgentOrganizationRuntime(coordinator=composition.coordinator,
        dispatch_store=composition.dispatch_store, run_store=composition.store,
        request_loader=composition.request_loader)
    composition.bind_organization_runtime(organization)
    return composition, organization, planner, runner


def _start(composition, organization, suffix):
    request = _request(suffix=suffix)
    request['turn_id'] = 'turn-' + uuid5(NAMESPACE_URL, 'parallel-test:' + suffix).hex
    request['operation_id'] = 'op-parallel-main-' + suffix
    started = organization.start(request, agent_turn_mode=True)
    steward_request = composition.request_loader(started['steward']['turn_id'])
    proposal = _cluster_proposal()
    proposal['plan_id'] += suffix
    proposal['cluster_id'] += suffix
    proposal['assignments'][0]['assignment_id'] += suffix
    proposal['assignments'][0]['budget']['wall_time_ms'] = 5000
    second = {**proposal['assignments'][0], 'assignment_id': 'second-' + suffix}
    proposal['assignments'].append(second)
    return started, steward_request, proposal


def _plan(composition, request, proposal):
    return composition.coordinator.plan(parent_turn_id=request['turn_id'],
        operation_id='op-test-plan-' + proposal['plan_id'], project_id='project-alpha',
        scope=request['scope'], privacy=request['privacy'], arguments=proposal)


@pytest.mark.parametrize('organizations', [1, 2])
def test_experts_overlap_and_parent_wait_does_not_starve_children(tmp_path, organizations):
    composition, organization, planner, runner = _organization(tmp_path)
    try:
        starts = [_start(composition, organization, str(i)) for i in range(organizations)]
        for _, request, proposal in starts:
            _plan(composition, request, proposal)
        planner.steward_release.set()
        deadline = monotonic() + 10
        while monotonic() < deadline:
            if len(planner.intervals) == organizations * 2 and all(v[1] for v in planner.intervals.values()):
                break
            sleep(.02)
        intervals = list(planner.intervals.values())
        assert len(intervals) == organizations * 2, planner.entered
        assert all(v[1] is not None for v in intervals), intervals
        assert max(v[0] for v in intervals) < min(v[1] for v in intervals), intervals
        for turn_id in planner.intervals:
            assert runner.wait_for_terminal(turn_id, timeout_seconds=3).status == 'completed'
        for started, request, _ in starts:
            children = composition.store.list_runs(project_id='project-alpha',
                parent_run_id=started['main']['run_id'])
            assert sum(child.status == 'completed' for child in children if child.profile_id != 'steward.scheduler') == 2
    finally:
        planner.main_release.set()
        planner.steward_release.set()
        runner.shutdown(timeout_seconds=5)


class _Observer:
    def claimed(self): pass
    def started(self): pass
    @contextmanager
    def fence(self): yield


def _dispatch(dispatcher, capability, request, arguments):
    tool = tool_from_capability(agent_capability_definition(capability))
    payload = {'turn_id': request['turn_id'], 'operation_id': 'op-parallel-' + capability.replace('.', '-'),
        'tool_call_id': 'call-parallel-' + capability.replace('.', '-'), 'capability_id': capability,
        'scope': request['scope'], 'privacy': request['privacy'], 'arguments': arguments}
    return dispatcher.dispatch(AgentCapabilityProvider(coordinator=dispatcher.coordinator, capability_id=capability),
        ToolDispatchRequest(payload, tool.execution_mode, tool.resource_locks, payload['tool_call_id'], 1, 5000), _Observer())


def test_agent_plan_finishes_within_one_second_while_main_waits(tmp_path):
    composition, organization, planner, runner = _organization(tmp_path)
    dispatcher = SynchronousToolDispatcher()
    dispatcher.coordinator = composition.coordinator
    started, steward_request, proposal = _start(composition, organization, 'gate')
    main_request = composition.request_loader(started['main']['turn_id'])
    # Observe entry into the actual coordinator wait through the real runner's
    # durable wait registration, rather than timing a guessed thread start.
    results, errors = [], []
    def wait_main():
        try:
            results.append(_dispatch(dispatcher, 'agent.wait', main_request,
                {'child_run_ids': [started['steward']['run_id']], 'timeout_ms': 3000}))
        except Exception as error:
            errors.append(error)
    waiter = Thread(target=wait_main)
    try:
        waiter.start()
        deadline = monotonic() + 1
        while started['steward']['turn_id'] not in runner._terminal_wakeups and monotonic() < deadline:
            sleep(.005)
        assert started['steward']['turn_id'] in runner._terminal_wakeups, errors
        began = monotonic()
        result = _dispatch(dispatcher, 'agent.plan', steward_request, proposal)
        elapsed = monotonic() - began
        assert result['summary']
        assert elapsed < 1
        assert waiter.is_alive()
    finally:
        planner.main_release.set()
        planner.steward_release.set()
        waiter.join(4)
        runner.shutdown(timeout_seconds=5)
    assert not errors


def test_cancelling_parent_reaches_experts_in_child_pool(tmp_path):
    composition, organization, planner, runner = _organization(tmp_path)
    try:
        started, request, proposal = _start(composition, organization, 'cancel')
        _plan(composition, request, proposal)
        planner.steward_release.set()
        deadline = monotonic() + 5
        while len(planner.intervals) < 2 and monotonic() < deadline:
            sleep(.01)
        assert len(planner.intervals) == 2
        assert runner.request_turn_cancel(started['main']['turn_id'], reason='parent cancelled')
        receipts = [runner.wait_for_terminal(turn_id, timeout_seconds=4) for turn_id in planner.intervals]
        assert [receipt.status for receipt in receipts] == ['cancelled', 'cancelled']
    finally:
        planner.main_release.set()
        planner.steward_release.set()
        runner.shutdown(timeout_seconds=5)


def test_parent_cancel_converges_queued_child_before_its_model_starts(tmp_path):
    composition, organization, planner, runner = _organization(tmp_path, max_child_workers=1)
    try:
        started, request, proposal = _start(composition, organization, 'queued-cancel')
        _plan(composition, request, proposal)
        planner.steward_release.set()
        assert planner.experts_started.wait(5)
        children = composition.store.list_runs(project_id='project-alpha', parent_run_id=started['main']['run_id'])
        experts = [child for child in children if child.profile_id != 'steward.scheduler']
        assert len(experts) == 2
        assert runner.request_turn_cancel(started['main']['turn_id'], reason='cancel queued children')
        receipts = [runner.wait_for_terminal(child.turn_id, timeout_seconds=4) for child in experts]
        assert [receipt.status for receipt in receipts] == ['cancelled', 'cancelled']
        assert len(planner.intervals) == 1
    finally:
        planner.main_release.set()
        planner.steward_release.set()
        runner.shutdown(timeout_seconds=5)
