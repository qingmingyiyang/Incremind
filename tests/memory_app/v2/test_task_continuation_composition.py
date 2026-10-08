"""The real product factory binds existing read and frame owners explicitly."""
from backend.memory_app import privacy_state, research_sources
from backend.memory_app.research_sources import ReadControl
from backend.memory_app.v2.turn_frames import TurnFrames
from tests.memory_app.v2.test_task_continuation_rejections import closed_main
from tests.memory_app.v2.test_workbench_do import env


def test_product_composition_injects_exact_read_control_and_frame_owners(env):
    state = closed_main(env)
    app_state = state.client.app.state
    planner = state.runtime.task_continuations.planner
    assert app_state.product_task_read_control_type is ReadControl
    assert app_state.product_task_frame_factory is TurnFrames
    assert planner.continuations.read_control_type is ReadControl
    assert planner.frame_factory is TurnFrames
    assert planner.continuations.records is state.service.records
    assert planner.continuations.planner is planner
    assert planner.continuations.paused(state.identity, 'project-a') == state.binding
    assert [role for role, _ in state.calls].count('main') == 1 and state.closed == ['main']


def test_read_boundary_keeps_the_existing_canonical_project_privacy_function(env):
    from backend.memory_app.v2.privacy import set_private_project
    records = env[0].app.state.recognition_service.records
    assert research_sources.is_private_project is privacy_state.is_private_project
    assert research_sources.is_private_project(records, 'project-a') is False
    set_private_project(records, 'project-a', True, 0)
    assert research_sources.is_private_project(records, 'project-a') is True
    assert research_sources.is_private_project(records, 'unrelated-project') is False
