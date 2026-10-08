import { formatLocalShortDate } from '../../shared/lib/time';
import { formatModelCost } from '../../shared/lib/modelCost';
import { useEffect, useState } from 'react';
import { Row, Switch, StatusDot } from '../../shared/ui';
import { ChatGPTSubscriptionSettings } from '../../shared/ui/ChatGPTSubscriptionSettings';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { settingsApi } from './settingsApi';
import { ProjectConstraints } from './ProjectConstraints';
import { SettingsData } from './SettingsData';
import { SettingsDevices } from './SettingsDevices';
import { ExternalAgentSettings } from './ExternalAgentSettings';
import { SkillExports } from './SkillExports';
import { ProviderStoreField } from './ProviderStoreField';
import './Settings.css';

const groups = { model: '模型', privacy: '隐私', project: '项目', data: '数据', devices: '设备' };
const purposes = { generation: '生成', asr: '转写', embedding: '向量', rerank: '重排', search: '搜索' };
export default function SettingsPage({ projectId = 'default', initialSection = 'model', expandProject, api = settingsApi }) {
  const [section, setSection] = useState(initialSection in groups ? initialSection : 'model');
  const [value, setValue] = useState(null), [projects, setProjects] = useState([]), [refresh, setRefresh] = useState(0);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const gate = useRequestScope(projectId), scope = gate.scope;
  useEffect(() => {
    const token = gate.issue('load', scope);
    Promise.all([api.load(), api.projects()]).then(([settings, list]) => {
      if (gate.isCurrent(token)) { setValue(settings); setProjects(list.items || []); }
    }).catch(() => { if (gate.isCurrent(token)) setError('读取未完成 · 重试'); });
  }, [api, projectId, refresh]);
  useEffect(() => {
    const embedding = value?.model?.embedding, local = embedding?.local;
    if (local?.status !== 'installing' && !(embedding?.mode === 'local' && local?.status === 'ready' && local?.index && local.index.done < local.index.total && !local.index_reason_code)) return;
    const timer = setInterval(() => setRefresh(old => old + 1), 1000);
    return () => clearInterval(timer);
  }, [value?.model?.embedding?.mode, value?.model?.embedding?.local]);
  async function run(operation) {
    if (busy) return;
    const token = gate.issue('write', scope);
    if (!token) return;
    setBusy(true); setError('');
    try { await operation();  }
    catch (reason) { if (gate.isCurrent(token)) setError(reason.status === 409 || reason.code === 'revision_conflict' ? '设置已变化 · 刷新' : ['server_unavailable', 'invalid_response'].includes(reason.code) ? reason.message : '操作未完成 · 重试'); }
    finally { if (gate.isCurrent(token)) { setBusy(false); setRefresh(old => old + 1); } }
  }
  return <div className="settings-page"><nav aria-label="设置分组">{Object.entries(groups).map(([key, label]) => <button type="button" key={key} aria-pressed={section === key} onClick={() => setSection(key)}>{label}</button>)}</nav>
    <main><h1>{groups[section]}</h1>{error && <p role="alert">{error} <button type="button" onClick={() => { setError(''); setRefresh(old => old + 1); }}>重试</button></p>}{!value ? <span role="status">读取中</span> : <>
      {section === 'model' && <ModelRows key={projectId} projectId={projectId} model={value.model} externalAgent={value.external_agent} externalProxy={value.external_proxy} api={api} busy={busy} run={run} reload={() => setRefresh(old => old + 1)}/>}
      {section === 'privacy' && <PrivacyRows privacy={value.privacy} projects={projects} api={api} run={run} busy={busy}/>}
      {section === 'project' && <ProjectRows key={projectId} projects={projects} expandProject={expandProject} api={api} run={run} busy={busy}/>}
      {section === 'data' && <SettingsData projectId={projectId}/>}
      {section === 'devices' && <SettingsDevices key={projectId} projectId={projectId}/>}
    </>}</main></div>;
}

