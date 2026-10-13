"""固定可信执行插件和独立偏好；不装载请求提供的代码、路径或命令。"""
from copy import deepcopy

from core.storage_provider import SQLiteUnitOfWorkConflict

from .external_agent_settings import external_agent_settings, replace_external_agent_settings_in_transaction


COLLECTION = 'v2_external_executor_plugins'
CLIENTS = frozenset({'claude', 'codex', 'deepseek-harness'})
EXECUTOR_CLIENTS = {'claude-code': 'claude', 'codex': 'codex', 'deepseek-harness': 'deepseek-harness'}
MANIFESTS = {
    'claude-code': {'id': 'claude-code', 'version': 'claude-code@1', 'client': 'claude',
        'display_name': 'Claude Code', 'capabilities': {'memory': True, 'task': True,
            'presets': ['research', 'workspace', 'folder'], 'commands': ['disabled', 'sandboxed', 'explicit']}},
    'codex': {'id': 'codex', 'version': 'codex@1', 'client': 'codex',
        'display_name': 'Codex', 'capabilities': {'memory': True, 'task': True,
            'presets': ['research', 'workspace', 'folder'], 'commands': ['disabled', 'sandboxed', 'explicit']}},
    'deepseek-harness': {'id': 'deepseek-harness', 'version': 'deepseek-harness@1', 'client': 'deepseek-harness',
        'display_name': 'DeepSeek Harness', 'capabilities': {'memory': True, 'task': True,
            'presets': ['workspace'], 'commands': ['disabled']}},
}


class ExecutorPluginError(ValueError):
    """错误码不包含输入正文、本机路径或凭据。"""


def _validated(plugins):
    if (not isinstance(plugins, dict) or set(plugins) != set(MANIFESTS)
            or any(not isinstance(value, dict) or set(value) != {'enabled'}
                or type(value['enabled']) is not bool for value in plugins.values())):
        raise ExecutorPluginError('external_executor_plugins_invalid')
    return deepcopy(plugins)


def plugin_preferences(reader):
    row = reader.read(COLLECTION, 'default')
    plugins = _validated(row.payload['plugins']) if row else {key: {'enabled': True} for key in MANIFESTS}
    return {'revision': row.revision if row else 0, 'plugins': plugins}


def replace_plugin_preferences(records, plugins, *, expected_revision, expected_client_settings_revision):
    wanted = _validated(plugins)
    if (type(expected_revision) is not int or expected_revision < 0
            or type(expected_client_settings_revision) is not int or expected_client_settings_revision < 0):
        raise ExecutorPluginError('external_executor_plugins_invalid')
    with records.begin() as tx:
        current = plugin_preferences(tx)
        if (current['revision'] != expected_revision
                or external_agent_settings(tx)['revision'] != expected_client_settings_revision):
            raise SQLiteUnitOfWorkConflict('external_executor_plugins_revision_conflict')
        legacy = external_agent_settings(tx)
        replace_external_agent_settings_in_transaction(tx,
            {key: value for key, value in legacy.items() if key != 'revision'} | {
                'clients': {client: wanted[executor]['enabled'] for executor, client in
                    EXECUTOR_CLIENTS.items() if client in {'claude', 'codex'}}},
            expected_revision=expected_client_settings_revision)
        if current['plugins'] == wanted:
            tx.commit()
            return current
        row = tx.put(COLLECTION, 'default', {'plugins': wanted}, expected_revision=expected_revision)
        tx.commit()
    return {'revision': row.revision, 'plugins': wanted}


def effective_external_agent_settings(reader):
    """旧两客户端 payload 保持原样，所有实际准入读取同一插件开关。"""
    legacy, plugins = external_agent_settings(reader), plugin_preferences(reader)
    clients = {client: plugins['plugins'][executor]['enabled']
        and legacy['clients'].get(client, True) for executor, client in EXECUTOR_CLIENTS.items()}
    # SQLite revision 是有符号 64 位整数；旁路修订为零时保留所有旧交付资格。
    return {**legacy, 'clients': clients, 'revision': legacy['revision'] + plugins['revision'] * (1 << 63)}


def plugin_status(records, state, models=None):
    preferences = plugin_preferences(records)
    legacy = external_agent_settings(records)
    host = getattr(state, 'external_execution_host', None)
    registrations = getattr(host, 'registrations', {}) if host else {}
    deployment = getattr(state, 'deployment', None)
    server = getattr(deployment, 'mode', None) == 'server'
    rows = []
    for identity, manifest in MANIFESTS.items():
        discovered = identity in registrations
        version = getattr(registrations.get(identity), 'cli_version', None)
        has_key = host.credential_status(identity)['has_key'] if host is not None else False
        isolated = not server or host is not None and host.server_available(identity)
        if identity == 'deepseek-harness' and models is not None and host is None:
            from .external_harness_profile import harness_credential_status
            has_key = harness_credential_status(models)['has_key']
        runnable = (discovered and version is not None and isolated
            and has_key
            and preferences['plugins'][identity]['enabled']
            and legacy['allow_remote'] and legacy['clients'].get(manifest['client'], True))
        rows.append({**deepcopy(manifest), **preferences['plugins'][identity],
            'enabled': preferences['plugins'][identity]['enabled'] and legacy['clients'].get(manifest['client'], True),
            'installed': discovered, 'runtime_version': version, 'has_key': has_key,
            'available': runnable,
            'capabilities': {**deepcopy(manifest['capabilities']), 'task': runnable,
                'presets': ['workspace'], 'commands': ['disabled']},
            'reason': 'external_host_isolation_unavailable' if not isolated else
                'external_executor_not_installed' if not discovered else
                'external_executor_version_unverified' if version is None else
                'external_executor_credentials_unavailable' if not has_key else
                None if runnable else 'external_agent_disabled',
            'runtime_status': 'unsupported' if not isolated else 'discovered' if discovered else 'not_installed'})
    return {'revision': preferences['revision'], 'client_settings_revision': legacy['revision'], 'plugins': rows}


def install_executor_plugin_routes(application, *, records, models):
    from fastapi import APIRouter, HTTPException, Request
    from ..workspace_contracts import _json
    router = APIRouter(prefix='/api/v2/settings/external-executor-plugins')

    @router.get('')
    async def read():
        return plugin_status(records, application.state, models)

    @router.put('')
    async def replace(request: Request):
        value = await _json(request)
        if set(value) != {'expected_revision', 'expected_client_settings_revision', 'plugins'}:
            raise HTTPException(400, 'external_executor_plugins_invalid')
        try:
            replace_plugin_preferences(records, value['plugins'], expected_revision=value['expected_revision'],
                expected_client_settings_revision=value['expected_client_settings_revision'])
            return plugin_status(records, application.state, models)
        except ExecutorPluginError:
            raise HTTPException(400, 'external_executor_plugins_invalid') from None
        except SQLiteUnitOfWorkConflict:
            raise HTTPException(409, 'external_executor_plugins_revision_conflict') from None

    application.include_router(router)
