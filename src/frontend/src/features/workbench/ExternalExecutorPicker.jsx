import { useEffect, useState } from 'react';
import { FocusPanel, Row, StatusDot } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { settingsApi } from '../settings/settingsApi';

export const EXECUTOR_LABELS = { builtin: '内置', 'claude-code': 'Claude Code', codex: 'Codex', 'deepseek-harness': 'DeepSeek Harness' };
export function executorReason(reason, id) {
  if (reason === 'external_executor_credentials_unavailable') return id === 'deepseek-harness' ? '配置 DeepSeek 生成模型' : '凭据未配置';
  return ({ external_host_isolation_unavailable: '此部署暂不可用', external_executor_not_installed: '未安装',
    external_executor_version_unverified: '版本待核', external_executor_version_unsupported: '版本不支持',
    external_agent_disabled: '未启用' })[reason] || '暂不可用';
}
export function runnablePlugin(plugin) {
  return Object.hasOwn(EXECUTOR_LABELS, plugin?.id) && plugin.id !== 'builtin'
    && plugin.enabled === true && plugin.installed === true && plugin.available === true && plugin.capabilities?.task === true
    && plugin.capabilities.presets?.includes('workspace') && plugin.capabilities.commands?.includes('disabled');
}
export function readExecutorPlugins(data) {
  if (!Number.isSafeInteger(data?.revision) || data.revision < 0
      || !Number.isSafeInteger(data.client_settings_revision) || data.client_settings_revision < 0 || !Array.isArray(data.plugins)
      || data.plugins.length !== 3
      || data.plugins.some(plugin => !Object.hasOwn(EXECUTOR_LABELS, plugin?.id) || plugin.id === 'builtin'
        || typeof plugin.enabled !== 'boolean' || typeof plugin.available !== 'boolean'
        || typeof plugin.installed !== 'boolean' || typeof plugin.has_key !== 'boolean'
        || typeof plugin.capabilities?.task !== 'boolean' || typeof plugin.capabilities?.memory !== 'boolean'
        || !Array.isArray(plugin.capabilities.presets) || !Array.isArray(plugin.capabilities.commands))
      || new Set(data.plugins.map(plugin => plugin.id)).size !== data.plugins.length) throw new Error('plugins_invalid');
  return data;
}

export function useExternalExecutor(projectId) {
  const [identity, setIdentity] = useState(0), [retry, setRetry] = useState(0);
  const gate = useRequestScope(`${userStorageKey(projectId)}:${identity}`), scope = gate.scope;
  const [state, setState] = useState(null), [selection, setSelection] = useState(null), [open, setOpen] = useState(null);
  useEffect(() => {
    const changed = () => { setIdentity(old => old + 1); setSelection(null); setOpen(null); };
    window.addEventListener('chriptmas-user-space', changed); window.addEventListener('chriptmas-device-required', changed);
    return () => { window.removeEventListener('chriptmas-user-space', changed); window.removeEventListener('chriptmas-device-required', changed); };
  }, []);
  useEffect(() => {
    const token = gate.issue('plugins', scope), controller = new AbortController();
    setState({ scope, loading: true });
    settingsApi.executorPlugins(controller.signal).then(readExecutorPlugins).then(data => {
      if (gate.isCurrent(token)) setState({ scope, plugins: data.plugins });
    }).catch(() => { if (gate.isCurrent(token)) setState({ scope, error: true }); });
    return () => { controller.abort(); gate.invalidate('plugins'); };
  }, [projectId, scope, retry]);
  const current = state?.scope === scope ? state : { loading: true };
  const value = selection?.scope === scope ? selection.value : 'builtin';
  return { ...current, value, open: open === scope,
    unavailable: value !== 'builtin' && !current.plugins?.some(plugin => plugin.id === value && runnablePlugin(plugin)),
    onOpen: () => { setOpen(scope); setRetry(old => old + 1); }, onClose: () => setOpen(null),
    onRetry: () => setRetry(old => old + 1), reset: () => setSelection(null),
    onChange: next => { if (next === 'builtin' || current.plugins?.some(plugin => plugin.id === next && runnablePlugin(plugin))) setSelection({ scope, value: next }); },
    async validate(signal) {
      if (value === 'builtin') return 'builtin';
      const data = readExecutorPlugins(await settingsApi.executorPlugins(signal));
      // 选择后可能卸载插件或撤销权限；提交时重读，不静默改用另一个执行者。
      if (!gate.isScopeCurrent(scope) || !data.plugins.some(plugin => plugin.id === value && runnablePlugin(plugin))) throw new Error('执行者不可用 · 重新选择');
      return value;
    },
  };
}

export function ExternalExecutorButton({ value = 'builtin', onOpen, disabled, unavailable }) {
  return <button type="button" className="workbench-executor" aria-label={`执行者 ${EXECUTOR_LABELS[value] || '内置'}`}
    title={unavailable ? '执行者不可用 · 重新选择' : '本次干活的执行者'} disabled={disabled} onClick={onOpen}>{EXECUTOR_LABELS[value] || '内置'}{unavailable && ' · 待选择'}</button>;
}
export function ExternalExecutorPicker({ value, plugins, loading, error, onChange, onClose, onRetry }) {
  const available = (plugins || []).filter(runnablePlugin);
  return <FocusPanel title="执行者" onClose={onClose}>
    <Row title="内置" selected={value === 'builtin'} onOpen={() => onChange('builtin')}/>
    {loading && <StatusDot state="processing"/>}
    {error && <p role="alert" className="workbench-error">读取未完成 · <button type="button" onClick={onRetry}>重试</button></p>}
    {available.map(plugin => <Row key={plugin.id} title={EXECUTOR_LABELS[plugin.id]} selected={value === plugin.id}
      onOpen={() => onChange(plugin.id)}/>)}
    {value !== 'builtin' && <>
      <Row readOnly title="工作目录内" meta="命令关闭"/>
      <Row readOnly title="草稿"/>
    </>}
  </FocusPanel>;
}
