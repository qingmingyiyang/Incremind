import pytest

from backend.memory_app.v2.policies import get, override
from backend.memory_app.v2.route import validate_route_output


def plan(remember='', *, dependent=True, instruction=None):
    return {'parts': [{'intent': 'remember', 'span': remember, 'instruction': instruction,
                       'depends_on': []},
                      {'intent': 'do', 'span': '对比三份的分歧', 'depends_on': [0] if dependent else []}]}


def test_attachment_route_preserves_legacy_and_registers_new_contract():
    with override(route='@1'), pytest.raises(ValueError, match='empty_span'):
        validate_route_output('对比三份的分歧', plan(), has_files=True)
    with override(route='@2'):
        assert validate_route_output('对比三份的分歧', plan(), has_files=True).parts[1].depends_on == [0]
        assert get('route').dependency_timeout == 900
        assert get('route').prompt != get('route', version='@1').prompt


@pytest.mark.parametrize('files,dependent,instruction', [(False, True, None), (True, False, None),
                                                       (True, True, '改写原件')])
def test_attachment_route_rejects_invalid_empty_remember(files, dependent, instruction):
    with override(route='@2'), pytest.raises(ValueError):
        validate_route_output('对比三份的分歧', plan(dependent=dependent, instruction=instruction), has_files=files)


def test_attachment_route_requires_admission_part():
    with override(route='@2'), pytest.raises(ValueError, match='attachment_without_remember'):
        validate_route_output('对比三份的分歧', {'parts': [{'intent': 'do', 'span': '对比三份的分歧'}]}, has_files=True)
