"""Freeze key-free per-wire rates beside, never inside, kernel execution facts."""
from datetime import datetime
from collections.abc import Mapping
from decimal import Decimal
import logging
from threading import RLock

from core.ai_kernel.contracts import AIKernelContractError, validate_model_wire_attempt_dispatch
from backend.shared.llm.model_transport import ModelInterrupted
from backend.shared.llm.litellm_gateway import _is_consumer_cancellation
from backend.shared.llm.model_prices import calculate_cost, validate_rates

PRICES = 'v2_model_prices'
WIRE_PRICES = 'v2_model_wire_prices'
WIRE_SHARED_COSTS = 'v2_model_wire_shared_costs'
_IDENTITY_FIELDS = ('attempt_id', 'turn_id', 'model_request_id', 'attempt_number',
                    'routing_snapshot_revision', 'provider_id', 'model_id')
_LOG = logging.getLogger(__name__)


class SharedModelMeteringError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.public_code = code
        self.status_code = 402 if code in {'server_model_balance_insufficient', 'server_model_balance_account_blocked'} else 403
        self.code = 'insufficient_quota' if self.status_code == 402 else 'server_model_access_denied'
        self.retryable = False


def _metering_error(error):
    from .v2.server_model_balance import ServerModelBalanceError
    from .v2.server_models import ServerModelError
    return SharedModelMeteringError(str(error) if isinstance(error, (ServerModelBalanceError, ServerModelError))
        else 'server_model_billing_unavailable')


def configuration_binding(configuration):
    return {key: configuration.get(key) for key in
            ('revision', 'provider', 'model', 'subscription_binding')} | (
                {'server_model_binding': dict(configuration['server_model_binding'])}
                if configuration.get('server_model_binding') is not None else {})


def attempt_cost(records, attempt):
    if records is None:
        return None
    charged = records.read(WIRE_SHARED_COSTS, attempt['attempt_id'])
    if charged is not None:
        saved = charged.payload
        if (saved.get('currency') == 'CNY' and saved.get('status') == 'settled'
                and all(saved.get(key) == attempt.get(key) for key in _IDENTITY_FIELDS)
                and saved.get('dispatched_at') == attempt.get('started_at')):
            return {'currency': 'CNY', 'amount': saved['amount']}
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
            ('revision', 'provider', 'model', 'base_url', 'subscription_binding')} | (
                {'server_model_binding': dict(configuration['server_model_binding'])}
                if configuration.get('server_model_binding') is not None else {})

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


class BalanceRecordingSink:
    def __init__(self, sink, balances, access, purpose, configuration, *, max_output_tokens=None, wire_request=None,
                 cost_records=None):
        self.sink, self.balances, self.access, self.purpose = sink, balances, access, purpose
        self.configuration = dict(configuration)
        self.max_output_tokens = max_output_tokens
        self.wire_request = wire_request
        self.cost_records = cost_records

    def __getattr__(self, name):
        return getattr(self.sink, name)

    def begin_model_wire_attempt(self):
        return _BalanceWireHandle(self.sink.begin_model_wire_attempt(), self)


