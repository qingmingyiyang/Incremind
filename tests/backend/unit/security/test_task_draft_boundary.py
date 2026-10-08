"""Only frozen task divisions may use the create-only draft exception."""
from dataclasses import replace

import pytest

from backend.security.project_boundary_profiles import ProjectBoundaryProfileStore
from backend.security.turn_boundary_adapter import TurnBoundaryRequestFactory
from core.ai_tooling import tool_from_capability
from tests.backend.unit.security.test_turn_boundary_adapter import _capability, _turn


CAPABILITY = 'document.draft.propose'


def draft():
    value = _capability(CAPABILITY, mode='write', approval=False)
    tool = replace(tool_from_capability(value), boundary_requirements=('draft_create_only',))
    return replace(value, tool_definition=tool)


def task():
    value = _turn()
    value['desired_outcome'] = 'project.task'
    value['capability_policy']['allowed'].append(CAPABILITY)
    return value


def factory(root, capabilities=(CAPABILITY,), *, deny=False):
    profiles = ProjectBoundaryProfileStore(root)
    profiles.update('project-alpha', mode='open', remote_default='allow',
        denied_effects=('write',) if deny else (), expected_revision=0)
    return TurnBoundaryRequestFactory(profiles, division_capabilities=lambda request: capabilities)


def test_frozen_task_draft_is_reversible_and_needs_no_approval(tmp_path):
    result = factory(tmp_path).evaluate(task(), draft(), destination_id='local-runtime')
    assert result.decision.outcome == 'allow'
    assert result.request.reversible is True


def test_same_draft_in_answer_still_requires_approval(tmp_path):
    value = task()
    value['desired_outcome'] = 'project.answer'
    result = factory(tmp_path).evaluate(value, draft(), destination_id='local-runtime')
    assert result.decision.outcome == 'ask'
    assert result.request.reversible is False


def test_draft_outside_frozen_division_still_requires_approval(tmp_path):
    result = factory(tmp_path, ()).evaluate(task(), draft(), destination_id='local-runtime')
    assert result.decision.outcome == 'ask'
    assert result.request.reversible is False


@pytest.mark.parametrize('mode', ['write', 'delete'])
def test_existing_object_modification_or_deletion_is_not_exempt(tmp_path, mode):
    capability = _capability(CAPABILITY, mode=mode, approval=True)
    result = factory(tmp_path).evaluate(task(), capability, destination_id='local-runtime')
    assert result.decision.outcome == 'ask'
    assert result.request.reversible is False


def test_profile_deny_wins_over_draft_exception(tmp_path):
    result = factory(tmp_path, deny=True).evaluate(task(), draft(), destination_id='local-runtime')
    assert result.decision.outcome == 'deny'
    assert result.decision.reason_codes == ('profile_explicit_deny',)


def test_unbound_factory_cannot_grant_draft_exception(tmp_path):
    result = TurnBoundaryRequestFactory(ProjectBoundaryProfileStore(tmp_path)).evaluate(
        task(), draft(), destination_id='local-runtime')
    assert result.decision.outcome == 'ask'