function ModelRows({ projectId, model, externalAgent, externalProxy, api, run, busy, reload }) {
  const [open, setOpen] = useState(null), [modeView, setModeView] = useState(null), [test, setTest] = useState({});
  const actualMode = model.generation_mode.mode, mode = modeView || actualMode;
  useEffect(() => { setModeView(null); }, [actualMode]);
  async function toggle(purpose, enabled) {
    const row = model[purpose] || {revision:0, enabled:false, allow_remote:false};
    if (purpose === 'asr') return run(() => enabled ? api.enableAsr() : api.disableAsr());
    if (purpose === 'generation' && actualMode === 'subscription') return run(() => api.toggleSubscription(enabled, row.revision));
    return run(() => api.saveModel({ purpose, baseUrl: row.base_url, model: row.model, apiKey: '', enabled: enabled || row.enabled === true, allowRemote: enabled, expectedRevision: row.revision }));
  }
  function pickMode(next) {
    setModeView(next === 'subscription' ? next : null);
    if (next !== 'subscription') run(() => api.saveMode({ mode: next, localEnabled: next === 'local' || model.generation_mode.local_enabled, localBaseUrl: model.generation_mode.local_base_url, expectedRevision: model.generation_mode.revision, clearSubscription: actualMode === 'subscription' }));
  }
  return <div>{Object.entries(purposes).map(([purpose, label]) => {
    if (purpose === 'embedding') return <EmbeddingRow key={purpose} row={model.embedding} open={open === purpose}
      onOpen={() => setOpen(old => old === purpose ? null : purpose)} api={api} run={run} busy={busy}/>;
    const row = model[purpose] || {}, asr = purpose === 'asr', local = purpose === 'generation' && actualMode === 'local';
    const enabled = asr ? row.enabled === true && row.egress_manifest?.consented === true : row.allow_remote === true;
    return <section key={purpose} className="settings-item"><Row title={label} expanded={open === purpose} onOpen={() => setOpen(old => old === purpose ? null : purpose)} trailing={<><span className="ui-row-meta">{local ? model.generation_mode.local_model : row.model || '—'}</span><StatusDot state={row.configured || row.status === 'ready' ? 'done' : 'unverified'}/><Switch label={`${label}外发`} checked={!local && enabled} disabled={busy || local} onChange={next => toggle(purpose, next)}/></>}/>
      {open === purpose && <div className="settings-expand">
        {purpose === 'generation' && <div role="group" aria-label="方式" className="settings-modes">{[['api', 'API'], ['subscription', '订阅'], ['local', '本机']].map(([key, name]) => <button type="button" key={key} aria-pressed={mode === key} disabled={busy || key === 'local' && !model.generation_mode.local_model_installed} onClick={() => pickMode(key)}>{name}</button>)}</div>}
        {purpose === 'generation' && mode === 'subscription' ? <ChatGPTSubscriptionSettings showEgressSwitch={false} onChanged={reload}/> : purpose === 'generation' && mode === 'local' ? <Row readOnly title={model.generation_mode.local_model} meta={model.generation_mode.local_model_installed ? '已安装' : '未安装'}/> : <ModelFields key={`${purpose}:${actualMode}:${row.revision}:${row.settings_revision}:${row.base_url}:${row.model}`} purpose={purpose} label={label} row={row} readonly={asr || purpose === 'generation' && actualMode !== 'api'} busy={busy} api={api} run={run}/>}
        {purpose === 'generation' && <FastModelField key={`${actualMode}:${row.model}:${row.revision}:${row.fast_model?.revision}:${row.subscription_binding?.selection}:${row.subscription_binding?.account}`} row={row} mode={actualMode} modeRevision={model.generation_mode.revision} api={api} run={run} busy={busy}/>}
        {purpose === 'generation' && <ProviderStoreField projectId={projectId} mode={mode} generationRevision={row.revision} modeRevision={model.generation_mode.revision} busy={busy}/>}
        {!asr && <PriceFields key={`${purpose}:${row.revision}:${row.pricing?.revision}:${row.model}`} purpose={purpose} label={label} row={row} busy={busy} api={api} run={run}/>}
        {asr && <><Row readOnly title="长音频" meta="60s · 重叠 5s"/><Row readOnly title="上限" meta={row.max_audio_bytes == null ? '—' : `${row.max_audio_bytes} B`}/></>}
        <div className="settings-test">{!asr && purpose !== 'search' && <button type="button" disabled={busy} title={asr ? 'asr_test_unavailable' : undefined} onClick={() => run(async () => {
          const start = performance.now(); const result = await api.testModel({ purpose });
          setTest(old => ({ ...old, [purpose]: result.status === 'complete' ? `✓ ${Math.round(performance.now() - start)}ms` : `✕ ${result.status || 'test_failed'}` }));
        })}>测试</button>}<span>{test[purpose]}</span><span>r{asr ? row.settings_revision ?? '—' : row.revision ?? '—'}</span></div>
      </div>}
    </section>;
  })}<VisionRow row={model.vision} open={open === 'vision'} onOpen={() => setOpen(old => old === 'vision' ? null : 'vision')} api={api} run={run} busy={busy}/>
    {externalAgent && <ExternalAgentSettings projectId={projectId} value={externalAgent} proxy={externalProxy} expanded={open === 'external-agent'} onOpen={() => setOpen(old => old === 'external-agent' ? null : 'external-agent')} api={api} run={run} busy={busy}/>}</div>;
}
function FastModelField({ row, mode, modeRevision, api, run, busy }) {
  const [model, setModel] = useState(row.fast_model?.configured ? row.fast_model.model : '');
  const [catalog, setCatalog] = useState([]), [error, setError] = useState(''), [retry, setRetry] = useState(0);
  useEffect(() => {
    if (mode !== 'subscription' || !row.configured) return undefined;
    let live = true; const controller = new AbortController();
    api.fastModels(controller.signal).then(value => { if (live) { setCatalog(value.items || []); setError(''); } })
      .catch(() => { if (live) setError('读取未完成 · 重试'); });
    return () => { live = false; controller.abort(); };
  }, [mode, row.configured, api, retry]);
  if (mode === 'local') return <Row readOnly title="快模型" meta={row.model || '主模型'}/>;
  return <form onSubmit={event => { event.preventDefault(); run(() => api.saveFastModel({ model: model.trim() || null,
    expectedRevision: row.fast_model?.revision ?? 0, expectedGenerationRevision: row.revision,
    expectedModeRevision: modeRevision })); }}>
    <label title="追问与检索；留空用主模型">快模型{mode === 'subscription'
      ? <select aria-label="快模型" value={model} disabled={busy || !row.configured} onChange={event => setModel(event.target.value)}>
        <option value="">主模型</option>{model && !catalog.some(item => item.id === model) && <option value={model}>{model}</option>}
        {catalog.map(item => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select>
      : <input aria-label="快模型" placeholder="主模型" value={model} disabled={busy} onChange={event => setModel(event.target.value)}/>}</label>
    {error && <p role="alert">{error} <button type="button" onClick={() => setRetry(old => old + 1)}>重试</button></p>}
    <button aria-label="保存快模型" disabled={busy || !!model && !row.configured}>保存</button>
  </form>;
}
function VisionRow({ row = { mode: 'local', mode_revision: 0, local: { status: 'unavailable' } }, open, onOpen, api, run, busy }) {
  const remote = row.mode === 'remote';
  return <section className="settings-item"><Row title="识图" expanded={open} onOpen={onOpen}
    trailing={<><div role="group" aria-label="识图方式" className="settings-modes settings-vision-modes">{[['local', '本机'], ['remote', '外接']].map(([mode, label]) => <button type="button" key={mode} disabled={busy} aria-pressed={row.mode === mode} onClick={() => run(() => api.saveVisionMode({ mode, expectedRevision: row.mode_revision ?? 0 }))}>{label}</button>)}</div>
      <StatusDot state={row.configured ? 'done' : 'unverified'}/>{remote && <Switch label="识图外发" checked={row.allow_remote === true} disabled={busy} onChange={allowRemote => run(() => api.saveModel({ purpose: 'vision', baseUrl: row.base_url, model: row.model, apiKey: '', enabled: row.enabled === true, allowRemote, expectedRevision: row.revision }))}/>}</>}/>
    {open && <div className="settings-expand">{remote ? <><ModelFields key={`vision:${row.revision}`} purpose="vision" label="识图" row={row} busy={busy} api={api} run={run}/><PriceFields key={`vision:${row.revision}:${row.pricing?.revision}`} purpose="vision" label="识图" row={row} busy={busy} api={api} run={run}/></> : <Row readOnly title={row.local?.provider || '—'} meta={row.local?.status === 'ready' ? '可用' : '不可用'}/>}</div>}
  </section>;
}
function EmbeddingRow({ row = {}, open, onOpen, api, run, busy }) {
  const [test, setTest] = useState('');
  const local = row.mode === 'local', state = row.local || {}, progress = state.status === 'installing' ? state.progress : state.index;
  const percent = progress?.total > 0 ? Math.min(100, Math.floor(progress.done / progress.total * 100)) : 0;
  const indexing = state.status === 'ready' && progress && progress.done < progress.total;
  return <section className="settings-item settings-vector-row"><Row title="向量" expanded={open} onOpen={onOpen}
    trailing={<><div role="group" aria-label="向量方式" className="settings-modes settings-vision-modes">{[['local', '本机'], ['remote', '外接']].map(([mode, label]) =>
      <button type="button" key={mode} aria-pressed={(row.mode || 'remote') === mode} disabled={busy}
        onClick={() => run(() => api.saveEmbeddingMode({ mode, expectedRevision: row.mode_revision ?? 0 }))}>{label}</button>)}</div>
      {!local ? <><span className="ui-row-meta">{row.model || '—'}</span><StatusDot state={row.configured ? 'done' : 'unverified'}/>
        <Switch label="向量外发" checked={row.allow_remote === true} disabled={busy} onChange={allowRemote => run(() => api.saveModel({ purpose: 'embedding', baseUrl: row.base_url, model: row.model, apiKey: '', enabled: allowRemote || row.enabled === true, allowRemote, expectedRevision: row.revision }))}/></> :
        state.status === 'installing' || indexing ? <span className="settings-vector-progress" title={state.index_reason_code || (indexing ? '已索引' : undefined)}>
          <span role="progressbar" aria-label={indexing ? '已索引' : '下载'} aria-valuemin={0} aria-valuemax={progress?.total || 0} aria-valuenow={progress?.done || 0}><span style={{ width: `${percent}%` }}/></span>
          <span>{indexing ? `${progress.done.toLocaleString()} / ${progress.total.toLocaleString()}` : `${percent}%`}</span></span> :
        state.status === 'failed' ? <><span className="settings-vector-failed" title={state.reason_code} aria-label="向量安装失败">✕</span>
          {state.can_install !== false && <button type="button" className="settings-vector-install" disabled={busy} onClick={() => run(() => api.installEmbedding(row.mode_revision ?? 0))}>重试</button>}</> :
        state.status === 'ready' ? <><span className="ui-row-meta">EmbeddingGemma 2 · {state.dims}</span><StatusDot state="done"/></> :
        <><span className="ui-row-meta" title="安装后约 579 MB，文字权重约 542 MB">≈ 1.53 GB</span>{state.can_install !== false && <button type="button" className="settings-vector-install" disabled={busy}
          title={state.reason_code} onClick={() => run(() => api.installEmbedding(row.mode_revision ?? 0))}>安装</button>}</>}
    </>}/>{open && <div className="settings-expand">{!local ? <><ModelFields key={`embedding:${row.revision}:${row.mode_revision}`} purpose="embedding" label="向量" row={row} api={api} run={run} busy={busy}/>
      <PriceFields key={`embedding:${row.revision}:${row.pricing?.revision}`} purpose="embedding" label="向量" row={row} api={api} run={run} busy={busy}/></> :
      state.index && <Row readOnly title="已索引" meta={`${state.index.done.toLocaleString()} / ${state.index.total.toLocaleString()}`} trailing={state.index_reason_code && <span className="settings-vector-failed" title={state.index_reason_code}>✕</span>}/>}
      {(!local || state.status === 'ready') && <div className="settings-test"><button type="button" disabled={busy} onClick={() => run(async () => {
        const start = performance.now(), result = await api.testModel({ purpose: 'embedding' });
        setTest(result.status === 'complete' ? `✓ ${Math.round(performance.now() - start)}ms` : `✕ ${result.status || 'test_failed'}`);
      })}>测试</button><span>{test}</span><span>r{row.revision ?? '—'}</span></div>}</div>}</section>;
}
function ModelFields({ purpose, label, row, readonly, busy, run, api }) {
  const capture = purpose === 'asr' && typeof globalThis.electronAPI?.captureCredential === 'function';
  const [baseUrl, setBaseUrl] = useState(row.base_url || row.endpoint || ''), [model, setModel] = useState(row.model || ''), [key, setKey] = useState('');
  return <form onSubmit={event => { event.preventDefault(); const secret = key; setKey(''); run(() => capture ? api.saveAsrKey(secret) : api.saveModel({ purpose, baseUrl, model, apiKey: secret, allowRemote: row.allow_remote === true, enabled: row.enabled === true, expectedRevision: row.revision })); }}>
    <label>地址{purpose === 'asr' ? <span className="ui-row-meta">{baseUrl || '—'}</span> : <input aria-label={`${label}地址`} value={baseUrl} disabled={readonly || busy} onChange={event => setBaseUrl(event.target.value)}/>}</label>
    <label>模型{purpose === 'asr' ? <span className="ui-row-meta">{model || '—'}</span> : <input aria-label={`${label}模型`} value={model} disabled={readonly || busy} onChange={event => setModel(event.target.value)}/>}</label>
    <label>密钥<input aria-label={`${label}密钥`} type="password" autoComplete="new-password" value={key} placeholder={row.has_api_key ? '已设置' : '未设置'} disabled={readonly && !capture || busy} onChange={event => setKey(event.target.value)}/></label>
    {(!readonly || capture) && <button type="submit" disabled={busy}>保存</button>}
  </form>;
}
function PriceFields({ purpose, label, row, busy, run, api }) {
  const fields = { input_per_million: '输入', output_per_million: '输出', cache_read_per_million: '缓存命中' };
  const pricing = row.pricing ?? {};
  const [rates, setRates] = useState(Object.fromEntries(Object.keys(fields).map(key => [key, pricing.rates?.[key] ?? ''])));
  const readonly = pricing.editable === false;
  return <form onSubmit={event => { event.preventDefault(); run(() => api.savePrices({ purpose,
    rates: Object.fromEntries(Object.entries(rates).map(([key, value]) => [key, value === '' ? null : String(value)])),
    expectedRevision: pricing.revision ?? 0, expectedConfigurationRevision: row.revision ?? 0 })); }}>
    <fieldset className="settings-prices"><legend>¥ / 百万 token</legend>
      {Object.entries(fields).map(([key, title]) => <label key={key}>{title}<input type="number" min="0" max="1000000" step="0.000001"
        aria-label={`${label}${title}单价`} placeholder="—" value={readonly ? pricing.rates?.[key] ?? '' : rates[key]} disabled={readonly || busy}
        onChange={event => setRates(old => ({ ...old, [key]: event.target.value }))}/></label>)}
    </fieldset>{!readonly && <button type="submit" disabled={busy}>保存单价</button>}
  </form>;
}
function PrivacyRows({ privacy, projects, api, run, busy }) {
  const [open, setOpen] = useState(null), [sources, setSources] = useState(null), [receipts, setReceipts] = useState(null), [add, setAdd] = useState('');
  const name = id => projects.find(row => row.id === id)?.name || id;
  async function show(key) { setOpen(old => old === key ? null : key); if (key === 'sources') await run(async () => setSources(await api.privateSources())); else await run(async () => setReceipts(await api.receipts())); }
  return <>
    <Row readOnly title="私密项目"/>
    <div className="settings-tags">{privacy.private_projects.map(id => <button type="button" disabled={busy} aria-label={`移除${name(id)}私密`} key={id} onClick={() => run(() => api.savePrivacy(privacy.private_projects.filter(value => value !== id), privacy.revision))}>{name(id)} ×</button>)}
      <select aria-label="添加私密项目" value={add} disabled={busy} onChange={event => { const id = event.target.value; setAdd(''); if (id) run(() => api.savePrivacy([...privacy.private_projects, id], privacy.revision)); }}><option value="">＋</option>{projects.filter(row => !privacy.private_projects.includes(row.id) && row.id !== 'me' && row.id !== 'inbox').map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select>
    </div>
    <Row title="私密资料" expanded={open === 'sources'} meta={sources ? String(sources.length) : '—'} onOpen={() => show('sources')}/>
    {open === 'sources' && <div className="settings-expand">{(sources || []).map(row => <Row readOnly key={`${row.type}:${row.source_id}`} title={row.title} sub={row.type === 'recognition' ? '认识' : '原件'} meta={name(row.project_id)} trailing={!row.inherited && Number.isInteger(row.source_revision) && Number.isInteger(row.policy_revision) && <button type="button" disabled={busy} onClick={() => run(async () => { await api.cancelPrivate(row); setSources(await api.privateSources()); })}>取消私密</button>}/>)}</div>}
    <Row title="外发记录" expanded={open === 'receipts'} meta={receipts ? String(receipts.length) : '—'} onOpen={() => show('receipts')}/>
    {open === 'receipts' && <div className="settings-table-scroll"><table><thead><tr>{['时间', '用途', '模型', '条目', '入/出', '花费'].map(label => <th key={label}>{label}</th>)}</tr></thead><tbody>{(receipts || []).map((row, i) => <tr key={i}><td>{formatLocalShortDate(row.at)}</td><td>{row.purpose || '—'}</td><td>{row.model || '—'}</td><td>{row.items ?? '—'}</td><td>{row.usage ? `${row.usage.input ?? '—'} / ${row.usage.output ?? '—'}` : '—'}</td><td>{formatModelCost(row.model_cost)}</td></tr>)}</tbody></table></div>}
  </>;
}
function ProjectRows({ projects, expandProject, api, run, busy }) {
  const [open, setOpen] = useState(expandProject || null), [newName, setNewName] = useState('');
  return <>{projects.map(row => <section className="settings-item" key={row.id}><Row title={row.name} expanded={open === row.id} meta={String(row.scenes?.length || 0)} onOpen={() => setOpen(old => old === row.id ? null : row.id)}/>
    {open === row.id && <section role="region" aria-label={`${row.name}设置`} className="settings-expand">
      {row.id !== 'me' && <ProjectFields key={`${row.id}:${row.revision}`} row={row} run={run} api={api} busy={busy}/>}
      {row.id !== 'inbox' && <ProjectConstraints key={row.id} projectId={row.id} api={api}/>}
      {row.id !== 'inbox' && <SkillExports key={`skills:${row.id}`} projectId={row.id} scenes={row.scenes || []}/>}
    </section>}
  </section>)}<form className="settings-new-project" onSubmit={event => { event.preventDefault(); if (newName.trim()) run(async () => { await api.createProject(newName.trim()); setNewName(''); }); }}><input aria-label="新建项目" value={newName} onChange={event => setNewName(event.target.value)}/><button disabled={busy || !newName.trim()}>新建项目</button></form></>;
}
function ProjectFields({ row, api, run, busy }) {
  const [name, setName] = useState(row.name), [scene, setScene] = useState('');
  return <><form onSubmit={event => { event.preventDefault(); run(() => api.saveProject(row, { name })); }}><label>名称<input aria-label="名称" value={name} onChange={event => setName(event.target.value)}/></label><button disabled={busy}>保存</button></form>
    <div className="settings-tags">{(row.scenes || []).map(value => <span key={value}>{value}</span>)}</div>
    <form onSubmit={event => { event.preventDefault(); if (scene.trim()) run(() => api.saveProject(row, { scenes: [...row.scenes, scene.trim()] })); }}><label>场景<input aria-label="添加场景" value={scene} onChange={event => setScene(event.target.value)}/></label><button disabled={busy || !scene.trim()}>添加</button></form>
    <Row readOnly title="私密" trailing={<Switch label="私密" checked={row.private === true} disabled={busy} onChange={value => run(() => api.saveProject(row, { private: value }))}/>}/>
  </>;
}
