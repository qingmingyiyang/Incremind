import { useEffect, useRef, useState } from 'react';
import { Icon, ProgressDots, StatusDot, Switch } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { displayScopedText, formatScopeTag } from '../../shared/lib/scopeTag';
import { formatLocalShortDate } from '../../shared/lib/time';
import { sendSignal, signalEpoch } from '../../shared/signalsApi';
import { REMOTE_PROCESSING_HINT } from '../../shared/lib/privacyMessages';
import { libraryApi } from '../library/libraryApi';
import { InsightSource } from '../../shared/ui/InsightSource';
import { settingsApi } from '../settings/settingsApi';
import { ModelConnectionForm, PROVIDER_PRESETS } from '../settings/ProviderConnections';
import { workbenchApi } from './workbenchApi';

const templates = [
  { name: '工作', scenes: ['会议', '周报', '客户'] },
  { name: '学习', scenes: ['课程', '读书', '笔记'] },
  { name: '写作', scenes: ['选题', '素材', '草稿'] },
  { name: '生活', scenes: ['运动', '健康', '家务'] },
  { name: '空白', scenes: [] },
];
function generationReady(settings) {
  const row = settings.model?.generation, mode = settings.model?.generation_mode;
  return mode?.mode === 'local' ? mode.local_enabled === true && mode.local_model_installed === true
    : row?.configured === true && row.enabled === true && row.allow_remote === true;
}

