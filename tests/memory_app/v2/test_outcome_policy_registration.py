import pytest

from backend.memory_app.v2 import policies
from backend.memory_app.v2.policies import continuation, style


def test_registered_candidates_and_active_defaults_keep_explicit_versions():
    assert policies.get('continuation', version='@1') is continuation.v1
    assert policies.get('continuation', version='@2') is continuation.v2
    assert policies.get('style', version='@1') is style.v1
    assert policies.ACTIVE['continuation'] == '@2' and policies.get('continuation') is continuation.v2
    assert policies.ACTIVE['style'] == '@1' and policies.get('style') is style.v1


def test_candidate_override_is_local_and_does_not_fall_back_for_unknown_version():
    before = dict(policies.ACTIVE)
    with policies.override(continuation='@1', style='@1'):
        assert policies.get('continuation') is continuation.v1
        assert policies.get('style') is style.v1
        assert policies.version('continuation') == policies.version('style') == '@1'
        with pytest.raises(ValueError, match='unknown_policy_version'):
            policies.get('style', version='@999999')
    assert policies.ACTIVE == before
    assert policies.version('continuation') == '@2' and policies.version('style') == '@1'


def test_unactivated_configuration_still_rejects_implicit_candidate_selection(monkeypatch):
    with monkeypatch.context() as inactive:
        inactive.delitem(policies.ACTIVE, 'continuation')
        inactive.delitem(policies.ACTIVE, 'style')
        with pytest.raises(ValueError, match='unknown_policy_interface'):
            policies.get('continuation')
        with pytest.raises(ValueError, match='unknown_policy_interface'):
            policies.get('style')
    assert policies.version('continuation') == '@2' and policies.version('style') == '@1'
