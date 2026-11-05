import base64
import binascii
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import json
import math
import os
import re
from urllib.parse import urlsplit
from uuid import uuid4
import psutil

from core.ai_kernel.contracts import validate_model_wire_attempt_dispatch
from core.storage_provider import SQLiteUnitOfWorkConflict
from backend.security.user_context import USER_ACCESS, UserError, user_action_allowed
from backend.shared.llm.model_prices import calculate_cost, validate_rates
from .server_users import DELETIONS, USERS, current_caller
from .server_model_process import model_process_origin


BALANCES = 'server_model_balances'
ENTRIES = 'server_model_balance_entries'
RESERVATIONS = 'server_model_reservations'
METERING = 'server_model_attempt_metering'
NO_WIRE = 'server_model_no_wire_receipts'
_JOB_SCOPE = ContextVar('server_model_balance_job_scope', default=None)
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z')
_MONEY = re.compile(r'-?(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,12})?\Z')
_LIMIT = Decimal('1000000000000')
_TERMINALS = frozenset({'succeeded', 'failed_transport', 'consumer_cancelled'})
_PENDING_REASONS = frozenset({'usage_unavailable', 'usage_invalid', 'cache_unavailable',
    'usage_exceeds_bound', 'transport_failed', 'consumer_cancelled', 'process_interrupted',
    'settlement_unavailable', 'receipt_unavailable'})
TOKEN_BOUND_EVIDENCE = {
    'verified_on': '2026-10-24',
    'source': 'https://api-docs.deepseek.com/api/list-models/',
    'aliases_source': 'https://api-docs.deepseek.com/quick_start/pricing/',
    'context_tokens': 1048576,
    'max_output_tokens': 393216,
}


class ServerModelBalanceError(ValueError):
    pass


@dataclass
class _JobGrant:
    owner: object
    user_id: str
    user_root: Path
    lease_token: object
    active: bool = True


def _fail(code):
    raise ServerModelBalanceError('server_model_' + code)


def _revision(value, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1):
        _fail('balance_revision_invalid')
    return value


def _money(value, *, signed=False, zero=True):
    if type(value) is not str or not _MONEY.fullmatch(value):
        _fail('balance_amount_invalid')
    result = Decimal(value)
    if abs(result) >= _LIMIT or (not signed and result < 0) or (not zero and result == 0):
        _fail('balance_amount_invalid')
    return result


def _text(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 240
            or any(ord(char) < 32 for char in value)):
        _fail('balance_reason_invalid')
    return value.strip()


def _amount(value):
    return format(value, 'f').rstrip('0').rstrip('.') if value % 1 else format(value.quantize(Decimal(1)), 'f')


def _page_boundary(cursor, user_id, state):
    if cursor is None:
        return None
    if type(cursor) is not str or not re.fullmatch(r'[A-Za-z0-9_-]{1,1024}', cursor):
        _fail('balance_query_invalid')
    try:
        payload = json.loads(base64.b64decode(cursor + '=' * (-len(cursor) % 4), altchars=b'-_', validate=True))
        if (not isinstance(payload, dict)
                or set(payload) != {'version', 'user_id', 'state', 'before_created_at', 'before_attempt_id'}
                or type(payload['version']) is not int or payload['version'] != 1
                or payload['user_id'] != user_id or payload['state'] != state
                or not isinstance(payload['before_attempt_id'], str)
                or not _ID.fullmatch(payload['before_attempt_id'])):
            _fail('balance_query_invalid')
        instant = datetime.fromisoformat(payload['before_created_at'])
        if instant.tzinfo is None or instant.astimezone(timezone.utc).isoformat() != payload['before_created_at']:
            _fail('balance_query_invalid')
    except (ValueError, TypeError, UnicodeError, OverflowError, binascii.Error):
        _fail('balance_query_invalid')
    return payload['before_created_at'], payload['before_attempt_id']


def _page_cursor(row, user_id, state):
    payload = {'version': 1, 'user_id': user_id, 'state': state,
        'before_created_at': row.payload['created_at'], 'before_attempt_id': row.object_id}
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(',', ':')).encode('utf-8')).decode('ascii').rstrip('=')


