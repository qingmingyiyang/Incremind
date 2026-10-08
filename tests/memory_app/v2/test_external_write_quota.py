"""Writes share the original call quota and the caller's real SQLite transaction."""
from datetime import datetime, timezone

import pytest

from backend.memory_app.v2.external_agent_guard import ExternalAgentGuard, ExternalAgentGuardError
from backend.memory_app.v2.privacy import set_private_project
from core.storage_provider import SQLiteStructuredRecordStore
from tests.memory_app.v2.test_external_agent_guard import settings, request


NOW = datetime(2026, 10, 6, 1, tzinfo=timezone.utc)
QUOTA = 'v2_external_agent_quota_20261006'
WRITES = 'v2_external_agent_write_reservations'


@pytest.fixture
def records(tmp_path):
    records = SQLiteStructuredRecordStore(tmp_path / 'records.sqlite3')
    settings(records, allow_remote=True)
    return records


def reserve(guard, tx, **changes):
    return guard.reserve_write(tx, **({'receipt_id': 'mcp-receipt-one', 'client': 'codex',
        'tool': 'remember', 'project_id': 'alpha'} | changes))


def test_write_reservation_is_body_free_idempotent_and_uses_the_original_quota(records):
    guard = ExternalAgentGuard(records, owner_id='local-user', now=lambda: NOW)
    with records.begin() as tx:
        first = reserve(guard, tx)
        assert reserve(guard, tx) == first
        tx.commit()
    assert set(first) == {'owner_id', 'receipt_id', 'request_json', 'day'}
    assert first['receipt_id'] == 'mcp-receipt-one' and first['day'] == '20261006'
    assert records.read(QUOTA, 'local-user').payload['count'] == 1
    assert len(records.list(WRITES)) == 1
    assert records.list('v2_external_agent_bindings') == records.list('v2_external_agent_reservations') == ()


def test_write_reservation_and_domain_write_roll_back_together(records):
    guard = ExternalAgentGuard(records, owner_id='local-user', now=lambda: NOW)
    with pytest.raises(RuntimeError, match='synthetic_failure'):
        with records.begin() as tx:
            reserve(guard, tx)
            tx.put('v2_test_external_write', 'example', {'state': 'pending'}, expected_revision=0)
            raise RuntimeError('synthetic_failure')
    assert records.read(QUOTA, 'local-user') is None
    assert records.list(WRITES) == records.list('v2_test_external_write') == ()


@pytest.mark.parametrize('disabled', ['client', 'remote', 'private', 'profile'])
def test_write_admission_reuses_current_client_privacy_and_profile_settings(records, disabled):
    project = 'me' if disabled == 'profile' else 'alpha'
    if disabled == 'client':
        settings(records, clients={'codex': False, 'claude': True})
    elif disabled == 'remote':
        settings(records, allow_remote=False)
    elif disabled == 'private':
        set_private_project(records, project, True, 0)
    else:
        settings(records, include_profile=False)
    guard = ExternalAgentGuard(records, owner_id='local-user', now=lambda: NOW)
    with pytest.raises(ExternalAgentGuardError):
        with records.begin() as tx:
            reserve(guard, tx, project_id=project)
    assert records.read(QUOTA, 'local-user') is None and records.list(WRITES) == ()


def test_read_and_write_share_one_limit_without_fabricating_read_bindings(records):
    settings(records, daily_limit=1)
    guard = ExternalAgentGuard(records, owner_id='local-user', now=lambda: NOW)
    with records.begin() as tx:
        reserve(guard, tx)
        tx.commit()
    guard.freeze('turn-read', request(), [])
    with pytest.raises(ExternalAgentGuardError, match='external_agent_quota_exhausted'):
        guard.reserve('turn-read', request())
    with pytest.raises(ExternalAgentGuardError, match='external_agent_quota_exhausted'):
        with records.begin() as tx:
            reserve(guard, tx, receipt_id='mcp-receipt-two', tool='propose_insight')
    assert records.read(QUOTA, 'local-user').payload['count'] == 1
    assert len(records.list(WRITES)) == 1 and records.list('v2_external_agent_reservations') == ()


def test_write_receipt_identity_cannot_be_rebound_to_another_tool(records):
    guard = ExternalAgentGuard(records, owner_id='local-user', now=lambda: NOW)
    with records.begin() as tx:
        reserve(guard, tx)
        tx.commit()
    with pytest.raises(ExternalAgentGuardError, match='external_agent_binding_invalid'):
        with records.begin() as tx:
            reserve(guard, tx, tool='propose_insight')
    assert records.read(QUOTA, 'local-user').payload['count'] == 1
