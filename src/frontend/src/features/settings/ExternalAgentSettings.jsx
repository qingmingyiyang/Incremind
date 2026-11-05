import { useEffect, useId, useRef, useState } from 'react';
import { Row, Switch, Icon, StatusDot } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { deviceIdentity, isDeviceIdentity, readDeviceCredential, readUserSpace } from '../../shared/api/deviceTransport';
import { formatLocalShortDate } from '../../shared/lib/time';
import { desktopFileAccessNotice, readExecutorPlugins } from '../workbench/ExternalExecutorPicker';
import { EXTERNAL_CLIENTS, readExternalConnection, externalReadiness, clientConnectionText, harnessAuthenticationText, proxyClientConfiguration, proxyConfigurationMessage } from './externalAgentConnection';
import './ExternalAgentSettings.css';
import { ExternalMemoryAccess } from './ExternalMemoryAccess';

export function ExternalAgentSettings({ projectId, value, proxy, expanded, onOpen, onConfigureGeneration, api, run, busy }) {
  const retentionId = useId(), retention = '外部 agent 可能保留交出的内容';
  const guideId = useId(), [proxyOpen, setProxyOpen] = useState(true);
  const [proxyShell, setProxyShell] = useState('powershell');
  const [limit, setLimit] = useState(String(value.daily_limit));
  const limitEdited = useRef(false), limitBaseline = useRef(value.revision);
  const [saved, setSaved] = useState(false), savedTimer = useRef(null);
  const [, refreshLimitReview] = useState(0);
  useEffect(() => () => clearTimeout(savedTimer.current), []);
  const [client, setClient] = useState('claude'), [connectionState, setConnectionState] = useState(null);
  const [readError, setReadError] = useState(''), [copyError, setCopyError] = useState('');
  const [retry, setRetry] = useState(0), [copying, setCopying] = useState(false), [copied, setCopied] = useState(null);
  const [snapshotTime, setSnapshotTime] = useState(null);
  const [pluginState, setPluginState] = useState(null), [pluginError, setPluginError] = useState('');
  const [identityEpoch, setIdentityEpoch] = useState(0), [checkedAt, setCheckedAt] = useState(null);
  const [paths, setPaths] = useState({ python: '', adapter: '', shell: 'powershell' });
  const [conditionsFor, setConditionsFor] = useState(null);
  const [includeTarget, setIncludeTarget] = useState(false);
  const identity = deviceIdentity();
  const copyRequest = useRef(null), guideHeading = useRef(null), focusGuide = useRef(false);
  const gate = useRequestScope(`${projectId}\0${identityEpoch}`), scope = gate.scope;
  const plugins = pluginState?.scope === scope && pluginState.settingsRevision === value.revision ? pluginState.data : null;
  const connection = connectionState?.scope === scope && connectionState.settingsRevision === value.revision
    && connectionState.pluginRevision === plugins?.revision && connectionState.client === client ? connectionState.data : null;
  const selected = EXTERNAL_CLIENTS.find(item => item.client === client);
  const plugin = plugins?.plugins.find(item => item.id === selected.id);
  const selectedSpace = readUserSpace();
  const authentication = connection?.client_setup?.authentication;
  const targetAvailable = Boolean(selectedSpace?.user_id && authentication?.target_user_id === selectedSpace.user_id);
  useEffect(() => { if (focusGuide.current) { focusGuide.current = false; guideHeading.current?.focus(); } }, [client]);
  function invalidateCopy() { copyRequest.current?.abort(); copyRequest.current = null; gate.invalidate('copy'); setCopying(false); setCopied(null); setCopyError(''); setSnapshotTime(null); }
  useEffect(() => { setPaths({ python: '', adapter: '', shell: 'powershell' }); setIncludeTarget(false); }, [scope]);
  useEffect(() => {
    const changed = () => {
      for (const channel of ['plugins', 'plugin-save', 'connection', 'copy', 'limit']) gate.invalidate(channel);
      copyRequest.current?.abort(); copyRequest.current = null; setPluginState(null); setConnectionState(null); setReadError(''); setPluginError('');
      setCheckedAt(null); setCopying(false); setCopied(null); setSnapshotTime(null); setCopyError(''); setSaved(false);
      limitEdited.current = false; setLimit('');
      setIdentityEpoch(old => old + 1);
    };
    window.addEventListener('chriptmas-user-space', changed); window.addEventListener('chriptmas-device-required', changed);
    return () => { window.removeEventListener('chriptmas-user-space', changed); window.removeEventListener('chriptmas-device-required', changed); };
  }, [gate]);
  useEffect(() => { if (!limitEdited.current) { setLimit(String(value.daily_limit)); limitBaseline.current = value.revision; } }, [scope, value.revision, value.daily_limit]);
  useEffect(() => {
    const token = gate.issue('plugins', scope), controller = new AbortController();
    setPluginState(null); setPluginError(''); setCheckedAt(null); invalidateCopy();
    api.executorPlugins(controller.signal).then(readExecutorPlugins).then(data => {
      if (gate.isCurrent(token)) { setPluginState({ scope, settingsRevision: value.revision, data }); setCheckedAt(new Date().toISOString()); }
    }).catch(() => { if (gate.isCurrent(token)) setPluginError('读取未完成 · 重试'); });
    return () => { controller.abort(); gate.invalidate('plugins'); };
  }, [api, scope, retry, value.revision]);
  useEffect(() => {
    if (!plugins) return undefined;
    const token = gate.issue('connection', scope), controller = new AbortController();
    setConnectionState(null); setReadError(''); invalidateCopy();
    api.connection(projectId, controller.signal).then(data => {
      if (!gate.isCurrent(token)) return;
      const validated = readExternalConnection(data, projectId, client);
      setConnectionState({ scope, settingsRevision: value.revision, pluginRevision: plugins.revision, client, data: validated });
    }).catch(() => { if (gate.isCurrent(token)) setReadError('读取未完成 · 重试'); });
    return () => { controller.abort(); copyRequest.current?.abort(); gate.invalidate('connection'); gate.invalidate('copy'); };
  }, [api, projectId, scope, client, retry, value.revision, plugins?.revision]);
  const dailyLimit = Number(limit), validLimit = limit !== '' && Number.isInteger(dailyLimit) && dailyLimit > 0;
  const limitChanged = limitEdited.current && limitBaseline.current !== value.revision && dailyLimit !== value.daily_limit;
  function save(changes) {
    if (!isDeviceIdentity(identity) || !gate.isScopeCurrent(scope) || busy) return;
    run(() => api.saveExternalAgent({ allow_remote: value.allow_remote, include_profile: value.include_profile,
      daily_limit: value.daily_limit, clients: { ...value.clients }, ...changes, expected_revision: value.revision }));
  }
  function savePlugin(id, enabled) {
    if (!plugins || busy || !isDeviceIdentity(identity) || !gate.isScopeCurrent(scope)) return;
    const token = gate.issue('plugin-save', scope); if (!token) return;
    gate.invalidate('plugins'); gate.invalidate('connection'); invalidateCopy();
    run(async () => {
      try {
        await api.saveExecutorPlugins({ expected_revision: plugins.revision,
          expected_client_settings_revision: plugins.client_settings_revision,
          plugins: Object.fromEntries(plugins.plugins.map(plugin => [plugin.id, { enabled: plugin.id === id ? enabled : plugin.enabled }])) });
      } finally {
        // 插件修订独立于外发设置，保存及冲突后都按真实插件接口重新读取。
        if (gate.isCurrent(token) && isDeviceIdentity(identity)) {
          setPluginState(null); setConnectionState(null); setCheckedAt(null);
          setRetry(old => old + 1);
        }
      }
    });
  }
  async function copy(kind) {
    if (busy || copying || copyRequest.current || !plugins || !connection?.available || connection.project_id !== projectId || !isDeviceIdentity(identity)) return;
    if (kind === 'command' && !commandText) return;
    if (kind === 'authentication' && !authenticationText) return;
    const token = gate.issue('copy', scope);
    if (!token) return;
    if (kind === 'snapshot' && (connection.remote || !selected.snapshot || !value.allow_remote || !plugin?.enabled || !plugin.capabilities?.memory)) return;
    const controller = new AbortController(); copyRequest.current = controller;
    let phase = kind === 'snapshot' ? 'generate' : 'copy';
    setCopying(true); setCopied(null); setCopyError(''); setSnapshotTime(null);
    try {
      let text = kind === 'command' ? commandText : kind === 'authentication' ? authenticationText : connection.instructions;
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
  async function copyProxyConfiguration(clientId, kind) {
    if (busy || copying || copyRequest.current || !proxy || !isDeviceIdentity(identity)) return;
    const token = gate.issue('copy', scope); if (!token) return;
    const controller = new AbortController(); copyRequest.current = controller;
    setCopying(true); setCopied(null); setCopyError('');
    try {
      const configuration = proxyClientConfiguration(connection, clientId, proxyShell);
      if (!configuration.available) return;
      await navigator.clipboard.writeText(kind === 'base' ? configuration.base_url : configuration.text);
      if (gate.isCurrent(token)) setCopied(`proxy-${clientId}-${kind}`);
    }
    catch { if (gate.isCurrent(token)) setCopyError('复制未完成 · 重试'); }
    finally { if (copyRequest.current === controller) copyRequest.current = null; if (gate.isCurrent(token)) setCopying(false); }
  }
  function refreshStatus() { if (busy || copying) return; invalidateCopy(); setConnectionState(null); setReadError(''); setRetry(old => old + 1); }
  function selectClient(next) { if (busy) return; invalidateCopy(); if (next === client) guideHeading.current?.focus(); else { focusGuide.current = true; setClient(next); } }
  function actOnCondition(item, action) {
    if (busy || copying || !action) return;
    if (action.kind === 'egress') save({ allow_remote: true });
    else if (action.kind === 'enable') savePlugin(item.id, true);
    else if (action.kind === 'generation') onConfigureGeneration();
    else { selectClient(item.client); setConditionsFor(item.client); }
  }
  let commandText = null, pathError = '';
  try { commandText = clientConnectionText(connection, client, paths); }
  catch (reason) { if (reason.message !== 'local_paths_invalid') throw reason; pathError = '填写本机绝对路径 · 适配器选择 mcp.py'; }
  const authenticationText = client === 'deepseek-harness' ? harnessAuthenticationText(connection, includeTarget && targetAvailable, selectedSpace?.user_id) : null;
  const canSnapshot = selected.snapshot && connection?.available && !connection.remote;
  return <section className="settings-item external-agent-settings" aria-label="外部 agent" aria-describedby={retentionId}>
    <span id={retentionId} hidden>{retention}</span>
    <Row dot={<span className="settings-glyph"><Icon name="related" size={18}/></span>} title={<span>交给外部 agent</span>} sub={<span aria-hidden="true">{value.allow_remote ? '外发已开启' : '外发已关闭'} · 每日 {value.daily_limit}</span>} expanded={expanded} onOpen={() => { if (!busy && !copying) onOpen(); }}
      trailing={<><Switch label="外部 agent外发" title="私密不交，每次写回执" checked={value.allow_remote} disabled={busy || copying} onChange={allow_remote => save({ allow_remote })}/><button type="button" className="settings-chevron" aria-label="接入选项" aria-expanded={expanded} disabled={busy || copying} onClick={onOpen}><Icon name="chevron-down" size={16}/></button></>}/>
    {expanded && <form className="external-agent-options" onSubmit={event => {
      event.preventDefault(); if (busy || copying || !validLimit || limitChanged || !isDeviceIdentity(identity)) return;
      const token = gate.issue('limit', scope); if (!token) return;
      clearTimeout(savedTimer.current); setSaved(false);
      run(async () => {
        if (dailyLimit !== value.daily_limit) await api.saveExternalAgent({ allow_remote: value.allow_remote, include_profile: value.include_profile,
          daily_limit: dailyLimit, clients: { ...value.clients }, expected_revision: limitBaseline.current });
        if (gate.isCurrent(token)) { limitEdited.current = false; setSaved(true); savedTimer.current = setTimeout(() => setSaved(false), 1400); }
      });
    }}><Row readOnly title="带上画像" trailing={<Switch label="带上画像" checked={value.include_profile} disabled={busy || copying} onChange={include_profile => save({ include_profile })}/>}/><div className="external-agent-limit"><label>每日上限<input type="number" min="1" step="1" aria-label="每日上限" value={limit} disabled={busy || copying} onChange={event => { if (!limitEdited.current) limitBaseline.current = value.revision; limitEdited.current = true; setSaved(false); setLimit(event.target.value); }}/></label><button type="submit" disabled={busy || copying || !validLimit || limitChanged}>{saved ? '已保存' : '保存'}</button></div>{limitChanged && <p role="status" className="external-agent-note">设置已变化 · 当前上限 {value.daily_limit} <button type="button" disabled={busy || copying} onClick={() => { limitBaseline.current = value.revision; refreshLimitReview(old => old + 1); }}>重新核对</button></p>}</form>}
    <div className="external-agent-health"><h3>客户端</h3>{checkedAt && plugins && <time title="读取已有注册与设置事实" dateTime={checkedAt}>{formatLocalShortDate(checkedAt)}</time>}<button type="button" disabled={busy || copying || !plugins && !pluginError} onClick={refreshStatus}><Icon name="review-reask" size={14}/>刷新状态</button></div>
    {pluginError && <p role="alert">{pluginError} <button type="button" disabled={busy || copying} onClick={refreshStatus}>重试</button></p>}
    <div className="external-agent-clients">{EXTERNAL_CLIENTS.map(item => {
      const entry = plugins?.plugins.find(row => row.id === item.id), readiness = externalReadiness(entry, value.allow_remote);
      return <article className={`external-agent-client ${item.client === client ? 'is-selected' : ''}`} key={item.id} aria-label={`${item.label}接入`}>
        <header><span className="external-agent-mark" aria-hidden="true">{item.mark}</span><h4>{item.label}</h4><Switch label={`${item.label}启用`} checked={entry?.enabled === true} disabled={busy || copying || !entry} onChange={enabled => savePlugin(item.id, enabled)}/></header>
        <Row readOnly title="记忆接入" dot={<StatusDot state="unverified"/>} meta={pluginError ? '读取未完成' : readiness.memory}/>
        <Row readOnly title="执行任务" dot={<StatusDot state={entry?.available && entry.capabilities.task ? 'done' : 'unverified'}/>} meta={pluginError ? '读取未完成' : readiness.task}/>
        <div className="external-agent-client-version">运行版本 <span>{entry?.runtime_version || '—'}</span></div>
        {desktopFileAccessNotice(entry) && <p className="external-agent-note">能访问这台电脑上的文件</p>}
        {readiness.missing.length > 0 && <div className="external-agent-missing-block"><span>执行条件</span><ul className="external-agent-missing" aria-label={`${item.label}执行任务待完成条件`}>{readiness.missing.map(condition => <li key={condition}>{condition}</li>)}</ul></div>}
        <div className="external-agent-card-actions"><button type="button" className="external-agent-guide-button" aria-controls={guideId} aria-expanded={item.client === client} disabled={busy} onClick={() => selectClient(item.client)}>连接说明<Icon name="arrow-down" size={14}/></button>{readiness.action && <button type="button" disabled={busy || copying} onClick={() => actOnCondition(item, readiness.action)}>{readiness.action.label}</button>}</div>
      </article>;
    })}</div>
    <section className="external-agent-guide" id={guideId} aria-label={`${selected.label}连接说明`}><div className="external-agent-guide-heading"><h3 tabIndex={-1} ref={guideHeading}>{selected.label} · 连接说明</h3><span>客户端连接待验证</span></div>
      {!plugins && !pluginError && <span role="status">读取客户端状态</span>}
      {readError && <p role="alert">指引未读取 · <button type="button" aria-label="重试连接说明" disabled={busy || copying} onClick={refreshStatus}>重试</button></p>}
      {plugins && !connection && !readError && <span role="status">读取接入指引</span>}
      {connection?.available === false && <p className="external-agent-note">{connection.reason === 'mcp_connection_https_required' ? '远程接入需要 HTTPS · 使用安全地址后刷新' : '接入指引暂不可用 · 刷新状态'}</p>}
      {connection?.available && <>
        <div className="external-agent-location"><StatusDot state="done"/><span>{connection.remote ? '当前应用在服务器' : '当前应用在本机'}</span><span>指引已读取</span></div>
        <ol className="external-agent-steps"><li>{connection.remote ? '在运行客户端的电脑安装本项目适配器，使用该电脑的 Python 与适配器路径。' : `在运行 ${selected.label} 的这台电脑，按下方${client === 'deepseek-harness' ? '配置接入记忆' : '命令添加 MCP'}。`}</li><li>{connection.remote ? client === 'deepseek-harness' ? '填好本机路径并合并 JSON 配置，再按客户端鉴权区域合并固定 YAML 环境引用。' : '按服务器指引设置设备授权，再添加 MCP；设备钥匙通过环境传递。' : client === 'deepseek-harness' ? '把配置加入独立的 Harness 配置后重新加载客户端。' : `在 ${connection.shell === 'powershell' ? 'PowerShell' : '终端'} 中执行命令，再重新加载客户端。`}</li><li>在客户端检查 chriptmas-memory 工具，再按下方说明调用；真实连接结果以客户端返回为准。</li></ol>
        {connection.remote && connection.client_setup && <div className="external-agent-local-paths" role="group" aria-label="客户端电脑路径">{client !== 'deepseek-harness' && <label className="external-agent-shell">客户端终端<select aria-label="客户端终端" value={paths.shell} disabled={busy} onChange={event => { invalidateCopy(); setPaths(old => ({ ...old, shell: event.target.value })); }}><option value="powershell">PowerShell</option><option value="sh">sh</option></select></label>}<label>本机 Python<input aria-label="本机Python绝对路径" autoComplete="off" maxLength={4096} value={paths.python} disabled={busy} placeholder={connection.client_setup.python_placeholder} onChange={event => { invalidateCopy(); setPaths(old => ({ ...old, python: event.target.value })); }}/></label><label>本机适配器<input aria-label="本机mcp.py绝对路径" autoComplete="off" maxLength={4096} value={paths.adapter} disabled={busy} placeholder={connection.client_setup.adapter_placeholder} onChange={event => { invalidateCopy(); setPaths(old => ({ ...old, adapter: event.target.value })); }}/></label>{pathError && <p role="status">{pathError}</p>}</div>}
        {commandText && <div className="external-agent-code"><div><span>{client === 'deepseek-harness' ? 'JSON 配置' : (connection.remote ? paths.shell : connection.shell) === 'powershell' ? 'PowerShell 命令' : '终端命令'}</span><button type="button" aria-label={`复制${selected.label}${client === 'deepseek-harness' ? '配置' : '命令'}`} disabled={busy || copying} onClick={() => copy('command')}>{copied === 'command' ? '已复制' : '复制'}</button></div><pre>{commandText}</pre></div>}
        {connection.remote && <p className="external-agent-note">{connection.client_setup ? '本机路径仅在当前页面生成配置；下方说明使用当前服务器与项目。' : '按客户端电脑的本机安装路径配置适配器。'}</p>}
        {connection.remote && <section className="external-agent-authentication" aria-label="客户端鉴权"><h4>客户端鉴权</h4><p>在客户端启动环境设置 <code>CHRIPTMAS_DEVICE_KEY</code>，设备钥匙通过环境传递。</p>{client === 'codex' && <p>在 Codex 的 <code>mcp_servers.chriptmas-memory.env_vars</code> 中加入 <code>CHRIPTMAS_DEVICE_KEY</code>{includeTarget && targetAvailable && <> 和 <code>CHRIPTMAS_MCP_TARGET_USER</code></>}。</p>}{targetAvailable && <Row readOnly title="访问所选空间" meta={selectedSpace.name || selectedSpace.display_name || authentication.target_user_id} trailing={<Switch label="访问所选空间" checked={includeTarget} disabled={busy} onChange={enabled => { invalidateCopy(); setIncludeTarget(enabled); }}/>}/ >}{includeTarget && targetAvailable && <p>在客户端环境另设 <code>CHRIPTMAS_MCP_TARGET_USER={authentication.target_user_id}</code>。</p>}{client === 'deepseek-harness' && (authenticationText ? <><p>将以下固定引用合并到 <code>memory-mcp.config.env</code>，保留原地址与四项模型密钥空覆盖。</p><div className="external-agent-code"><div><span>YAML 环境引用</span><button type="button" aria-label="复制Harness鉴权引用" disabled={busy || copying} onClick={() => copy('authentication')}>{copied === 'authentication' ? '已复制' : '复制'}</button></div><pre>{authenticationText}</pre></div></> : <p>鉴权引用待提供 · 更新服务器后刷新状态</p>)}</section>}
        <div className="external-agent-code"><div><span>使用说明</span><button type="button" aria-label="复制说明" disabled={busy || copying} onClick={() => copy('instructions')}>{copied === 'instructions' ? '已复制' : '复制'}</button></div><pre>{connection.instructions}</pre></div>
        {canSnapshot && <div className="external-agent-snapshot"><button type="button" aria-label="复制快照" title="将当前项目的静态记忆交给客户端；每次生成保留回执" disabled={copying || busy || !value.allow_remote || !plugin?.enabled || !plugin.capabilities.memory} onClick={() => copy('snapshot')}>{copied === 'snapshot' ? '已复制' : '复制快照'}</button>{snapshotTime?.projectId === projectId && snapshotTime.client === client && snapshotTime.revision === value.revision && <time aria-label="快照生成时间" dateTime={snapshotTime.generatedAt}>{formatLocalShortDate(snapshotTime.generatedAt)}</time>}</div>}
        {copying && <span role="status" className="external-agent-copy-status"><StatusDot state="processing"/>复制中</span>}
        <details className="external-agent-task-setup" open={conditionsFor === client} onToggle={event => { if (gate.isScopeCurrent(scope)) setConditionsFor(event.currentTarget.open ? client : null); }}><summary>执行条件</summary><Row readOnly title="应用执行器" meta={plugin?.installed ? plugin.runtime_version || '版本待核' : '未发现'}/><Row readOnly title="适配版本" meta={plugin?.version || '—'}/><Row readOnly title="任务凭据" meta={plugin?.has_key ? '已配置' : '未配置'}/><Row readOnly title="隔离准入" meta={plugin?.available && plugin.capabilities.task ? '已满足' : plugin?.reason === 'external_host_isolation_unavailable' ? '暂不可用' : '待满足条件'}/>{!plugin?.has_key && (selected.credential ? <p>在启动应用的环境配置 <code>{selected.credential}</code>，再重启应用。</p> : <button type="button" disabled={busy || copying} onClick={onConfigureGeneration}>配置模型</button>)}<p>安装或更新执行器后重启应用，再刷新已有注册状态。</p><Row readOnly title="任务范围" meta="工作目录内 · 命令关闭"/><p>任务交付以实际成果与回执为准。</p></details>
      </>}
    </section>
    {proxy && <section className="external-agent-proxy"><Row title="代理接入" sub="官方 API 凭据透传" expanded={proxyOpen} onOpen={() => setProxyOpen(old => !old)}/>{proxyOpen && <div className="external-agent-proxy-guides">
      <p className="external-agent-note">配置运行客户端的本机。Claude 使用 ANTHROPIC_API_KEY，Codex 使用 CODEX_API_KEY。这里提供官方 API 接入，模型费用由对应服务商收取。</p>
      {connection?.remote && <p className="external-agent-note">启动环境另设 CHRIPTMAS_DEVICE_KEY。设备钥匙只用于本应用鉴权，模型凭据单独透传。配置绑定当前用户空间，切换空间后重新复制。</p>}
      <label>Claude 终端<select aria-label="代理配置终端" value={proxyShell} disabled={busy || copying} onChange={event => { invalidateCopy(); setProxyShell(event.target.value); }}><option value="powershell">PowerShell</option><option value="sh">sh / bash</option></select></label>
      {['claude', 'codex'].map(clientId => {
        const configuration = proxyClientConfiguration(connection, clientId, proxyShell), label = clientId === 'claude' ? 'Claude Code' : 'Codex';
        return <div className="external-agent-proxy-client" key={`proxy-${clientId}`}>
          <Row readOnly title={label} sub="记录对话" trailing={<Switch label={`${label}记录对话`} checked={proxy.record_conversations[clientId]} disabled={busy || copying} onChange={enabled => run(() => api.saveExternalProxy({ record_conversations: { ...proxy.record_conversations, [clientId]: enabled }, expected_revision: proxy.revision }))}/>}/>
          {configuration.available ? <><div className="external-agent-proxy-base"><code>{configuration.base_url}</code><button type="button" className="external-agent-proxy-copy" disabled={busy || copying} aria-label={`复制${label}代理地址`} onClick={() => copyProxyConfiguration(clientId, 'base')}>{copied === `proxy-${clientId}-base` ? '已复制' : '复制地址'}</button></div>
            <details className="external-agent-code"><summary>{clientId === 'claude' ? '当前终端环境配置' : '~/.codex/config.toml 配置'}</summary><pre>{configuration.text}</pre></details>
            <p className="external-agent-note">{clientId === 'claude' ? '保留已有模型密钥，在当前终端合并所需请求头后启动 Claude。' : '合并到本机用户配置，保留已有 model 选择；使用官方 OpenAI API Key，不使用订阅登录。'}</p>
            <button type="button" className="external-agent-proxy-copy" disabled={busy || copying} aria-label={`复制${label}代理配置`} onClick={() => copyProxyConfiguration(clientId, 'configuration')}>{copied === `proxy-${clientId}-configuration` ? '已复制' : '复制配置'}</button></>
            : <p className="external-agent-note" role="status">{readError ? '接入信息读取未完成，请刷新后重试。' : proxyConfigurationMessage(configuration)}</p>}
        </div>;
      })}
    </div>}</section>}
    {copyError && <p role="alert" className="external-agent-copy-error">{copyError}</p>}
    {expanded && !readDeviceCredential()?.mobile && <ExternalMemoryAccess projectId={projectId}/>}
  </section>;
}