class _BalanceWireHandle:
    def __init__(self, handle, sink):
        self._handle, self._sink = handle, sink
        self._lock = RLock()
        self._reserved, self._terminal, self._invoked = False, False, False
        self._meter_observation = None
        self._executing, self._transmission_started = False, False
        self._confirmed_none_reason = None
        self._confirmed_none_terminal = None

    def __getattr__(self, name):
        return getattr(self._handle, name)

    def _failed(self, error):
        control = getattr(self._handle, 'control', None)
        if (isinstance(error, ModelInterrupted) or _is_consumer_cancellation(error)
                or getattr(control, 'cancel_requested', False) is True):
            self.consumer_cancelled()
        else:
            self.failed_transport(error_code='ai.shared_model_wire_failed')

    def invoke_wire(self, handler):
        if not callable(handler):
            raise TypeError('model wire Handler must be callable')

        def guarded():
            with self._lock:
                if self._invoked:
                    raise SharedModelMeteringError('server_model_wire_already_invoked')
                self._invoked = True
            try:
                dispatch = validate_model_wire_attempt_dispatch(self._handle.dispatch)
                configuration = self._sink.configuration
                binding = configuration.get('server_model_binding')
                if (not isinstance(binding, Mapping) or binding.get('mode') != 'shared'
                        or binding.get('purpose') != self._sink.purpose
                        or dispatch['model_id'] != configuration.get('model')
                        or dispatch['provider_id'] != configuration.get('provider')):
                    raise SharedModelMeteringError('server_model_billing_binding_invalid')
                self._sink.balances.reserve(user_id=self._sink.access.user_id,
                    user_root=self._sink.access.user_root, binding=binding,
                    attempt_id=dispatch['attempt_id'], model=dispatch['model_id'],
                    max_output_tokens=self._sink.max_output_tokens, dispatch=dispatch,
                    wire_request=self._sink.wire_request)
                self._reserved = True
            except Exception as error:
                failure = error if isinstance(error, SharedModelMeteringError) else _metering_error(error)
                self.failed_transport(error_code='ai.' + failure.public_code)
                raise failure from None
            try:
                self._executing = True
                result = handler()
                if not self._terminal:
                    self.failed_transport(error_code='ai.server_model_wire_receipt_missing')
                    raise SharedModelMeteringError('server_model_billing_unavailable')
                return result
            except BaseException as error:
                self._failed(error)
                raise
            finally:
                self._executing = False

        return self._handle.invoke_wire(guarded)

    def mark_transmission_started(self):
        with self._lock:
            if (not self._reserved or not self._executing or self._terminal or self._transmission_started):
                raise SharedModelMeteringError('server_model_wire_not_active')
            self._transmission_started = True

    @property
    def transmission_started(self):
        return self._transmission_started

    def confirmed_none(self, *, error_code, error=None):
        with self._lock:
            if (not self._reserved or not self._executing or self._terminal or self._transmission_started
                    or self._meter_observation is not None or self._sink.purpose != 'asr'):
                raise SharedModelMeteringError('server_model_no_wire_proof_invalid')
            self._terminal = True
            self._confirmed_none_reason = error_code
        try:
            cancelled = (isinstance(error, ModelInterrupted) or error is not None and _is_consumer_cancellation(error)
                or getattr(getattr(self._handle, 'control', None), 'cancel_requested', False) is True)
            self._confirmed_none_terminal = 'consumer_cancelled' if cancelled else 'failed_transport'
            if cancelled:
                self._handle.consumer_cancelled()
            else:
                self._handle.failed_transport(error_code='ai.' + error_code)
            settlement = self._sink.balances.confirm_none(self, reason=error_code)
            self._record_settlement(settlement)
        except BaseException:
            self._sink.balances.mark_unknown(self._handle.dispatch['attempt_id'], reason_code='receipt_unavailable')
            raise

    def _record_settlement(self, settlement):
        if self._sink.wire_request is None or self._sink.cost_records is None:
            return
        dispatch = self._handle.dispatch
        snapshot = {key: dispatch[key] for key in _IDENTITY_FIELDS}
        snapshot.update(currency='CNY', status=settlement['status'],
            amount=settlement['charged_amount'], dispatched_at=dispatch['dispatched_at'])
        with self._sink.cost_records.begin() as tx:
            row = tx.read(WIRE_SHARED_COSTS, dispatch['attempt_id'])
            tx.put(WIRE_SHARED_COSTS, dispatch['attempt_id'], snapshot,
                expected_revision=row.revision if row else 0)
            tx.commit()

    def observe_metering(self, response):
        if not self._reserved or self._terminal:
            raise SharedModelMeteringError('server_model_wire_not_active')
        from .v2.server_model_balance import METERING
        from .v2.policies.shared_metering import get
        row = self._sink.balances.records.read(METERING, self._handle.dispatch['attempt_id'])
        if row is not None:
            certificate = row.payload['certificate']
            observation = get(version=certificate['version']).observe(certificate, response)
            if self._meter_observation is not None and observation != self._meter_observation:
                raise SharedModelMeteringError('server_model_balance_settlement_conflict')
            self._meter_observation = observation

    def _finish(self, status, *, usage=None, cache_observation=None, error_code=None):
        with self._lock:
            if self._terminal:
                return
            self._terminal = True
        if status == 'succeeded' and not self._reserved:
            self._handle.failed_transport(error_code='ai.server_model_wire_not_reserved')
            raise SharedModelMeteringError('server_model_wire_not_reserved')
        try:
            if status == 'succeeded':
                self._handle.succeeded(usage=usage, cache_observation=cache_observation)
            elif status == 'consumer_cancelled':
                self._handle.consumer_cancelled(usage=usage, cache_observation=cache_observation)
            else:
                self._handle.failed_transport(error_code=error_code,
                    usage=usage, cache_observation=cache_observation)
        except BaseException:
            if self._reserved:
                self._sink.balances.mark_unknown(self._handle.dispatch['attempt_id'],
                    reason_code='receipt_unavailable')
            raise
        if self._reserved:
            try:
                settlement = self._sink.balances.settle(self._handle.dispatch['attempt_id'], usage=usage,
                    cache_observation=cache_observation, terminal_status=status,
                    meter_observation=self._meter_observation)
                self._record_settlement(settlement)
            except Exception as error:
                self._sink.balances.mark_unknown(self._handle.dispatch['attempt_id'],
                    reason_code='settlement_unavailable')
                raise _metering_error(error) from None

    def succeeded(self, *, usage, cache_observation):
        self._finish('succeeded', usage=usage, cache_observation=cache_observation)

    def failed_transport(self, *, error_code, usage=None, cache_observation=None):
        self._finish('failed_transport', error_code=error_code, usage=usage, cache_observation=cache_observation)

    def consumer_cancelled(self, *, usage=None, cache_observation=None):
        self._finish('consumer_cancelled', usage=usage, cache_observation=cache_observation)
