"""Leaf external admission and call-budget reservation, never kernel authority.

The caller supplies its authenticated owner and final request. Bindings contain
no material bodies. Each read/reservation rechecks the existing domain source
authority; callers must still do this at the actual delivery boundary. A quota
reservation is neither a dispatch fact nor an external delivery receipt.
"""
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import sqlite3

from backend.recognition import WorkScope
from ..source_egress import SourceEgressService
from ..transaction_records import TransactionRecords
from .external_agent_settings import external_agent_settings
from .privacy import (
    external_catalog_allowed, external_egress_allowed, is_private_project, privacy_revision, resolve_turn_material,
)
from .policies.handoff import MAX_BUDGET


_BINDINGS = 'v2_external_agent_bindings'
_RESERVATIONS = 'v2_external_agent_reservations'
_WRITE_RESERVATIONS = 'v2_external_agent_write_reservations'
_QUOTA_PREFIX = 'v2_external_agent_quota_'
_REQUEST_FIELDS = {'client', 'tool', 'query', 'scope', 'budget'}
_BINDING_FIELDS = {'schema_version', 'owner_id', 'turn_id', 'request_json',
                   'settings_revision', 'privacy_revision', 'material_refs', 'source_snapshots'}
_MATERIAL_TYPES = {'original_item', 'original_source', 'document', 'recognition', 'experience'}


class ExternalAgentGuardError(ValueError):
    """Fixed external code without material text, provider errors or local paths."""


def _identifier(value):
    if not isinstance(value, str):
        raise ExternalAgentGuardError('external_agent_request_invalid')
    try:
        WorkScope(value)
    except ValueError:
        raise ExternalAgentGuardError('external_agent_request_invalid') from None
    return value


def _request(value, owner):
    if (not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS
            or not isinstance(value['client'], str) or value['client'] not in {'claude', 'codex'}
            or not isinstance(value['tool'], str) or value['tool'] not in {'projects', 'recall', 'methods', 'read'}
            or not isinstance(value['query'], str) or '\x00' in value['query']
            or type(value['budget']) is not int or not 1 <= value['budget'] <= MAX_BUDGET
            or not isinstance(value['scope'], Mapping) or set(value['scope']) != {'user_id', 'project_id'}
            or value['scope']['user_id'] != owner):
        raise ExternalAgentGuardError('external_agent_request_invalid')
    project = _identifier(value['scope']['project_id'])
    return {'client': value['client'], 'tool': value['tool'], 'query': value['query'],
            'scope': {'user_id': owner, 'project_id': project}, 'budget': value['budget']}


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _write_request(client, tool, project_id, owner_id):
    project_id = _identifier(project_id)
    if (not isinstance(client, str) or client not in {'claude', 'codex'}
            or not isinstance(tool, str) or tool not in {'remember', 'propose_insight'}):
        raise ExternalAgentGuardError('external_agent_request_invalid')
    return {'client': client, 'tool': tool,
        'scope': {'user_id': owner_id, 'project_id': project_id}}


def _materials(values):
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise ExternalAgentGuardError('external_agent_material_invalid')
    result, seen = [], set()
    for value in values:
        if (not isinstance(value, Mapping) or set(value) != {'type', 'id', 'project_id', 'revision'}
                or not isinstance(value['type'], str) or value['type'] not in _MATERIAL_TYPES
                or type(value['revision']) is not int or value['revision'] < 1):
            raise ExternalAgentGuardError('external_agent_material_invalid')
        identity, project = _identifier(value['id']), _identifier(value['project_id'])
        key = value['type'], project, identity
        if key in seen:
            raise ExternalAgentGuardError('external_agent_material_invalid')
        seen.add(key)
        result.append({'type': value['type'], 'id': identity, 'project_id': project,
                       'revision': value['revision']})
    return result


def _quota_collection(day):
    if not isinstance(day, str) or len(day) != 8 or not day.isascii() or not day.isdecimal():
        raise ExternalAgentGuardError('external_agent_binding_invalid')
    try:
        datetime.strptime(day, '%Y%m%d')
    except ValueError:
        raise ExternalAgentGuardError('external_agent_binding_invalid') from None
    return _QUOTA_PREFIX + day


