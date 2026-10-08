"""外部任务的运行事实与每用户原子占位，不承担执行或外发授权。"""
from collections.abc import Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re

from backend.shared.secret_detection import contains_secret
from core.storage_provider import SQLiteUnitOfWorkConflict
from ..workspace_contracts import _now


COLLECTION = 'v2_external_runs'
SLOTS = 'v2_external_run_slots'
_IDENTITY = re.compile(r'[A-Za-z0-9][A-Za-z0-9._~-]{0,127}\Z')
_VERSION = re.compile(r'[0-9]+(?:\.[0-9]+){1,3}(?:[-+][A-Za-z0-9.-]+)?\Z')
_TERMINAL = ('completed', 'failed', 'cancelled', 'timed_out', 'output_limit')
_TOKENS = frozenset({'input_tokens', 'output_tokens', 'cached_input_tokens',
    'cache_write_input_tokens', 'reasoning_output_tokens', 'cache_read_input_tokens',
    'cache_creation_input_tokens'})
_FIELDS = frozenset({'turn_id', 'owner_id', 'executor', 'adapter_version', 'cli_version',
    'preset', 'workspace', 'status', 'reserved_at', 'started_at', 'ended_at', 'exit_code', 'usage'})


class ExternalRunError(ValueError):
    """错误码固定，不附带启动参数、路径或数据库异常。"""


class ExternalRunConflict(ExternalRunError):
    """已有事实、调用方修订或占位绑定不一致。"""


class ExternalRunBusy(ExternalRunError):
    """该用户已达到调用方配置的并发上限。"""


@dataclass(frozen=True)
class Reservation:
    reservation_created: bool
    run: dict


@dataclass(frozen=True)
class Transition:
    changed: bool
    run: dict


def _invalid():
    raise ExternalRunError('external_run_invalid')


def _conflict():
    raise ExternalRunConflict('external_run_conflicted')


def _identity(value):
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value) or contains_secret(value):
        _invalid()
    return value


def _revision(value):
    if type(value) is not int or value < 1:
        _invalid()


def _instant(value):
    try:
        if not isinstance(value, str):
            raise ValueError
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ExternalRunError('external_run_clock_invalid') from None


def _usage(value):
    if value is None:
        return None
    if (not isinstance(value, Mapping) or not {'input_tokens', 'output_tokens'} <= value.keys()
            or not value.keys() <= _TOKENS
            or any(type(counter) is not int or counter < 0 for counter in value.values())):
        _invalid()
    return dict(value)


def _metadata(turn_id, owner_id, executor, adapter_version, cli_version, preset, workspace):
    _identity(turn_id)
    _identity(owner_id)
    if (executor not in ('codex', 'claude-code') or not isinstance(adapter_version, str)
            or len(adapter_version) > 64
            or not re.fullmatch(re.escape(executor) + r'@[1-9][0-9]*', adapter_version)
            or not isinstance(cli_version, str) or len(cli_version) > 64 or not _VERSION.fullmatch(cli_version)
            or contains_secret(cli_version)
            or preset not in ('research', 'workspace', 'folder')
            or not isinstance(workspace, Path) or not workspace.is_absolute()):
        _invalid()
    path = str(workspace)
    if len(path) > 4096 or contains_secret(path) or any(ord(character) < 32 for character in path):
        _invalid()
    return dict(turn_id=turn_id, owner_id=owner_id, executor=executor, adapter_version=adapter_version,
        cli_version=cli_version, preset=preset, workspace=path)


def _validate_record(row):
    try:
        value = row.payload
        if set(value) != _FIELDS or value['turn_id'] != row.object_id:
            _conflict()
        _metadata(row.object_id, value['owner_id'], value['executor'], value['adapter_version'],
            value['cli_version'], value['preset'], Path(value['workspace']))
        reserved = _instant(value['reserved_at'])
        started = _instant(value['started_at']) if value['started_at'] is not None else None
        ended = _instant(value['ended_at']) if value['ended_at'] is not None else None
        if started is not None and started < reserved or ended is not None and ended < (started or reserved):
            _conflict()
        status = value['status']
        if status not in ('reserved', 'running', *_TERMINAL):
            _conflict()
        if status in ('reserved', 'running'):
            if (status == 'reserved' and started is not None or status == 'running' and started is None
                    or ended is not None or value['exit_code'] is not None or value['usage'] is not None):
                _conflict()
        elif (ended is None or value['exit_code'] is not None and type(value['exit_code']) is not int
                or status == 'completed' and (started is None or value['exit_code'] != 0)):
            _conflict()
        _usage(value['usage'])
    except (ExternalRunError, TypeError, KeyError, AttributeError):
        _conflict()
    return value


def _public(row):
    return {**deepcopy(dict(row.payload)), 'revision': row.revision}


