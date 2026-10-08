"""Policy metadata is optional on history and strict on new frozen requests."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from backend.memory_app.v2.policies import pipelines
from core.ai_kernel import AIKernelContractError, validate_turn_request
from core.ai_kernel.turn_kinds import TURN_KINDS
from tests.rebuild.test_product_turn_kinds import request
from tests.memory_app.v2.test_turn_requests import materials, freeze


SCHEMA = Path(__file__).resolve().parents[3] / 'core-contracts/ai/turn-request.schema.json'


def schema():
    return Draft202012Validator(json.loads(SCHEMA.read_text(encoding='utf-8')))


@pytest.mark.parametrize('kind', TURN_KINDS)
def test_new_policy_metadata_passes_python_and_json_contracts(kind):
    value = request(kind)
    value['policy_versions'] = pipelines.versions_for_turn(kind)
    assert validate_turn_request(value) == value
    assert schema().is_valid(value)


@pytest.mark.parametrize('versions', [None, [], {}, {'unknown': '@1'}, {'Rank': '@1'},
    {'rank': 1}, {'rank': True}, {'rank': '1'}, {'rank': '@0'}, {'rank': '@01'},
    {'rank': '@-1'}, {'rank': '@1\n'}, {'rank': '@１'}])
def test_invalid_policy_maps_are_rejected_by_both_contracts(versions):
    value = request('project.answer')
    value['policy_versions'] = versions
    with pytest.raises(AIKernelContractError):
        validate_turn_request(value)
    assert not schema().is_valid(value)


def test_legacy_request_remains_byte_identical_and_unknown_fields_stay_rejected():
    value = request('project.answer')
    before = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    validated = validate_turn_request(value)
    assert 'policy_versions' not in validated
    assert json.dumps(validated, ensure_ascii=False, separators=(',', ':')).encode('utf-8') == before
    assert schema().is_valid(validated)
    invalid = {**value, 'unrecognized_policy_metadata': {'rank': '@1'}}
    with pytest.raises(AIKernelContractError):
        validate_turn_request(invalid)
    assert not schema().is_valid(invalid)


@pytest.mark.parametrize('kind', TURN_KINDS)
def test_product_freezing_adds_recipe_without_mutating_any_input(materials, kind):
    records, models, descriptors = materials
    original = copy.deepcopy(descriptors)
    if kind == 'external.context':
        # The historical generation entry has no exact external request or
        # independent permission. Its new kind must reject before acceptance.
        with pytest.raises(AIKernelContractError):
            freeze(records, models, descriptors, kind)
        assert descriptors == original
        return
    first, _ = freeze(records, models, descriptors, kind)
    second, _ = freeze(records, models, descriptors, kind)
    assert first['policy_versions'] == pipelines.versions_for_turn(kind)
    assert json.dumps(first, ensure_ascii=False, separators=(',', ':')).encode('utf-8') == json.dumps(second, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    assert descriptors == original
    assert first['input'] == second['input']
    assert first['privacy'] == second['privacy']
    assert schema().is_valid(first)