class ExternalAgentGuard:
    """Bind one supplied owner to this existing record space; no routing occurs."""

    def __init__(self, records, *, owner_id, now=None):
        self.records, self.owner_id = records, _identifier(owner_id)
        self.now = now or (lambda: datetime.now(timezone.utc))

    @contextmanager
    def _transaction(self):
        try:
            with self.records.begin() as tx:
                yield tx
        except ExternalAgentGuardError:
            raise
        except (ValueError, TypeError, KeyError, OSError, sqlite3.Error):
            raise ExternalAgentGuardError('external_agent_binding_invalid') from None

    def _current(self, tx, turn_id, request, materials):
        settings = external_agent_settings(tx)
        project, client = request['scope']['project_id'], request['client']
        allowed = (external_catalog_allowed(tx, client)
            if request['tool'] == 'projects' and project == 'default' and materials == []
            else external_egress_allowed(tx, project, client))
        if not allowed:
            if is_private_project(tx, project):
                raise ExternalAgentGuardError('external_agent_private')
            if settings['clients'][client] is not True:
                raise ExternalAgentGuardError('external_agent_client_disabled')
            raise ExternalAgentGuardError('external_agent_disabled')
        if project == 'me' and not settings['include_profile']:
            raise ExternalAgentGuardError('external_agent_profile_disabled')
        epoch = privacy_revision(tx)
        authority = SourceEgressService(TransactionRecords(tx))
        resolved, snapshots = [], []
        for material in materials:
            own_project = material['project_id']
            if own_project not in {project, 'me'}:
                raise ExternalAgentGuardError('external_agent_material_invalid')
            if own_project == 'me' and not settings['include_profile']:
                raise ExternalAgentGuardError('external_agent_profile_disabled')
            if not external_egress_allowed(tx, own_project, client):
                raise ExternalAgentGuardError('external_agent_private')
            scope = WorkScope(self.owner_id, own_project)
            row, roots = resolve_turn_material(tx, scope, material)
            snapshot = authority.snapshot(scope, roots)
            if material['type'] == 'original_source':
                node = next((node for node in snapshot['nodes']
                    if node['type'] == 'original_source' and node['id'] == material['id']), None)
                if (node is None or node['source_revision'] != row['revision']
                        or node.get('incarnation') != row['payload']['_original_incarnation']):
                    raise ExternalAgentGuardError('external_agent_material_changed')
            # This existing source-purpose argument checks only non-private
            # source closure. External permission comes from privacy above;
            # it does not consult the global generation configuration.
            authority.require(snapshot, 'generation')
            authority.validate_snapshot(scope, snapshot)
            resolved.append(row)
            snapshots.append(snapshot)
        if privacy_revision(tx) != epoch:
            raise ExternalAgentGuardError('external_agent_binding_invalid')
        binding = {'schema_version': 1, 'owner_id': self.owner_id, 'turn_id': turn_id,
            'request_json': _encoded(request), 'settings_revision': settings['revision'],
            'privacy_revision': epoch, 'material_refs': materials, 'source_snapshots': snapshots}
        return binding, resolved

    def _validated(self, tx, turn_id, request):
        row = tx.read(_BINDINGS, turn_id)
        if (row is None or row.revision != 1 or set(row.payload) != _BINDING_FIELDS
                or type(row.payload['schema_version']) is not int or row.payload['schema_version'] != 1
                or type(row.payload['settings_revision']) is not int or row.payload['settings_revision'] < 0
                or type(row.payload['privacy_revision']) is not int or row.payload['privacy_revision'] < 0
                or row.payload['owner_id'] != self.owner_id or row.payload['turn_id'] != turn_id
                or row.payload['request_json'] != _encoded(request)):
            raise ExternalAgentGuardError('external_agent_binding_invalid')
        materials = _materials(row.payload['material_refs'])
        current, resolved = self._current(tx, turn_id, request, materials)
        # Full fresh closure comparison catches removal, policy drift and
        # original-source delete/recreation even when its revision returns to 1.
        if _encoded(current) != _encoded(dict(row.payload)):
            raise ExternalAgentGuardError('external_agent_binding_invalid')
        authority = SourceEgressService(TransactionRecords(tx))
        for material, snapshot in zip(materials, row.payload['source_snapshots']):
            scope = WorkScope(self.owner_id, material['project_id'])
            authority.validate_snapshot(scope, snapshot)
            authority.require(snapshot, 'generation')
        return current, resolved

    def freeze(self, turn_id, request, materials):
        """Freeze a body-free leaf binding; do not count a call or submit a Turn."""
        turn_id = _identifier(turn_id)
        request, materials = _request(request, self.owner_id), _materials(materials)
        with self._transaction() as tx:
            old = tx.read(_BINDINGS, turn_id)
            if old is not None:
                saved, _ = self._validated(tx, turn_id, request)
                if _encoded(saved['material_refs']) != _encoded(materials):
                    raise ExternalAgentGuardError('external_agent_binding_invalid')
                return deepcopy(saved)
            saved, _ = self._current(tx, turn_id, request, materials)
            tx.put(_BINDINGS, turn_id, saved, expected_revision=0)
            tx.commit()
        return deepcopy(saved)

    def validate(self, turn_id, request):
        """Resolve current bodies after exact binding and fresh permission checks."""
        turn_id, request = _identifier(turn_id), _request(request, self.owner_id)
        with self._transaction() as tx:
            _, resolved = self._validated(tx, turn_id, request)
        return resolved

    def _quota(self, tx, day):
        row = tx.read(_quota_collection(day), self.owner_id)
        if row is not None and (set(row.payload) != {'owner_id', 'day', 'count'}
                or row.payload['owner_id'] != self.owner_id or row.payload['day'] != day
                or type(row.payload['count']) is not int or row.payload['count'] < 1):
            raise ExternalAgentGuardError('external_agent_binding_invalid')
        return row

    def reserve(self, turn_id, request):
        """Atomically count once per exact Turn; no dispatch or delivery fact."""
        turn_id, request = _identifier(turn_id), _request(request, self.owner_id)
        with self._transaction() as tx:
            self._validated(tx, turn_id, request)
            reservation = self._reserve_once(tx, _RESERVATIONS, 'turn_id', turn_id, request)
            tx.commit()
        return deepcopy(reservation)

    def reserve_write(self, tx, *, receipt_id, client, tool, project_id):
        """Spend in the real domain transaction; its caller commits or rolls back."""
        receipt_id = _identifier(receipt_id)
        request = _write_request(client, tool, project_id, self.owner_id)
        self._current(tx, receipt_id, request, [])
        return self._reserve_once(tx, _WRITE_RESERVATIONS, 'receipt_id', receipt_id, request)

    def check_write(self, *, client, tool, project_id):
        """Check the same current admission before acquiring a public link."""
        request = _write_request(client, tool, project_id, self.owner_id)
        with self._transaction() as tx:
            self._current(tx, 'external-write-admission', request, [])

    def _reserve_once(self, tx, collection, identity_field, identity, request):
        old = tx.read(collection, identity)
        if old is not None:
            payload = old.payload
            if (old.revision != 1 or set(payload) != {'owner_id', identity_field, 'request_json', 'day'}
                    or payload['owner_id'] != self.owner_id or payload[identity_field] != identity
                    or payload['request_json'] != _encoded(request)
                    or self._quota(tx, payload['day']) is None):
                raise ExternalAgentGuardError('external_agent_binding_invalid')
            return deepcopy(dict(payload))
        now = self.now()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ExternalAgentGuardError('external_agent_clock_invalid')
        day = now.astimezone(timezone.utc).strftime('%Y%m%d')
        quota = self._quota(tx, day)
        count = quota.payload['count'] if quota is not None else 0
        if count >= external_agent_settings(tx)['daily_limit']:
            raise ExternalAgentGuardError('external_agent_quota_exhausted')
        tx.put(_quota_collection(day), self.owner_id,
            {'owner_id': self.owner_id, 'day': day, 'count': count + 1},
            expected_revision=quota.revision if quota is not None else 0)
        reservation = {'owner_id': self.owner_id, identity_field: identity,
            'request_json': _encoded(request), 'day': day}
        tx.put(collection, identity, reservation, expected_revision=0)
        return deepcopy(reservation)