def verified_token_bound(*, provider, base_url, model, purpose, protocol='chat_completions'):
    # 仅精确官方身份具备已核验的总词元上限，代理名称和输入估算不授予预算资格。
    try:
        address = urlsplit(base_url)
        paths = {'chat_completions': {'', '/v1', '/beta'}, 'responses': {'', '/v1'},
                 'anthropic_messages': {'/anthropic'}}
        valid = (provider in {'openai', 'deepseek'} and address.scheme == 'https'
            and address.hostname == 'api.deepseek.com' and address.port in {None, 443}
            and not (address.username or address.password or address.query or address.fragment)
            and address.path.rstrip('/') in paths.get(protocol, set())
            and model in {'deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-flash-vision-exp', 'deepseek-v4-pro'}
            and purpose in {'generation', 'vision'})
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        _fail('balance_bound_unavailable')
    return dict(TOKEN_BOUND_EVIDENCE)


def _normalized_report(usage, cache):
    if not isinstance(usage, Mapping):
        return None, None, 'usage_unavailable' if usage is None else 'usage_invalid'
    fields = {'input_tokens', 'output_tokens', 'total_tokens'}
    normalized = {key: value for key, value in usage.items()
                  if key in fields and type(value) is int and value >= 0}
    if (set(usage) - fields or len(normalized) != len(usage)
            or not {'input_tokens', 'output_tokens'} <= normalized.keys()):
        return normalized or None, None, 'usage_invalid'
    if 'total_tokens' in normalized and (type(normalized['total_tokens']) is not int
            or normalized['total_tokens'] != normalized['input_tokens'] + normalized['output_tokens']):
        return None, None, 'usage_invalid'
    normalized['total_tokens'] = normalized['input_tokens'] + normalized['output_tokens']
    if cache is None:
        return normalized, None, 'cache_unavailable' if normalized['input_tokens'] else None
    fields = {'cache_read_input_tokens', 'cache_creation_input_tokens', 'cache_miss_input_tokens'}
    if not isinstance(cache, Mapping) or set(cache) - fields or any(
            value is not None and (type(value) is not int or value < 0) for value in cache.values()):
        return normalized, None, 'usage_invalid'
    translated = {'cache_read_input_tokens': cache.get('cache_read_input_tokens'),
        'cache_write_input_tokens': cache.get('cache_creation_input_tokens'),
        'uncached_input_tokens': cache.get('cache_miss_input_tokens')}
    return normalized, translated, None


