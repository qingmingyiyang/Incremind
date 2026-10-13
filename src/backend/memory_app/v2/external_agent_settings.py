"""Independent external-agent preferences; runtime admission belongs to callers."""
from collections.abc import Mapping

from core.storage_provider import SQLiteUnitOfWorkConflict


COLLECTION = 'v2_external_agent_settings'
_ID = 'default'
_FIELDS = {'allow_remote', 'include_profile', 'daily_limit', 'clients'}


class ExternalAgentSettingsError(ValueError):
    """Fixed code without input values or storage details."""


def _defaults():
    return {'allow_remote': False, 'include_profile': True, 'daily_limit': 200,
            'clients': {'claude': True, 'codex': True}}


def _validated(value):
    if (not isinstance(value, Mapping) or set(value) != _FIELDS
            or type(value['allow_remote']) is not bool or type(value['include_profile']) is not bool
            or type(value['daily_limit']) is not int or value['daily_limit'] < 1):
        raise ExternalAgentSettingsError('external_agent_settings_invalid')
    clients = value['clients']
    if (not isinstance(clients, Mapping) or set(clients) != {'claude', 'codex'}
            or any(type(enabled) is not bool for enabled in clients.values())):
        raise ExternalAgentSettingsError('external_agent_settings_invalid')
    return {**value, 'clients': dict(clients)}


def external_agent_settings(reader):
    row = reader.read(COLLECTION, _ID)
    return {'revision': row.revision if row else 0,
            **(_validated(row.payload) if row else _defaults())}


def replace_external_agent_settings(records, value, *, expected_revision):
    with records.begin() as tx:
        result = replace_external_agent_settings_in_transaction(tx, value, expected_revision=expected_revision)
        tx.commit()
        return result


def replace_external_agent_settings_in_transaction(tx, value, *, expected_revision):
    """插件入口在同一事务委托原偏好 owner，保留两客户端 payload 形状。"""
    wanted = _validated(value)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ExternalAgentSettingsError('external_agent_settings_invalid')
    current = external_agent_settings(tx)
    if current['revision'] != expected_revision:
        raise SQLiteUnitOfWorkConflict('external_agent_revision_conflict')
    if {key: current[key] for key in _FIELDS} == wanted:
        return current
    row = tx.put(COLLECTION, _ID, wanted, expected_revision=expected_revision)
    return {'revision': row.revision, **wanted}