export function WorkbenchHome({ projectId, projects, threads, onNavigate, onSample, onProjectCreated, hasDraft }) {
  const [identityEpoch, setIdentityEpoch] = useState(0);
  const actor = deviceIdentity(), userSpace = userStorageKey(projectId);
  const requests = useRequestScope(`${userSpace}:${identityEpoch}`), scope = requests.scope;
  const [loaded, setLoaded] = useState(null), [refresh, setRefresh] = useState(0);
  const [settingsRead, setSettingsRead] = useState(null), [settingsRefresh, setSettingsRefresh] = useState(0);
  const [deciding, setDeciding] = useState(false), [error, setError] = useState('');
  const operation = useRef(null);
  const currentRequest = token => requests.isCurrent(token) && isDeviceIdentity(actor) && userStorageKey(projectId) === userSpace;
  useEffect(() => { operation.current = null; setDeciding(false); setError(''); }, [scope]);
  useEffect(() => {
    const changed = () => setIdentityEpoch(value => value + 1);
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => { window.removeEventListener('chriptmas-user-space', changed); window.removeEventListener('chriptmas-device-required', changed); };
  }, []);
  useEffect(() => {
    const token = requests.issue('settings', scope);
    setSettingsRead({ scope, loading: true });
    settingsApi.load().then(settings => {
      if (currentRequest(token)) setSettingsRead({ scope, settings });
    }).catch(() => { if (currentRequest(token)) setSettingsRead({ scope, error: true }); });
    return () => requests.invalidate('settings');
  }, [scope, settingsRefresh]);
  useEffect(() => {
    const token = requests.issue('home', scope), controller = new AbortController();
    libraryApi.list('insight', projectId, { signal: controller.signal }).then(insights => {
      if (currentRequest(token)) setLoaded({ scope, items: Array.isArray(insights?.items) ? insights.items.filter(item => item.state === 'pending') : null });
    }).catch(() => { if (currentRequest(token)) setLoaded({ scope, items: null }); });
    return () => { controller.abort(); requests.invalidate('home'); };
  }, [projectId, scope, refresh]);
  const value = loaded?.scope === scope ? loaded : null;
  const pending = value?.items, current = pending?.[0];
  useEffect(() => {
    if (current) sendSignal({ kind: 'view', project_id: projectId,
      object: { kind: 'insight', id: current.id, revision: current.revision } }, signalEpoch());
  }, [projectId, current?.id, current?.revision]);
  async function decide(action) {
    if (!current || operation.current || !requests.isScopeCurrent(scope) || !isDeviceIdentity(actor)) return;
    const token = requests.issue('decide', scope); operation.current = token;
    setDeciding(true); setError('');
    try {
      await workbenchApi.insight(projectId, current, action);
      if (currentRequest(token)) setLoaded(old => old?.scope === scope
        ? { ...old, items: old.items.filter(item => item.id !== current.id) } : old);
    } catch (failure) {
      if (currentRequest(token)) setError(failure.status === 409 ? '内容已变化 · 刷新' : '操作未完成 · 重试');
    } finally {
      if (operation.current === token) operation.current = null;
      if (currentRequest(token)) setDeciding(false);
    }
  }
  const project = projects.find(item => item.id === projectId);
  return <>
    {!hasDraft && <div className="workbench-examples" aria-label="试一试">
      {[['remember', '记住链接', 'https://'], ['ask', '问上周的会', '上周会议讨论了什么？'], ['do', '写本周周报', '帮我把本周会议整理成周报']].map(([intent, label, text]) =>
        <button key={intent} type="button" onClick={() => onSample({ text, intent })}><Icon name={intent} size={14}/>{label}</button>)}
    </div>}
    {settingsRead?.scope === scope && settingsRead.error && <div role="alert" className="workbench-error">设置未读取 <button type="button" onClick={() => setSettingsRefresh(old => old + 1)}>重试</button></div>}
    {settingsRead?.scope === scope && settingsRead.settings && !threads.length && <WorkbenchStart key={`${userSpace}:${identityEpoch}`} settings={settingsRead.settings} projects={projects} projectId={projectId}
      onNavigate={onNavigate} onProjectCreated={onProjectCreated} onSample={onSample} onRefresh={() => setSettingsRefresh(old => old + 1)}/>}
    <div className="workbench-home-grid">
      <section aria-label="待确认">
        <h2><StatusDot state="pending"/>待确认<span className="ui-count">{pending?.length ?? '—'}</span>
          <button type="button" onClick={() => onNavigate?.('library', { layer: 'insight', filter: 'pending_memory' })}>逐条确认 <Icon name="arrow-up-right" size={12}/></button>
        </h2>
        {!value ? <StatusDot state="processing"/> : !pending ? <div role="alert" className="workbench-home-empty">读取未完成 <button type="button" onClick={() => setRefresh(old => old + 1)}>重试</button></div>
          : current ? <article className="workbench-pending-card">
            <div className="workbench-pending-meta"><span>1 / {pending.length}</span><span>{formatScopeTag(project?.name || projectId, current.scene || null)}</span></div>
            <p>{current.text}</p>
            <InsightSource source={current.source} onReference={onNavigate ? reference => onNavigate('workbench', reference) : undefined}/>
            <div className="workbench-pending-meta">{current.created_at && <time dateTime={current.created_at}>{formatLocalShortDate(current.created_at)}</time>}
              <button type="button" onClick={() => onNavigate?.('library', { layer: 'insight', insight_id: current.id, filter: 'pending_memory' })}>查看原句 <Icon name="arrow-up-right" size={12}/></button>
            </div>
            <div className="workbench-pending-actions"><button type="button" disabled={deciding} onClick={() => decide('drop')}><Icon name="close" size={14}/>丢弃</button>
              <button type="button" className="workbench-primary" disabled={deciding} onClick={() => decide('confirm')}><Icon name="check" size={14}/>确认</button></div>
          </article> : <div className="workbench-home-empty"><Icon name="check" size={24}/><span className="ui-count">0</span></div>}
        {error && <div role="alert" className="workbench-error">{error} <button type="button" onClick={() => { setError(''); setRefresh(old => old + 1); }}>刷新</button></div>}
      </section>
      <section aria-label="最近对话">
        <h2>最近对话{threads.length > 4 && <button type="button" onClick={() => document.querySelector('.workbench-history select')?.focus()}>全部 <Icon name="arrow-up-right" size={12}/></button>}</h2>
        {threads.length ? threads.slice(0, 4).map(thread => <button key={thread.id} type="button" className="workbench-recent"
          onClick={() => onNavigate?.('workbench', { thread_id: thread.id })}><Icon name="workbench" size={14}/><span>{displayScopedText(thread.title)}</span>
          {thread.updated_at && <time dateTime={thread.updated_at}>{formatLocalShortDate(thread.updated_at)}</time>}</button>)
          : <div className="workbench-home-empty"><span className="ui-count">0</span></div>}
      </section>
    </div>
  </>;
}

