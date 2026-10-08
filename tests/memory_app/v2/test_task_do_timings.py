"""Exercise product TaskDo timing through the existing real-kernel scenario."""
from threading import Lock, current_thread
from time import monotonic, sleep

from core.storage_provider.observability import current_observation
from core.storage_provider.connection_scope import _SCOPE
from tests.memory_app.v2.test_workbench_do import env
from tests.memory_app.v2.test_divided_do import _real_kernel_drafts


def test_real_task_do_propagates_product_timing_to_main_and_children(env, monkeypatch):
    client, model = env
    records = client.app.state.recognition_service.records
    original = model._completion_fn
    observations = []
    units = []
    guard = Lock()

    def observed_provider(**request):
        observation = current_observation()
        assert observation is not None, 'real model worker lost the product observation'
        before = observation.statements
        # A real read at the transport seam proves SQL from this worker is counted.
        records.list('synthetic_timing_probe')
        assert observation.statements > before
        unit = _SCOPE.get()
        assert unit is not None, 'real model worker lost the product unit of work'
        with guard:
            observations.append((observation.turn_id, current_thread().name))
            units.append(unit)
        return original(**request)

    monkeypatch.setattr(model, '_completion_fn', observed_provider)
    # Reuses the existing fake transport responses, real organization, three
    # real child drafts and product terminal assertions without duplicating them.
    _real_kernel_drafts(env)
    turns = records.list('v2_turns')
    assert len(turns) == 1
    identity = turns[0].object_id
    assert observations and all(turn_id == identity for turn_id, _ in observations)
    assert any(name.startswith('ai-turn_') for _, name in observations)
    assert any(name.startswith('ai-turn-child_') for _, name in observations)
    assert all(unit is units[0] for unit in units)
    deadline = monotonic() + 10
    timing = None
    while monotonic() < deadline:
        timing = records.read('v2_turn_timings', identity)
        if timing is not None:
            break
        sleep(.01)
    assert timing is not None, 'worker/callback observation did not finish'
    assert timing.payload['operation'] == 'task'
    assert timing.payload['connection_count'] > 0
    assert timing.payload['statement_count'] >= timing.payload['connection_count']
    assert turns[0].payload['receipt']['do']['state'] == 'done'
