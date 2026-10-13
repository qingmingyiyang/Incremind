import { useEffect, useId, useRef, useState } from 'react';
import { Row, Switch } from '../../shared/ui';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { formatLocalShortDate } from '../../shared/lib/time';
import { readExecutorPlugins, EXECUTOR_LABELS, executorReason } from '../workbench/ExternalExecutorPicker';

const CLIENTS = [{ id: 'claude-code', client: 'claude' }, { id: 'codex', client: 'codex' }, { id: 'deepseek-harness', client: 'deepseek-harness' }];

export function ExternalAgentSettings({ projectId, value, proxy, expanded, onOpen, onConfigureGeneration, api, run, busy }) {
  const retentionId = useId(), retention = '外部 agent 可能保留交出的内容';
  const proxyId = useId(), [proxyOpen, setProxyOpen] = useState(false);
  const proxyPath = libraryBackendUrl('/api/v2/external-agent/proxy');
  const address = proxyPath.startsWith('/')
    ? new URL(proxyPath, import.meta.env.DEV ? __BACKEND_PROXY_ORIGIN__ : globalThis.location.origin).href
    : proxyPath;
  const proxyDescription = `${address}/claude/v1/messages · ${address}/codex/v1/responses`;
  const [limit, setLimit] = useState(String(value.daily_limit));
  const [client, setClient] = useState(null), [connection, setConnection] = useState(null);
  const [readError, setReadError] = useState(''), [copyError, setCopyError] = useState('');
  const [retry, setRetry] = useState(0), [copying, setCopying] = useState(false), [copied, setCopied] = useState(null);
  const [snapshotTime, setSnapshotTime] = useState(null);
  const [plugins, setPlugins] = useState(null), [pluginError, setPluginError] = useState('');
  const copyRequest = useRef(null);
  const gate = useRequestScope(projectId), scope = gate.scope;
  useEffect(() => { setLimit(String(value.daily_limit)); }, [value.revision, value.daily_limit]);
  useEffect(() => {
    if (!expanded) return;
    const token = gate.issue('plugins', scope), controller = new AbortController();
    setPlugins(null); setPluginError('');
    copyRequest.current?.abort(); gate.invalidate('copy'); setCopying(false); setCopied(null); setSnapshotTime(null);
    api.executorPlugins(controller.signal).then(readExecutorPlugins).then(data => {
      if (gate.isCurrent(token)) setPlugins(data);
    }).catch(() => { if (gate.isCurrent(token)) setPluginError('读取未完成 · 重试'); });
    return () => { controller.abort(); gate.invalidate('plugins'); };
  }, [api, projectId, expanded, retry, value.revision, busy]);
  useEffect(() => {
    if (!expanded || !client) return undefined;
    const token = gate.issue('connection', scope), controller = new AbortController();
    setConnection(null); setReadError(''); setCopyError(''); setCopied(null); setCopying(false); setSnapshotTime(null);
    api.connection(projectId, controller.signal).then(data => {
      if (!gate.isCurrent(token)) return;
      if (data?.available === false) { setConnection({ ...data, forClient: client }); return; }
      if (data?.available !== true || data.project_id !== projectId || data.version !== 'mcp-connection@1'
          || !['powershell', 'sh'].includes(data.shell) || typeof data.instructions !== 'string' || !data.instructions
          || !['claude', 'codex'].every(key => typeof data.commands?.[key] === 'string' && data.commands[key])) throw new Error('connection_invalid');
      if (client === 'deepseek-harness' && (data.profiles?.['deepseek-harness']?.version !== 'deepseek-memory-profile@1'
          || data.profiles['deepseek-harness'].format !== 'json' || typeof data.profiles['deepseek-harness'].text !== 'string'
          || !data.profiles['deepseek-harness'].text)) throw new Error('connection_invalid');
      if (client === 'deepseek-harness') {
        const profile = JSON.parse(data.profiles['deepseek-harness'].text);
        const entry = profile?.[0]?.insert?.[0], config = entry?.config;
        const absolute = path => typeof path === 'string' && /^(?:[A-Za-z]:[\\/]|\/)/.test(path);
        if (!Array.isArray(profile) || profile.length !== 1 || Object.keys(profile[0] || {}).length !== 1
            || !Array.isArray(profile[0].insert) || profile[0].insert.length !== 1
            || entry.id !== 'memory-mcp' || entry.name !== '@deepseek-ai/dsh-mcp-client'
            || config?.serverName !== 'chriptmas-memory' || config.transport !== 'stdio'
            || !absolute(config.command) || !Array.isArray(config.args) || config.args.length !== 2
            || config.args[0] !== '-I' || !absolute(config.args[1]) || !/[\\/]mcp\.py$/i.test(config.args[1])
            || !config.env || Object.keys(config.env).length !== 5
            || !['DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'CODEX_API_KEY'].every(key => config.env[key] === '')
            || config.failOnStartupError !== true || config.reconnect?.enabled !== false) throw new Error('connection_invalid');
        const backend = new URL(config.env.CHRIPTMAS_MCP_BACKEND_URL);
        if (backend.username || backend.password || backend.search || backend.hash || backend.pathname !== '/'
            || !(backend.protocol === 'https:' || backend.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(backend.hostname))) throw new Error('connection_invalid');
      }
      setConnection({ ...data, forClient: client });
    }).catch(() => { if (gate.isCurrent(token)) setReadError('读取未完成 · 重试'); });
    return () => { controller.abort(); copyRequest.current?.abort(); gate.invalidate('connection'); gate.invalidate('copy'); };
  }, [api, projectId, expanded, client, retry, value.revision, plugins?.revision]);
  const dailyLimit = Number(limit), validLimit = limit !== '' && Number.isInteger(dailyLimit) && dailyLimit > 0;
  function save(changes) {
    run(() => api.saveExternalAgent({ allow_remote: value.allow_remote, include_profile: value.include_profile,
      daily_limit: value.daily_limit, clients: { ...value.clients }, ...changes, expected_revision: value.revision }));
  }
  function savePlugin(id, enabled) {
    if (!plugins || busy) return;
    run(() => api.saveExecutorPlugins({ expected_revision: plugins.revision,
      expected_client_settings_revision: plugins.client_settings_revision,
      plugins: Object.fromEntries(plugins.plugins.map(plugin => [plugin.id, { enabled: plugin.id === id ? enabled : plugin.enabled }])) }));
  }
  async function copy(kind) {
    if (busy || copying || !plugins || !connection?.available || connection.forClient !== client || connection.project_id !== projectId) return;
    const token = gate.issue('copy', scope);
    if (!token) return;
    if (kind === 'snapshot' && (!value.allow_remote || !plugins?.plugins.some(plugin => plugin.client === client && plugin.enabled && plugin.capabilities?.memory))) return;
    const controller = new AbortController(); copyRequest.current = controller;
    let phase = kind === 'snapshot' ? 'generate' : 'copy';
    setCopying(true); setCopied(null); setCopyError(''); setSnapshotTime(null);
    try {
      let text = kind === 'command' ? client === 'deepseek-harness' ? connection.profiles[client].text : connection.commands[client] : connection.instructions;
      let generatedAt = null;
      if (kind === 'snapshot') {
        const data = await api.snapshot({ client, project_id: projectId, budget: 3000 }, controller.signal);
        if (!gate.isCurrent(token)) return;
        if (data?.version !== 'external-snapshot@1' || data.project_id !== projectId || data.client !== client
            || typeof data.generated_at !== 'string' || !Number.isFinite(Date.parse(data.generated_at))
            || typeof data.turn_id !== 'string' || !/^turn-[a-f0-9]{32}$/.test(data.turn_id)
            || typeof data.text !== 'string'
            || !data.text.startsWith('<!-- chriptmas-memory:external-snapshot@1:begin -->\n')
            || !data.text.endsWith('\n<!-- chriptmas-memory:external-snapshot@1:end -->')) throw new Error('snapshot_invalid');
        text = data.text; generatedAt = data.generated_at; phase = 'copy';
      }
      await navigator.clipboard.writeText(text);
      if (gate.isCurrent(token)) {
        setCopied(kind);
        if (generatedAt !== null) setSnapshotTime({ generatedAt, projectId, client, revision: value.revision });
      }
    }
    catch { if (gate.isCurrent(token)) setCopyError(phase === 'generate' ? '生成未完成 · 重试' : '复制未完成 · 重试'); }
    finally {
      if (copyRequest.current === controller) copyRequest.current = null;
      if (gate.isCurrent(token)) setCopying(false);
    }
  }
  return <section className="settings-item" aria-label="外部 agent" aria-describedby={retentionId} title={retention}>
    <span id={retentionId} hidden>{retention}</span>
    <Row title="外部 agent" expanded={expanded} onOpen={onOpen}
      trailing={<Switch label="外部 agent外发" checked={value.allow_remote} disabled={busy} onChange={allow_remote => save({ allow_remote })}/>}/>
    {expanded && <div className="settings-expand">
      <Row readOnly title="带上画像" trailing={<Switch label="带上画像" checked={value.include_profile} disabled={busy} onChange={include_profile => save({ include_profile })}/>}/>
      <form onSubmit={event => { event.preventDefault(); if (validLimit && !busy) save({ daily_limit: dailyLimit }); }}>
        <label>每日上限<input type="number" min="1" step="1" aria-label="每日上限" value={limit} disabled={busy} onChange={event => setLimit(event.target.value)}/></label>
        <button type="submit" aria-label="保存每日上限" disabled={busy || !validLimit}>保存</button>
      </form>
      {pluginError && <p role="alert">{pluginError} <button type="button" onClick={() => setRetry(old => old + 1)}>重试</button></p>}
      {CLIENTS.map(({ id, client: key }) => {
        const label = EXECUTOR_LABELS[id], plugin = plugins?.plugins.find(entry => entry.id === id);
        const state = !plugin ? pluginError ? '暂不可用' : '读取中' : !plugin.installed ? '未安装' : !plugin.enabled ? '未启用'
          : plugin.available && plugin.capabilities?.task ? '可用' : plugin.capabilities?.memory ? '仅记忆' : '不可用';
        return <div key={key}>
        <Row title={label} expanded={client === key} onOpen={() => setClient(old => old === key ? null : key)}
          meta={plugin?.runtime_version ? `${state} · ${plugin.runtime_version}` : state}
          trailing={<Switch label={`${label}启用`} checked={plugin?.enabled === true} disabled={busy || !plugin} onChange={enabled => savePlugin(id, enabled)}/>}/>
        {client === key && <div className="settings-connection">
          {plugin?.version && <Row readOnly title="插件版本" meta={plugin.version}/>}
          {plugin && !plugin.capabilities?.task && <div className="settings-test"><span>{executorReason(plugin.reason, id)}</span>{id === 'deepseek-harness' && !plugin.has_key && <button type="button" onClick={onConfigureGeneration}>去配置</button>}</div>}
          {readError && <p role="alert">{readError} <button type="button" aria-label="重试连接说明" onClick={() => setRetry(old => old + 1)}>重试</button></p>}
          {!connection && !readError && <span role="status">读取中</span>}
          {connection?.forClient === key && connection.available === false && <p>连接暂不可用</p>}
          {connection?.forClient === key && connection.available && connection.project_id === projectId && <>
            <div className="settings-connection-title">连接说明 <span>{connection.shell === 'powershell' ? 'PowerShell' : 'sh'}</span></div>
            <div className="settings-connection-block"><button type="button" aria-label={`复制${label}${key === 'deepseek-harness' ? '配置' : '命令'}`} disabled={copying} onClick={() => copy('command')}>{copied === 'command' ? '已复制' : '复制'}</button><pre>{key === 'deepseek-harness' ? connection.profiles[key].text : connection.commands[key]}</pre></div>
            <div className="settings-connection-block"><button type="button" aria-label="复制说明" disabled={copying} onClick={() => copy('instructions')}>{copied === 'instructions' ? '已复制' : '复制'}</button><pre>{connection.instructions}</pre></div>
            <div className="settings-test">
              <button type="button" aria-label="复制快照" disabled={copying || busy || !value.allow_remote || !plugin?.enabled || !plugin.capabilities?.memory} onClick={() => copy('snapshot')}>{copied === 'snapshot' ? '已复制' : '复制快照'}</button>
              {/* 时间只属于当前项目、客户端和设置修订下成功复制的快照。 */}
              {snapshotTime?.projectId === projectId && snapshotTime.client === key && snapshotTime.revision === value.revision
                && <time aria-label="快照生成时间" dateTime={snapshotTime.generatedAt}>{formatLocalShortDate(snapshotTime.generatedAt)}</time>}
            </div>
            {copyError && <p role="alert">{copyError}</p>}
          </>}
        </div>}
      </div>; })}
      {proxy && <>
        <span id={proxyId} hidden>{proxyDescription}</span>
        <Row title="代理接入" expanded={proxyOpen} onOpen={() => setProxyOpen(old => !old)}
          trailing={<span className="ui-row-meta" title={proxyDescription} aria-describedby={proxyId}>{address}</span>}/>
        {proxyOpen && [['claude', 'Claude Code'], ['codex', 'Codex']].map(([client, label]) => <Row key={`proxy-${client}`} readOnly title={label} sub="记录对话"
          trailing={<Switch label={`${label}记录对话`} checked={proxy.record_conversations[client]} disabled={busy}
            onChange={enabled => run(() => api.saveExternalProxy({ record_conversations: { ...proxy.record_conversations, [client]: enabled }, expected_revision: proxy.revision }))}/>}/>)}
      </>}
    </div>}
  </section>;
}
