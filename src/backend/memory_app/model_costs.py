"""Freeze key-free per-wire rates beside, never inside, kernel execution facts."""
from datetime import datetime
from collections.abc import Mapping
from decimal import Decimal
import logging

from core.ai_kernel.contracts import AIKernelContractError, validate_model_wire_attempt_dispatch
from backend.shared.llm.model_prices import calculate_cost, validate_rates

PRICES = 'v2_model_prices'
WIRE_PRICES = 'v2_model_wire_prices'
_IDENTITY_FIELDS = ('attempt_id', 'turn_id', 'model_request_id', 'attempt_number',
                    'routing_snapshot_revision', 'provider_id', 'model_id')
_LOG = logging.getLogger(__name__)


def configuration_binding(configuration):
    return {key: configuration.get(key) for key in
            ('revision', 'provider', 'model', 'subscription_binding')}


def attempt_cost(records, attempt):
    if records is None:
        return None
    row = records.read(WIRE_PRICES, attempt['attempt_id'])
    if row is None:
        return None
    saved = row.payload
    if (row.revision != 1 or type(saved.get('schema_version')) is not int
            or saved.get('schema_version') != 1 or saved.get('currency') != 'CNY'
            or saved.get('unit') != 'million_tokens'
            or attempt.get('usage_status') != 'reported'
            or any(type(saved.get(key)) is not type(attempt.get(key))
                   or saved.get(key) != attempt.get(key) for key in _IDENTITY_FIELDS)
            or saved.get('dispatched_at') != attempt.get('started_at')):
        return None
    # 本机零费率仍须有完整用量和冻结身份；不构造不存在的缓存观测。
    if (saved.get('model_purpose') == 'embedding' and saved.get('price_source') == 'local'
            and saved.get('provider_id') == 'local'):
        usage = attempt.get('usage')
        if (not isinstance(usage, Mapping) or usage.get('observed_only')
                or usage.get('usage_status') == 'partial'):
            return None
        incoming, outgoing = usage.get('input_tokens'), usage.get('output_tokens')
        if any(type(value) is not int or value < 0 for value in (incoming, outgoing)):
            return None
        if 'total_tokens' in usage and (type(usage['total_tokens']) is not int
                or usage['total_tokens'] != incoming + outgoing):
            return None
        try:
            rates = validate_rates(saved.get('rates'))
        except ValueError:
            return None
        if all(value is not None and Decimal(value) == 0 for value in rates.values()):
            return {'currency': 'CNY', 'amount': '0'}
    if attempt.get('usage', {}).get('input_tokens', 0) > 0 and attempt.get('cache_status') != 'reported':
        return None
    amount = calculate_cost(attempt.get('usage'), attempt.get('cache_metadata'), saved.get('rates'))
    return {'currency': 'CNY', 'amount': format(amount, 'f')} if amount is not None else None


class PriceRecordingSink:
    """Keep the original Handle, wire executor, witness and terminal semantics."""
    def __init__(self, sink, models, purpose, configuration):
        self.sink, self.models, self.purpose = sink, models, purpose
        # The endpoint is only needed in memory to identify official pricing.
        # Credentials, endpoint and prompt data never enter a snapshot.
        self.configuration = {key: configuration.get(key) for key in
            ('revision', 'provider', 'model', 'base_url', 'subscription_binding')}

    def begin_model_wire_attempt(self):
        handle = self.sink.begin_model_wire_attempt()
        try:
            dispatch = validate_model_wire_attempt_dispatch(getattr(handle, 'dispatch', None))
            if (dispatch['provider_id'] != self.configuration['provider']
                    or dispatch['model_id'] != self.configuration['model']
                    or dispatch['turn_id'] != self.sink.turn_id
                    or dispatch['model_request_id'] != self.sink.model_request_id):
                return handle
            price = self.models.model_prices(self.purpose, configuration=self.configuration,
                at=datetime.fromisoformat(dispatch['dispatched_at'].replace('Z', '+00:00')))
            snapshot = {key: dispatch[key] for key in _IDENTITY_FIELDS}
            snapshot.update(schema_version=1, dispatched_at=dispatch['dispatched_at'],
                currency='CNY', unit='million_tokens', rates=price['rates'],
                price_source=price['source'], price_revision=price['revision'],
                configuration_revision=self.configuration['revision'], model_purpose=self.purpose)
            with self.models.records.begin() as tx:
                if tx.read(WIRE_PRICES, dispatch['attempt_id']) is None:
                    tx.put(WIRE_PRICES, dispatch['attempt_id'], snapshot, expected_revision=0)
                tx.commit()
        except (AIKernelContractError, ValueError):
            # Non-kernel legacy sinks have no canonical dispatch metadata.
            _LOG.warning('model_price_snapshot_invalid')
        except Exception:
            # This optional display ledger must not alter wire authorization.
            # Missing proof is projected as unknown, never recomputed later.
            _LOG.warning('model_price_snapshot_unavailable')
        return handle
