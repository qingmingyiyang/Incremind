"""代理对话留存偏好；独立旁路 CAS 不改变外发授权。"""
from collections.abc import Mapping

from core.storage_provider import SQLiteUnitOfWorkConflict


COLLECTION = 'v2_external_proxy_settings'
_ID = 'default'


class ExternalProxySettingsError(ValueError):
    """固定错误码不带设置输入或存储详情。"""


def _validated(value):
    if not isinstance(value, Mapping) or set(value) != {'record_conversations'}:
        raise ExternalProxySettingsError('external_proxy_settings_invalid')
    clients = value['record_conversations']
    if (not isinstance(clients, Mapping) or set(clients) != {'claude','codex'}
            or any(type(enabled) is not bool for enabled in clients.values())):
        raise ExternalProxySettingsError('external_proxy_settings_invalid')
    return {'record_conversations':dict(clients)}


def external_proxy_settings(reader):
    row = reader.read(COLLECTION, _ID)
    return {'revision':row.revision if row else 0,
        **(_validated(row.payload) if row else {'record_conversations':{'claude':False,'codex':False}})}


def replace_external_proxy_settings(records, value, *, expected_revision):
    wanted = _validated(value)
    if type(expected_revision) is not int or expected_revision < 0:
        raise ExternalProxySettingsError('external_proxy_settings_invalid')
    with records.begin() as tx:
        current = external_proxy_settings(tx)
        if current['revision'] != expected_revision:
            raise SQLiteUnitOfWorkConflict('external_proxy_revision_conflict')
        if current['record_conversations'] == wanted['record_conversations']:
            return current
        row = tx.put(COLLECTION, _ID, wanted, expected_revision=expected_revision)
        tx.commit()
    return {'revision':row.revision, **wanted}