function WorkbenchStart({ settings, projects, projectId, onNavigate, onProjectCreated, onSample, onRefresh }) {
  const requests = useRequestScope(projectId), scope = requests.scope;
  const row = settings.model?.generation, mode = settings.model?.generation_mode;
  const modelReady = generationReady(settings);
  const projectReady = projects.some(item => !['default', 'inbox', 'me'].includes(item.id));
  const [step, setStep] = useState(modelReady ? projectReady ? 3 : 2 : 1);
  const [template, setTemplate] = useState(templates[0]), [name, setName] = useState('工作'), [scenes, setScenes] = useState(templates[0].scenes);
  const [scene, setScene] = useState(''), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [provider, setProvider] = useState(PROVIDER_PRESETS[0]);
  const created = useRef(null), pending = useRef(false);
  async function configure(action) {
    if (pending.current) return;
    const token = requests.issue('model', scope);
    pending.current = true; setBusy(true); setError('');
    try {
      await action();
      if (!requests.isCurrent(token)) return;
      const current = await settingsApi.load();
      if (requests.isCurrent(token)) { if (generationReady(current)) setStep(projectReady ? 3 : 2); onRefresh(); }
    }
    catch (failure) { if (requests.isCurrent(token)) setError(failure.status === 409 ? '设置已变化 · 重新读取' : '接入未完成 · 重试'); }
    finally { pending.current = false; if (requests.isCurrent(token)) setBusy(false); }
  }
  async function createProject(event) {
    event.preventDefault();
    if (pending.current || !name.trim()) return;
    pending.current = true; setBusy(true); setError('');
    const token = requests.issue('create', scope);
    try {
      const project = created.current || await settingsApi.createProject(name.trim());
      created.current = project;
      if (!requests.isCurrent(token)) return;
      onProjectCreated?.(project);
      const saved = scenes.length ? await settingsApi.saveProject(project, { scenes }) : project;
      if (!requests.isCurrent(token)) return;
      created.current = null; onProjectCreated?.(saved); setStep(3); onRefresh();
      onNavigate?.('workbench', { project_id: saved.id });
    } catch (failure) {
      if (requests.isCurrent(token)) setError(failure.status === 409 ? '项目已变化 · 查看设置' : '创建未完成 · 重试');
    } finally { pending.current = false; if (requests.isCurrent(token)) setBusy(false); }
  }
  return <section className="workbench-start" aria-label="开始">
    <header><h2>开始</h2><ProgressDots total={3} done={Number(modelReady) + Number(projectReady)}/><span className="ui-count">{Number(modelReady) + Number(projectReady)}/3</span></header>
    {[['接入模型', modelReady], ['建一个项目', projectReady], ['投第一条', false]].map(([title, done], index) => <section key={title}>
      <button type="button" className="workbench-start-step" aria-expanded={step === index + 1} onClick={() => setStep(step === index + 1 ? 0 : index + 1)}>
        <StatusDot state={done ? 'done' : step === index + 1 ? 'pending' : 'unverified'}/><span>{title}</span>{done && <Icon name="check" size={14}/>}<Icon name="chevron-down" size={14}/>
      </button>
      {step === index + 1 && <div className="workbench-start-body">
        {index === 0 ? <><div className="workbench-start-model"><Icon name="pattern" size={20}/><span>{mode?.mode === 'local' ? mode.local_model : row?.model || '模型'}</span><span className="ui-count">{modelReady ? '已配置' : row?.configured ? '待启用' : '待接入'}</span></div>
          {row && mode?.mode !== 'local' && <div className="workbench-start-egress"><span>外发</span><Switch label="生成外发" title={REMOTE_PROCESSING_HINT} checked={row.allow_remote === true && row.enabled === true} disabled={busy || !row.configured} onChange={enabled => configure(() => mode?.mode === 'subscription' ? settingsApi.toggleSubscription(enabled, row.revision) : settingsApi.saveModel({ purpose: 'generation', serverManaged: row.server_managed === true, baseUrl: row.base_url, model: row.model, apiKey: '', enabled: enabled || row.enabled === true, allowRemote: enabled, expectedRevision: row.revision }))}/></div>}
          {!modelReady && row && !row.server_managed && (!mode || mode.mode === 'api') && <><div className="settings-provider-grid workbench-provider-grid" role="group" aria-label="接入服务商">{PROVIDER_PRESETS.map(item => <button type="button" className="settings-provider-card" key={item.id} aria-pressed={provider.id === item.id} disabled={busy} onClick={() => setProvider(item)}><span className="settings-provider-mark">{item.mark}</span><strong>{item.name}</strong></button>)}</div>
            <ModelConnectionForm key={provider.id} row={row} mode={mode?.mode || 'api'} preset={provider} run={configure} busy={busy}/></>}
          <button type="button" className="workbench-primary" onClick={() => onNavigate?.('settings', { section: 'model' })}>{modelReady ? '查看设置' : '接入模型'}<Icon name="arrow-up-right" size={14}/></button></>
          : index === 1 ? <form onSubmit={createProject}>
            <div className="workbench-templates" role="group" aria-label="项目模板">{templates.map(item => <button key={item.name} type="button" aria-pressed={item === template} disabled={busy || Boolean(created.current)} onClick={() => { setTemplate(item); setName(item.name === '空白' ? '' : item.name); setScenes(item.scenes); }}><strong>{item.name}</strong><span>{item.scenes.join(' · ') || '—'}</span></button>)}</div>
            <div className="workbench-start-fields"><input aria-label="项目名称" value={name} disabled={busy || Boolean(created.current)} onChange={event => setName(event.target.value)} required maxLength={80}/><button type="submit" className="workbench-primary" disabled={busy || !name.trim()}>{created.current ? '重试' : '建好'}</button></div>
            <div className="workbench-scene-chips">{scenes.map(item => <span key={item}>{item}<button type="button" aria-label={`去掉场景 ${item}`} disabled={busy} onClick={() => setScenes(old => old.filter(value => value !== item))}><Icon name="close" size={12}/></button></span>)}</div>
            <div className="workbench-start-fields"><input aria-label="新场景" placeholder="场景" maxLength={80} value={scene} disabled={busy} onChange={event => setScene(event.target.value)}/><button type="button" disabled={busy || !scene.trim() || scenes.includes(scene.trim())} onClick={() => { setScenes(old => [...old, scene.trim()]); setScene(''); }}>添加场景</button></div>
          </form> : <div className="workbench-first-input">{[['remember', '记一条', '今天想记住的事：'], ['ask', '问一句', '我最近记录了什么？'], ['do', '写一篇', '帮我整理本周的进展']].map(([intent, label, text]) => <button key={intent} type="button" onClick={() => onSample({ text, intent })}><Icon name={intent} size={18}/>{label}</button>)}</div>}
      </div>}
    </section>)}
    {error && <div className="workbench-error" role="alert">{error}{created.current && <button type="button" onClick={() => onNavigate?.('settings', { section: 'project', expand_project: created.current.id })}>查看设置</button>}</div>}
  </section>;
}
