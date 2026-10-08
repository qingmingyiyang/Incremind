from backend.memory_app.v2 import policies
from backend.memory_app.v2.policies.extract import v2
from backend.memory_app.v2.policies.extract_comments import prepare, decide, v3
from backend.memory_app.v2.policies.types import ModelPolicy


def test_comment_recipe_is_active_without_changing_explicit_legacy_recipe():
    before = dict(policies.ACTIVE)
    selected = policies.get('extract', version='@3')
    assert isinstance(selected, ModelPolicy)
    assert selected.prepare is prepare and selected.decide is decide
    assert selected is v3
    assert policies.get('extract') is v3
    assert policies.get('extract', version='@2') is v2
    assert policies.parse_overrides(['extract=@3']) == {'extract': '@3'}
    with policies.override(extract='@3'):
        assert policies.version('extract') == '@3'
        assert policies.get('extract') is v3
        assert policies.get('extract', version='@2') is v2
    with policies.override(extract='@2'):
        assert policies.version('extract') == '@2'
        assert policies.get('extract') is v2
        assert policies.get('extract', version='@3') is v3
    assert policies.version('extract') == '@3'
    assert policies.ACTIVE == before