class ServerModelBalances:
    def __init__(self, users, models=None, *, clock=None):
        self.users, self.models, self.records = users, models, users.records
        if models is not None and models.records.database_path != self.records.database_path:
            _fail('balance_store_invalid')
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._job_authority = None
        self._instance_id = str(uuid4())

    def bind_job_authority(self, jobs, pool):
        current = getattr(pool, 'model_balance_job_current', None)
        lease = getattr(jobs, 'execution_lease', None)
        if (self._job_authority is not None or getattr(jobs, 'users', None) is not self.users
                or getattr(pool, 'users', None) is not self.users
                or getattr(lease, '__self__', None) is not pool
                or getattr(current, '__self__', None) is not pool
                or not callable(getattr(jobs, 'active_for', None))):
            _fail('balance_job_authority_invalid')
        self._job_authority = (jobs, pool, lease.__func__, current.__func__)

    def _job_current(self, grant):
        if (not isinstance(grant, _JobGrant) or grant.owner is not self or not grant.active
                or self._job_authority is None):
            _fail('balance_job_unauthorized')
        jobs, pool, lease_function, current_function = self._job_authority
        current = getattr(pool, 'model_balance_job_current', None)
        lease = getattr(jobs, 'execution_lease', None)
        if (getattr(lease, '__self__', None) is not pool or getattr(lease, '__func__', None) is not lease_function
                or getattr(current, '__self__', None) is not pool or getattr(current, '__func__', None) is not current_function
                or jobs.active_for(grant.user_id) < 1
                or current(grant.lease_token, grant.user_id, grant.user_root) is not True):
            _fail('balance_job_unauthorized')

    @contextmanager
    def job_scope(self, user_id, user_root, *, lease_token):
        root = Path(user_root).resolve()
        if root != self.users.root_for(user_id):
            _fail('balance_job_unauthorized')
        grant = _JobGrant(self, user_id, root, lease_token)
        self._job_current(grant)
        token = _JOB_SCOPE.set(grant)
        try:
            yield
        finally:
            grant.active = False
            _JOB_SCOPE.reset(token)

    def _reservation_actor(self, tx, actor, user_id, user_root):
        access = USER_ACCESS.get()
        if actor is None and access is not None:
            if access.target_user_id != user_id:
                _fail('balance_forbidden')
            actor = access.caller
        if actor is not None:
            self._authorize(tx, actor, user_id, sending=True)
            return actor.user_id, actor.device_id
        grant = _JOB_SCOPE.get()
        self._job_current(grant)
        if grant.user_id != user_id or grant.user_root != Path(user_root).resolve():
            _fail('balance_job_unauthorized')
        user = self._user(tx, user_id)
        if user.payload.get('disabled_at') is not None:
            _fail('balance_user_disabled')
        return user_id, None

    def _now(self):
        return self.clock().astimezone(timezone.utc).isoformat()

    def _user(self, tx, user_id):
        if not isinstance(user_id, str) or not _ID.fullmatch(user_id):
            _fail('balance_user_not_found')
        row = tx.read(USERS, user_id)
        if row is None:
            _fail('balance_user_not_found')
        if tx.read(DELETIONS, user_id) is not None:
            _fail('balance_user_deleting')
        return row

    def _authorize(self, tx, actor, user_id, *, admin=False, sending=False):
        try:
            caller = current_caller(tx, actor)
        except UserError:
            _fail('balance_unauthorized')
        if not user_action_allowed(caller.payload['role'], actor.user_id, user_id,
                                   'manage_users' if admin else 'space'):
            _fail('balance_forbidden')
        user = self._user(tx, user_id)
        if sending and user.payload.get('disabled_at') is not None:
            _fail('balance_user_disabled')
        return user

    def _balance(self, tx, user_id):
        row = tx.read(BALANCES, user_id)
        payload = dict(row.payload) if row is not None else {'user_id': user_id, 'currency': 'CNY',
            'balance': '0', 'reserved': '0', 'spent': '0', 'blocked_reason': None}
        if payload.get('user_id') != user_id or payload.get('currency') != 'CNY':
            _fail('balance_record_invalid')
        for field in ('balance', 'reserved', 'spent'):
            value = payload.get(field)
            if (type(value) is not str or not re.fullmatch(r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?', value)
                    or field != 'balance' and Decimal(value) < 0):
                _fail('balance_record_invalid')
        return payload, row.revision if row else 0

    def _save_balance(self, tx, payload, revision):
        return tx.put(BALANCES, payload['user_id'], {**payload, 'updated_at': self._now()},
            expected_revision=revision)

    def _public(self, tx, user_id):
        payload, revision = self._balance(tx, user_id)
        difference = Decimal(payload['balance']) - Decimal(payload['reserved'])
        pending = sum(row.payload['user_id'] == user_id and row.payload['status'] != 'settled'
                      for row in tx.list(RESERVATIONS))
        return {key: payload[key] for key in ('user_id', 'currency', 'balance', 'reserved', 'spent')} | {
            'available': _amount(max(Decimal(0), difference)), 'revision': revision,
            'pending_count': pending, 'deficit': _amount(max(Decimal(0), -difference)),
            'blocked_reason': payload.get('blocked_reason')}

    def read(self, actor, user_id):
        with self.records.begin_read() as tx:
            self._authorize(tx, actor, user_id)
        self.recover_interrupted(user_id=user_id)
        with self.records.begin_read() as tx:
            self._authorize(tx, actor, user_id)
            return self._public(tx, user_id)

    def adjust(self, actor, user_id, *, amount, expected_revision, expected_user_revision, reason):
        delta = _money(amount, signed=True, zero=False)
        _revision(expected_revision, zero=True)
        _revision(expected_user_revision)
        reason = _text(reason)
        try:
            with self.records.begin() as tx:
                user = self._authorize(tx, actor, user_id, admin=True)
                if user.revision != expected_user_revision:
                    _fail('balance_user_revision_conflict')
                payload, revision = self._balance(tx, user_id)
                if revision != expected_revision:
                    _fail('balance_revision_conflict')
                updated = Decimal(payload['balance']) + delta
                if updated >= _LIMIT:
                    _fail('balance_amount_invalid')
                if delta < 0 and updated < Decimal(payload['reserved']):
                    _fail('balance_reserved_conflict')
                payload['balance'] = _amount(updated)
                self._save_balance(tx, payload, revision)
                tx.put(ENTRIES, 'adjust-' + uuid4().hex, {'user_id': user_id, 'currency': 'CNY',
                    'kind': 'adjustment', 'amount': _amount(delta), 'reason': reason,
                    'actor_user_id': actor.user_id, 'device_id': actor.device_id,
                    'created_at': self._now()}, expected_revision=0)
                result = self._public(tx, user_id)
                tx.commit()
                return result
        except SQLiteUnitOfWorkConflict:
            _fail('balance_revision_conflict')

    def reserve(self, *, actor=None, user_id, user_root, binding, attempt_id, model,
                max_output_tokens=None, protocol='chat_completions', dispatch=None, external_task_id=None,
                wire_request=None):
        if self.models is None:
            _fail('balance_model_owner_unavailable')
        if not isinstance(attempt_id, str) or not _ID.fullmatch(attempt_id):
            _fail('balance_attempt_invalid')
        if external_task_id is not None and (not isinstance(external_task_id, str)
                or not _ID.fullmatch(external_task_id) or dispatch is not None):
            _fail('balance_attempt_invalid')
        if dispatch is not None:
            dispatch = validate_model_wire_attempt_dispatch(dispatch)
            if dispatch['attempt_id'] != attempt_id or dispatch['model_id'] != model:
                _fail('balance_attempt_invalid')
        access = self.models.access_for(user_id, Path(user_root))
        process = psutil.Process()
        process_identity = {'process_pid': process.pid, 'process_created_at': process.create_time(),
            'process_origin': model_process_origin(self.users, user_id), 'owner_instance_id': self._instance_id}
        with self.records.begin() as tx:
            actor_user_id, device_id = self._reservation_actor(tx, actor, user_id, user_root)
            selection = access.validate_binding(binding, reader=tx)
            if selection['mode'] != 'shared':
                _fail('balance_independent')
            configuration = selection['configuration']
            if model not in configuration['allowed_models']:
                _fail('balance_model_forbidden')
            if dispatch is not None and dispatch['provider_id'] != configuration['provider']:
                _fail('balance_attempt_invalid')
            metering = None
            if selection['purpose'] in {'embedding', 'rerank', 'search', 'asr'}:
                from .policies.shared_metering import get
                policy = get(version='@3')
                try:
                    certificate = policy.bound(provider=configuration['provider'], base_url=configuration['base_url'],
                        model=model, purpose=selection['purpose'], request=wire_request)
                except ValueError:
                    _fail('balance_bound_unavailable')
                price = self.models.metering_price(tx, binding['profile_id'], model)
                try:
                    maximum = policy.reserve(certificate, price['price'])
                except ValueError:
                    _fail('balance_price_unavailable')
                metering = {'user_id': user_id, 'certificate': certificate, **price,
                    'observation': None, 'observations': []}
                bound = {'context_tokens': certificate['limits'].get('input_tokens', 0),
                    'source': certificate['source'], 'verified_on': '2026-11-03'}
                output = certificate['limits'].get('output_tokens', 0)
                rates = configuration.get('prices', {}).get(model)
            else:
                bound = verified_token_bound(provider=configuration['provider'],
                    base_url=configuration['base_url'], model=model, purpose=selection['purpose'], protocol=protocol)
                output = bound['max_output_tokens'] if max_output_tokens is None else max_output_tokens
                if type(output) is not int or not 1 <= output <= bound['max_output_tokens']:
                    _fail('balance_output_bound_invalid')
                try:
                    rates = validate_rates(configuration.get('prices', {}).get(model))
                except ValueError:
                    _fail('balance_price_unavailable')
                if any(value is None for value in rates.values()):
                    _fail('balance_price_unavailable')
                incoming = max(Decimal(rates['input_per_million']), Decimal(rates['cache_read_per_million']))
                outgoing = Decimal(rates['output_per_million'])
                # 总容量同时约束输入和输出，缓存按输入中的最高单价预留。
                maximum = (Decimal(bound['context_tokens']) * incoming
                    + Decimal(output) * max(Decimal(0), outgoing - incoming)) / Decimal(1000000)
            if tx.read(RESERVATIONS, attempt_id) is not None:
                _fail('balance_attempt_already_reserved')
            payload, revision = self._balance(tx, user_id)
            if payload.get('blocked_reason'):
                _fail('balance_account_blocked')
            if Decimal(payload['balance']) - Decimal(payload['reserved']) < maximum:
                _fail('balance_insufficient')
            payload['reserved'] = _amount(Decimal(payload['reserved']) + maximum)
            self._save_balance(tx, payload, revision)
            row = tx.put(RESERVATIONS, attempt_id, {'attempt_id': attempt_id, 'user_id': user_id,
                'currency': 'CNY', 'status': 'reserved', 'reserved_amount': _amount(maximum),
                'charged_amount': None, 'model': model, 'provider': configuration['provider'],
                'binding': dict(selection['server_model_binding']), 'rates': rates,
                'context_tokens': bound['context_tokens'], 'max_output_tokens': output,
                'bound_source': bound['source'], 'bound_verified_on': bound['verified_on'],
                'protocol': protocol, 'dispatch': dict(dispatch) if dispatch is not None else None,
                'external_task_id': external_task_id,
                'actor_user_id': actor_user_id, 'device_id': device_id, **process_identity,
                'created_at': self._now(), 'updated_at': self._now(),
                'usage': None, 'cache_observation': None, 'terminal_status': None,
                'pending_reason': None, 'bound_breached': False, 'observations': []}, expected_revision=0)
            if metering is not None:
                tx.put(METERING, attempt_id, metering, expected_revision=0)
            result = self._attempt_public(row)
            tx.commit()
            return result

    def recover_interrupted(self, user_id=None):
        if user_id is not None and (not isinstance(user_id, str) or not _ID.fullmatch(user_id)):
            _fail('balance_user_not_found')
        result = {'recovered': 0, 'alive': 0, 'unknown': 0, 'legacy': 0}
        for observed in self.records.list(RESERVATIONS):
            payload = observed.payload
            if payload['status'] != 'reserved' or user_id is not None and payload['user_id'] != user_id:
                continue
            pid, created = payload.get('process_pid'), payload.get('process_created_at')
            if (type(pid) is not int or pid < 1 or type(created) not in {int, float}
                    or not math.isfinite(created) or created <= 0):
                result['legacy'] += 1
                continue
            origin = payload.get('process_origin')
            if not isinstance(origin, dict) or not isinstance(payload.get('owner_instance_id'), str):
                result['legacy'] += 1
                continue
            try:
                current_origin = model_process_origin(self.users, payload['user_id'])
            except (OSError, ValueError):
                result['unknown'] += 1
                continue
            if origin != current_origin:
                result['unknown'] += 1
                continue
            try:
                process = psutil.Process(pid)
                interrupted = process.create_time() != created or process.status() == psutil.STATUS_ZOMBIE
            except psutil.NoSuchProcess:
                interrupted = True
            except psutil.AccessDenied:
                result['unknown'] += 1
                continue
            if not interrupted:
                result['alive'] += 1
                continue
            with self.records.begin() as tx:
                row = tx.read(RESERVATIONS, observed.object_id)
                if (row is None or row.payload['status'] != 'reserved'
                        or row.payload.get('process_pid') != pid or row.payload.get('process_created_at') != created):
                    continue
                if row.payload.get('process_origin') != origin:
                    continue
                try:
                    current_origin = model_process_origin(self.users, row.payload['user_id'])
                except (OSError, ValueError):
                    result['unknown'] += 1
                    continue
                if current_origin != origin:
                    result['unknown'] += 1
                    continue
                tx.put(RESERVATIONS, row.object_id, {**row.payload, 'status': 'pending_review',
                    'pending_reason': 'process_interrupted', 'updated_at': self._now()}, expected_revision=row.revision)
                tx.commit()
                result['recovered'] += 1
        return result

    def _attempt(self, tx, attempt_id):
        if not isinstance(attempt_id, str) or not _ID.fullmatch(attempt_id):
            _fail('balance_attempt_invalid')
        row = tx.read(RESERVATIONS, attempt_id)
        if row is None:
            _fail('balance_attempt_not_found')
        return row

    def _attempt_public(self, row):
        fields = ('attempt_id', 'user_id', 'currency', 'status', 'reserved_amount', 'charged_amount',
            'model', 'provider', 'created_at', 'updated_at', 'terminal_status', 'pending_reason',
            'usage', 'cache_observation', 'observations', 'external_task_id')
        return {key: row.payload[key] for key in fields} | {'revision': row.revision}

    def attempts(self, actor, user_id, *, state='pending', limit=50, cursor=None):
        if (not isinstance(state, str) or state not in {'pending', 'settled', 'all'}
                or type(limit) is not int or not 1 <= limit <= 200):
            _fail('balance_query_invalid')
        with self.records.begin_read() as tx:
            self._authorize(tx, actor, user_id)
        self.recover_interrupted(user_id=user_id)
        with self.records.begin_read() as tx:
            self._authorize(tx, actor, user_id)
            boundary = _page_boundary(cursor, user_id, state)
            rows = [row for row in tx.list(RESERVATIONS) if row.payload['user_id'] == user_id
                and (state == 'all' or (row.payload['status'] == 'settled') == (state == 'settled'))
                and (boundary is None or (row.payload['created_at'], row.object_id) < boundary)]
            rows.sort(key=lambda row: (row.payload['created_at'], row.object_id), reverse=True)
            page, has_more = rows[:limit], len(rows) > limit
            items = []
            for row in page:
                item = self._attempt_public(row)
                meter = tx.read(METERING, row.object_id)
                if meter is not None:
                    item['metering'] = meter.payload
                no_wire = tx.read(NO_WIRE, row.object_id)
                if no_wire is not None:
                    item['transmission'] = no_wire.payload
                items.append(item)
            return {'items': items, 'has_more': has_more,
                'next_cursor': _page_cursor(page[-1], user_id, state) if has_more else None}

    def mark_unknown(self, attempt_id, *, reason_code):
        if reason_code not in _PENDING_REASONS:
            _fail('balance_reason_invalid')
        with self.records.begin() as tx:
            row = self._attempt(tx, attempt_id)
            if row.payload['status'] == 'settled':
                return self._attempt_public(row)
            payload = {**row.payload, 'status': 'pending_review',
                       'pending_reason': 'usage_exceeds_bound' if row.payload['bound_breached'] else reason_code,
                       'updated_at': self._now()}
            saved = tx.put(RESERVATIONS, attempt_id, payload, expected_revision=row.revision)
            tx.commit()
            return self._attempt_public(saved)

    def _charge(self, tx, row, amount, *, observed, reconciliation=None):
        payload, revision = self._balance(tx, row.payload['user_id'])
        held = Decimal(row.payload['reserved_amount'])
        if Decimal(payload['reserved']) < held:
            _fail('balance_record_invalid')
        payload.update(balance=_amount(Decimal(payload['balance']) - amount),
            reserved=_amount(Decimal(payload['reserved']) - held),
            spent=_amount(Decimal(payload['spent']) + amount))
        self._save_balance(tx, payload, revision)
        saved = tx.put(RESERVATIONS, row.object_id, {**row.payload, **observed,
            'status': 'settled', 'charged_amount': _amount(amount), 'pending_reason': None,
            'reconciliation': reconciliation, 'updated_at': self._now()}, expected_revision=row.revision)
        tx.put(ENTRIES, row.object_id, {'user_id': row.payload['user_id'], 'currency': 'CNY',
            'kind': 'charge', 'attempt_id': row.object_id, 'amount': _amount(-amount),
            'created_at': self._now(), 'reconciliation': reconciliation}, expected_revision=0)
        return self._attempt_public(saved)

    def confirm_none(self, handle, *, reason):
        from ..model_costs import _BalanceWireHandle
        from ..kernel.memory_turn import MemoryTurn
        if (not isinstance(handle, _BalanceWireHandle) or handle._sink.balances is not self
                or not handle._executing or not handle._reserved or not handle._terminal
                or handle.transmission_started or handle._confirmed_none_reason != reason
                or handle._sink.purpose != 'asr' or handle._sink.cost_records is None
                or handle._meter_observation is not None):
            _fail('no_wire_proof_invalid')
        reason = _text(reason)
        dispatch = validate_model_wire_attempt_dispatch(handle.dispatch)
        store = MemoryTurn.store_for(handle._sink.cost_records)
        matches = []
        for reference in handle._handle.control.wire_attempt_receipt_refs:
            receipt = store.get(reference)
            if receipt.get('attempt_id') == dispatch['attempt_id']:
                matches.append((reference, receipt))
        if len(matches) != 1:
            _fail('no_wire_proof_invalid')
        reference, receipt = matches[0]
        terminal = handle._confirmed_none_terminal
        if (terminal not in {'failed_transport', 'consumer_cancelled'} or receipt.get('status') != terminal
                or receipt.get('usage') is not None or receipt.get('cache_metadata') is not None
                or receipt.get('error_code') != ('ai.consumer_cancelled' if terminal == 'consumer_cancelled' else 'ai.' + reason)
                or any(receipt.get(key) != dispatch.get(key) for key in
                    ('turn_id', 'model_request_id', 'attempt_number', 'provider_id', 'model_id', 'routing_snapshot_revision'))):
            _fail('no_wire_proof_invalid')
        with self.records.begin() as tx:
            row = self._attempt(tx, dispatch['attempt_id'])
            if (row.payload['status'] != 'reserved' or row.payload['dispatch'] != dict(dispatch)
                    or row.payload['binding'] != handle._sink.configuration.get('server_model_binding')
                    or row.payload['user_id'] != handle._sink.access.user_id
                    or handle._sink.access._owner is not self.models):
                _fail('no_wire_proof_invalid')
            proof = {'status': 'confirmed_none', 'reason': reason, 'receipt_ref': reference,
                'attempt_id': dispatch['attempt_id'], 'binding': row.payload['binding'],
                'user_id': row.payload['user_id']}
            tx.put(NO_WIRE, row.object_id, proof, expected_revision=0)
            result = self._charge(tx, row, Decimal(0), observed={'usage': None, 'cache_observation': None,
                'terminal_status': terminal, 'observations': row.payload['observations']})
            tx.commit()
            return result

    def settle(self, attempt_id, *, usage, cache_observation, terminal_status, meter_observation=None):
        if terminal_status not in _TERMINALS:
            _fail('balance_terminal_invalid')
        normalized, cache, reason = _normalized_report(usage, cache_observation)
        observed = {'usage': normalized, 'cache_observation': cache, 'terminal_status': terminal_status}
        with self.records.begin() as tx:
            row = self._attempt(tx, attempt_id)
            metering = tx.read(METERING, attempt_id)
            if metering is not None:
                result = self._settle_metering(tx, row, metering, meter_observation, observed)
                tx.commit()
                return result
            if row.payload['status'] == 'settled':
                if (row.payload.get('reconciliation') is None
                        and all(row.payload.get(key) == value for key, value in observed.items())):
                    return self._attempt_public(row)
                _fail('balance_settlement_conflict')
            amount = None if reason else calculate_cost(normalized, cache, row.payload['rates'])
            if row.payload['bound_breached'] or normalized and (
                    normalized.get('total_tokens', 0) > row.payload['context_tokens']
                    or normalized.get('input_tokens', 0) > row.payload['context_tokens']
                    or normalized.get('output_tokens', 0) > row.payload['max_output_tokens']
                    or amount is not None and amount > Decimal(row.payload['reserved_amount'])):
                reason = 'usage_exceeds_bound'
            elif amount is None:
                reason = reason or 'cache_unavailable'
            if reason:
                observations = list(row.payload['observations'])
                if observed not in observations:
                    observations.append(observed)
                saved = tx.put(RESERVATIONS, attempt_id, {**row.payload, **observed,
                    'status': 'pending_review', 'pending_reason': reason,
                    'bound_breached': row.payload['bound_breached'] or reason == 'usage_exceeds_bound',
                    'observations': observations,
                    'updated_at': self._now()}, expected_revision=row.revision)
                if reason == 'usage_exceeds_bound':
                    payload, revision = self._balance(tx, row.payload['user_id'])
                    payload['blocked_reason'] = reason
                    self._save_balance(tx, payload, revision)
                result = self._attempt_public(saved)
            else:
                if any(item['usage'] is not None and any(normalized.get(key) != value
                        for key, value in item['usage'].items()) for item in row.payload['observations']):
                    _fail('balance_settlement_conflict')
                result = self._charge(tx, row, amount,
                    observed={**observed, 'observations': [*row.payload['observations'], observed]})
            tx.commit()
            return result

    def _settle_metering(self, tx, row, metering, observation, observed):
        from .policies.shared_metering import get
        contract = metering.payload
        policy = get(version=contract['certificate']['version'])
        if row.payload['status'] == 'settled':
            if (row.payload.get('reconciliation') is None and contract['observation'] == observation
                    and all(row.payload.get(key) == value for key, value in observed.items())):
                return self._attempt_public(row)
            _fail('balance_settlement_conflict')
        previous = contract['observations']
        if observation is not None and any(value is not None and value != observation for value in previous):
            _fail('balance_settlement_conflict')
        amount, reason = policy.charge(contract['certificate'], contract['price'], observation)
        if row.payload['bound_breached'] or amount is not None and amount > Decimal(row.payload['reserved_amount']):
            reason = 'usage_exceeds_bound'
        observations = previous if observation in previous else [*previous, observation]
        tx.put(METERING, row.object_id, {**contract, 'observation': observation, 'observations': observations},
            expected_revision=metering.revision)
        if reason is None:
            return self._charge(tx, row, amount,
                observed={**observed, 'observations': [*row.payload['observations'], observed]})
        saved = tx.put(RESERVATIONS, row.object_id, {**row.payload, **observed,
            'status': 'pending_review', 'pending_reason': reason,
            'bound_breached': row.payload['bound_breached'] or reason == 'usage_exceeds_bound',
            'observations': [*row.payload['observations'], observed], 'updated_at': self._now()},
            expected_revision=row.revision)
        if reason == 'usage_exceeds_bound':
            balance, revision = self._balance(tx, row.payload['user_id'])
            balance['blocked_reason'] = reason
            self._save_balance(tx, balance, revision)
        return self._attempt_public(saved)

    def reconcile(self, actor, user_id, attempt_id, *, amount, expected_revision,
                  expected_user_revision, reason):
        amount = _money(amount)
        _revision(expected_revision)
        _revision(expected_user_revision)
        reason = _text(reason)
        with self.records.begin() as tx:
            user = self._authorize(tx, actor, user_id, admin=True)
            if user.revision != expected_user_revision:
                _fail('balance_user_revision_conflict')
            row = self._attempt(tx, attempt_id)
            if row.payload['user_id'] != user_id:
                _fail('balance_attempt_not_found')
            if row.revision != expected_revision:
                _fail('balance_revision_conflict')
            if row.payload['status'] == 'settled':
                _fail('balance_settlement_conflict')
            if row.payload['status'] != 'pending_review':
                _fail('balance_attempt_not_pending_review')
            result = self._charge(tx, row, amount, observed={}, reconciliation={
                'actor_user_id': actor.user_id, 'device_id': actor.device_id,
                'reason': reason, 'created_at': self._now()})
            if not any(other.payload['user_id'] == user_id
                    and other.payload['status'] != 'settled'
                    and other.payload['bound_breached']
                    for other in tx.list(RESERVATIONS)):
                payload, revision = self._balance(tx, user_id)
                if payload.get('blocked_reason') == 'usage_exceeds_bound':
                    payload['blocked_reason'] = None
                    self._save_balance(tx, payload, revision)
            tx.commit()
            return result