class ExternalRuns:
    """同事务核验运行事实和占位；崩溃后的槽位保留，等待真实恢复证据。"""

    def __init__(self, records, *, limit=1, now=_now):
        if type(limit) is not int or limit < 1 or not callable(now):
            _invalid()
        self.records, self.limit, self.now = records, limit, now

    @contextmanager
    def _transaction(self):
        try:
            with self.records.begin() as tx:
                yield tx
                tx.commit()
        except ExternalRunError:
            raise
        except SQLiteUnitOfWorkConflict:
            raise ExternalRunConflict('external_run_conflicted') from None
        except Exception:
            raise ExternalRunError('external_run_store_failed') from None

    def _slots(self, tx, owner_id):
        row = tx.read(SLOTS, owner_id)
        active = {}
        for run in tx.list(COLLECTION):
            if run.payload.get('owner_id') == owner_id:
                value = _validate_record(run)
                if value['status'] in ('reserved', 'running'):
                    active[run.object_id] = run.revision
        if row is None:
            if active:
                _conflict()
            return None, {}
        value = row.payload
        if set(value) != {'owner_id', 'claims'} or value['owner_id'] != owner_id or not isinstance(value['claims'], list):
            _conflict()
        claims = {}
        for claim in value['claims']:
            if (not isinstance(claim, dict) or set(claim) != {'turn_id', 'revision'}
                    or not isinstance(claim['turn_id'], str) or claim['turn_id'] in claims
                    or type(claim['revision']) is not int or claim['revision'] < 1):
                _conflict()
            claims[claim['turn_id']] = claim['revision']
        if claims != active:
            _conflict()
        return row, claims

    def _save_slots(self, tx, owner_id, row, claims):
        tx.put(SLOTS, owner_id, {'owner_id': owner_id, 'claims': [
            {'turn_id': turn, 'revision': revision} for turn, revision in sorted(claims.items())]},
            expected_revision=row.revision if row else 0)

    def _run(self, tx, turn_id, owner_id):
        row = tx.read(COLLECTION, turn_id)
        if row is None or row.payload.get('owner_id') != owner_id:
            _conflict()
        _validate_record(row)
        return row

    def reserve(self, turn_id, *, owner_id, executor, adapter_version, cli_version, preset, workspace, **unknown):
        """仅新占位返回可启动标志；重复请求不会重新获得启动许可。"""
        if unknown:
            _invalid()
        metadata = _metadata(turn_id, owner_id, executor, adapter_version, cli_version, preset, workspace)
        with self._transaction() as tx:
            prior = tx.read(COLLECTION, turn_id)
            if prior is not None:
                value = _validate_record(prior)
                if any(value[key] != expected for key, expected in metadata.items()):
                    _conflict()
                self._slots(tx, owner_id)
                return Reservation(False, _public(prior))
            slot, claims = self._slots(tx, owner_id)
            if len(claims) >= self.limit:
                raise ExternalRunBusy('external_run_busy')
            reserved_at = _instant(self.now()).isoformat()
            saved = tx.put(COLLECTION, turn_id, {**metadata, 'status': 'reserved', 'reserved_at': reserved_at,
                'started_at': None, 'ended_at': None, 'exit_code': None, 'usage': None}, expected_revision=0)
            claims[turn_id] = saved.revision
            self._save_slots(tx, owner_id, slot, claims)
            return Reservation(True, _public(saved))

    def mark_started(self, turn_id, *, owner_id, expected_revision, **unknown):
        """调用方仅在真正创建 CLI 并持有进程 owner 后报告开始。"""
        if unknown:
            _invalid()
        _identity(turn_id)
        _identity(owner_id)
        _revision(expected_revision)
        with self._transaction() as tx:
            prior = self._run(tx, turn_id, owner_id)
            if prior.revision != expected_revision or prior.payload['status'] in _TERMINAL:
                _conflict()
            slot, claims = self._slots(tx, owner_id)
            if prior.payload['status'] == 'running':
                return Transition(False, _public(prior))
            started = _instant(self.now())
            if started < _instant(prior.payload['reserved_at']):
                raise ExternalRunError('external_run_clock_invalid')
            saved = tx.put(COLLECTION, turn_id, {**prior.payload, 'status': 'running', 'started_at': started.isoformat()},
                expected_revision=expected_revision)
            claims[turn_id] = saved.revision
            self._save_slots(tx, owner_id, slot, claims)
            return Transition(True, _public(saved))

    def finish(self, turn_id, *, owner_id, expected_revision, status, exit_code, usage=None, **unknown):
        """唯一终态和释放同事务完成；相同回放只读返回，不释放后来的占位。"""
        if (unknown or status not in _TERMINAL or exit_code is not None and type(exit_code) is not int
                or status == 'completed' and exit_code != 0):
            _invalid()
        _identity(turn_id)
        _identity(owner_id)
        _revision(expected_revision)
        terminal = {'status': status, 'exit_code': exit_code, 'usage': _usage(usage)}
        with self._transaction() as tx:
            prior = self._run(tx, turn_id, owner_id)
            slot, claims = self._slots(tx, owner_id)
            if prior.payload['status'] in _TERMINAL:
                if any(prior.payload[key] != value for key, value in terminal.items()):
                    _conflict()
                return Transition(False, _public(prior))
            if prior.revision != expected_revision or status == 'completed' and prior.payload['started_at'] is None:
                _conflict()
            ended = _instant(self.now())
            if ended < _instant(prior.payload['started_at'] or prior.payload['reserved_at']):
                raise ExternalRunError('external_run_clock_invalid')
            saved = tx.put(COLLECTION, turn_id, {**prior.payload, **terminal, 'ended_at': ended.isoformat()},
                expected_revision=expected_revision)
            del claims[turn_id]
            self._save_slots(tx, owner_id, slot, claims)
            return Transition(True, _public(saved))

    def read(self, turn_id, *, owner_id):
        _identity(turn_id)
        _identity(owner_id)
        try:
            row = self.records.read(COLLECTION, turn_id)
            if row is None or row.payload.get('owner_id') != owner_id:
                raise ExternalRunError('external_run_not_found')
            _validate_record(row)
            return _public(row)
        except ExternalRunError:
            raise
        except Exception:
            raise ExternalRunError('external_run_store_failed') from None
