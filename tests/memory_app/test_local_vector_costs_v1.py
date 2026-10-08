"""本机费用的冻结证明负控；真实存储，未替换费用实现。"""
import pytest

from core.storage_provider import SQLiteStructuredRecordStore
from backend.memory_app.model_costs import attempt_cost, WIRE_PRICES


def proof(records, *, receipt_changes=None, price_changes=None, usage_changes=None):
    identity = dict(attempt_id='attempt-1', turn_id='turn-1', model_request_id='request-1',
        attempt_number=1, routing_snapshot_revision=1, provider_id='local', model_id='model-1')
    usage = dict(input_tokens=12, output_tokens=0, total_tokens=12)
    usage.update(usage_changes or {})
    receipt = dict(identity, started_at='2026-10-08T00:00:00Z', usage_status='reported',
        usage=usage, cache_status='unavailable', cache_metadata=None)
    receipt.update(receipt_changes or {})
    price = dict(identity, schema_version=1, currency='CNY', unit='million_tokens',
        dispatched_at='2026-10-08T00:00:00Z', model_purpose='embedding', price_source='local',
        rates=dict(input_per_million='0', output_per_million='0', cache_read_per_million='0'))
    price.update(price_changes or {})
    with records.begin() as tx:
        tx.put(WIRE_PRICES, 'attempt-1', price, expected_revision=0)
        tx.commit()
    return receipt


@pytest.mark.parametrize('changes', [
    {'input_tokens': True}, {'input_tokens': -1}, {'input_tokens': '12'},
    {'output_tokens': -1}, {'output_tokens': 0.0}, {'total_tokens': 13},
    {'total_tokens': True}, {'observed_only': True}, {'usage_status': 'partial'},
])
def test_local_zero_rejects_incomplete_or_invalid_usage(tmp_path, changes):
    records = SQLiteStructuredRecordStore(tmp_path / 'costs.sqlite3')
    assert attempt_cost(records, proof(records, usage_changes=changes)) is None


@pytest.mark.parametrize('changes', [
    {'usage_status': 'partial'}, {'usage': None}, {'usage': []},
    {'provider_id': 'remote'}, {'model_id': 'other'}, {'attempt_number': True},
    {'started_at': '2026-10-08T00:00:01Z'},
])
def test_local_zero_requires_matching_reported_wire(tmp_path, changes):
    records = SQLiteStructuredRecordStore(tmp_path / 'costs.sqlite3')
    assert attempt_cost(records, proof(records, receipt_changes=changes)) is None


@pytest.mark.parametrize('changes', [
    {'price_source': 'custom'}, {'model_purpose': 'generation'},
    {'rates': {'input_per_million': '0', 'output_per_million': '0', 'cache_read_per_million': None}},
    {'rates': {'input_per_million': '0', 'output_per_million': '0', 'cache_read_per_million': True}},
    {'rates': {'input_per_million': '1', 'output_per_million': '0', 'cache_read_per_million': '0'}},
    {'rates': {'input_per_million': '-1', 'output_per_million': '0', 'cache_read_per_million': '0'}},
    {'rates': {'input_per_million': '0', 'output_per_million': '0'}},
])
def test_local_zero_requires_explicit_valid_three_rates(tmp_path, changes):
    records = SQLiteStructuredRecordStore(tmp_path / 'costs.sqlite3')
    assert attempt_cost(records, proof(records, price_changes=changes)) is None


def test_remote_unknown_cache_still_has_unknown_cost(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'costs.sqlite3')
    receipt = proof(records, receipt_changes={'provider_id': 'openai'},
        price_changes={'provider_id': 'openai', 'price_source': 'custom'})
    assert attempt_cost(records, receipt) is None
