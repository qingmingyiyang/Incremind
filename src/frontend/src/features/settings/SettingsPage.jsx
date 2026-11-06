import { formatBytes } from '../../shared/lib/formatBytes';
import { createContext, useContext, useEffect, useId, useRef, useState } from 'react';
import { Row, Switch, StatusDot, Icon } from '../../shared/ui';
import { ChatGPTSubscriptionSettings } from '../../shared/ui/ChatGPTSubscriptionSettings';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { readDeviceCredential, userStorageKey } from '../../shared/api/deviceTransport';
import { PROJECT_TEMPLATES } from '../../shared/lib/projectTemplates';
import { beginCredentialDraft, completeCredentialDraft, editCredentialDraft, receiveCredentialDraft, reviewCredentialDraft, submitCredentialDraft } from './credentialDraft';
import { REMOTE_PROCESSING_HINT, PRIVATE_MATERIAL_HINT, LOCAL_GENERATION_HINT, LOCAL_VECTOR_HINT } from '../../shared/lib/privacyMessages';
import { settingsApi } from './settingsApi';
import { localGenerationStatus } from './localGenerationStatus';
import { ProjectConstraints } from './ProjectConstraints';
import { ProjectScenes } from './ProjectScenes';
import { SharedProjectManagement } from '../shared-projects/SharedProjectManagement';
import { SettingsData } from './SettingsData';
import { SettingsDevices } from './SettingsDevices';
import { ExternalAgentSettings } from './ExternalAgentSettings';
import { SkillExports } from './SkillExports';
import { ProviderStoreField } from './ProviderStoreField';
import { PlatformSessionSettings } from './PlatformSessionSettings';
import { AsrLexicon } from './AsrLexicon';
import { AsrCredentialSettings } from './AsrCredentialSettings';
import { PurposeTest } from './PurposeTest';
import { QuickSettings } from './QuickSettings';
import { EgressSwitch } from './EgressSwitch';
import { ProviderConnections } from './ProviderConnections';
import { useProviderOperations } from './useProviderOperations';
import { ModelTrialPanel } from './ModelTrialPanel';
import { PrivacyMap } from './PrivacyMap';
import { RecentModelUsage } from './RecentModelUsage';
import { formatReceiptCost, ReceiptInspection, ReceiptScopeNote, useMonthlyUsage } from './ReceiptInspection';
import { SettingsQuestion } from './SettingsQuestion';
import { localBaseUrlForMode } from './modelEndpoint';
import { applyTheme, readStoredTheme } from '../rebuild/themeBootstrap';
import './Settings.css';

const groups = { model: '模型', connect: '连接', project: '项目', privacy: '隐私', data: '数据', devices: '设备' };
const purposes = { generation: '生成', asr: '转写', embedding: '向量', rerank: '重排', search: '搜索' };
const groupIcons = { model: 'pattern', connect: 'link', project: 'library', privacy: 'book', data: 'save', devices: 'person' };
const purposeIcons = { generation: 'pattern', asr: 'mic', embedding: 'library', rerank: 'related', search: 'search', vision: 'book' };
function SettingsGlyph({ name, text }) { return <span className="settings-glyph" aria-hidden="true">{text || <Icon name={name} size={18}/>}</span>; }
function SettingName({ label }) { return <span>{label}</span>; }
function ModelSummary({ children, configured }) { return <span aria-hidden="true" className="settings-model-summary"><StatusDot state={configured ? 'done' : 'unverified'}/>{children}</span>; }
function AccessibleModelSummary({ value }) { return <span className="settings-accessible-summary" role="status" aria-label={value}/>; }
function Expander({ open, onOpen }) { return <button type="button" className="settings-chevron" aria-label="展开" aria-expanded={open} onClick={onOpen}><Icon name="chevron-down" size={16}/></button>; }
const PanelSaveContext = createContext(null);
const configurationIdentity = row => JSON.stringify(['revision', 'provider', 'base_url', 'model', 'enabled', 'allow_remote', 'subscription_binding', 'server_model_binding'].map(key => row[key]));
function usePanelSave(part, save, acceptParent) {
  const panel = useContext(PanelSaveContext);
  useEffect(() => { panel.current.set(part, { save, acceptParent }); return () => { panel.current.delete(part); }; }, [panel, part, save, acceptParent]);
}
function ModelPanel({ children, row, purpose, modeRevision, modeSelection, api, run, busy }) {
  const commands = useRef(new Map()), [saved, setSaved] = useState(false), timer = useRef(null);
  const gate = useRequestScope(purpose), scope = gate.scope;
  useEffect(() => () => clearTimeout(timer.current), []);
  if (purpose === 'asr') return <div className="settings-model-form">{children}</div>;
  return <PanelSaveContext.Provider value={commands}><form className="settings-model-form" onChange={() => setSaved(false)} onSubmit={event => {
      event.preventDefault(); if (busy) return;
      const token = gate.issue('save', scope); if (!token) return;
      clearTimeout(timer.current); setSaved(false);
      run(async () => {
        const submitted = new Map(commands.current);
        let current = row, currentModeRevision = modeRevision;
        async function verifyParent() {
          const settings = await api.load(), fresh = settings.model[purpose];
          const same = configurationIdentity(fresh) === configurationIdentity(current);
          const selection = settings.model.generation_mode;
          const sameMode = purpose !== 'generation' || selection.revision === currentModeRevision
            && ['mode', 'local_enabled', 'local_base_url'].every(key => selection[key] === modeSelection[key]);
          if (!same || !sameMode) throw Object.assign(new Error('settings_changed'), { status: 409 });
        }
        for (const part of ['model', 'fast', 'prices']) {
          if (!gate.isCurrent(token)) return;
          if (!submitted.has(part)) continue;
          if (part !== 'model') await verifyParent();
          if (!gate.isCurrent(token)) return;
          const changed = await submitted.get(part).save(current, currentModeRevision);
          if (changed?.configuration && purpose !== 'asr') {
            const result = changed.configuration;
            if (!Number.isInteger(result.revision)) throw Object.assign(new Error('操作未完成 · 重试'), { code: 'invalid_response' });
            current = { ...current, ...result };
            // 生产 update 只在已有方式记录时随模型配置写入一次，读取验证不得接纳其他写入。
            if (purpose === 'generation' && modeRevision > 0) currentModeRevision = modeRevision + 1;
            await verifyParent();
            if (!gate.isCurrent(token)) return;
            for (const command of submitted.values()) command.acceptParent?.(current, currentModeRevision);
          }
        }
        if (gate.isCurrent(token)) { setSaved(true); timer.current = setTimeout(() => setSaved(false), 1400); }
      });
    }}>{children}{(!row.server_managed || purpose === 'generation') && <div className="settings-save"><button type="submit" title={`模型 r${row.revision ?? row.settings_revision ?? '—'} · 快模型 r${row.fast_model?.revision ?? '—'} · 单价 r${row.pricing?.revision ?? '—'}`} disabled={busy}>{saved ? '✓ 已保存' : '保存'}</button></div>}
  </form></PanelSaveContext.Provider>;
}
export default function SettingsPage({ projectId = 'default', initialSection = 'model', initialPurpose, expandProject, createProject, api = settingsApi }) {
  const showAgentTools = !readDeviceCredential()?.mobile;
  const [section, setSection] = useState(initialSection === 'websites' ? 'connect' : initialSection in groups ? initialSection : 'model');
  const [modelFocus, setModelFocus] = useState(initialPurpose || null);
  useEffect(() => { setSection(initialSection === 'websites' ? 'connect' : initialSection in groups ? initialSection : 'model'); }, [initialSection]);
  useEffect(() => { setModelFocus(initialPurpose || null); }, [initialPurpose]);
  const [theme, setTheme] = useState(readStoredTheme);
  const [value, setValue] = useState(null), [projects, setProjects] = useState([]), [refresh, setRefresh] = useState(0);
  const [scheduleRefresh, setScheduleRefresh] = useState(0);
  const [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const gate = useRequestScope(projectId), scope = gate.scope;
  useEffect(() => {
    // 桌面按钮更新实际主题时，分段控件仍读取已保存的三态值。
    const observer = new MutationObserver(() => setTheme(readStoredTheme()));
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const token = gate.issue('load', scope);
    Promise.all([api.load(), api.projects()]).then(([settings, list]) => {
      if (gate.isCurrent(token)) { setValue(settings); setProjects(list.items || []); }
    }).catch(() => { if (gate.isCurrent(token)) setError('读取未完成 · 重试'); });
  }, [api, projectId, refresh]);
  useEffect(() => {
    const embedding = value?.model?.embedding, local = embedding?.local;
    if (!['loading', 'unloading'].includes(local?.runtime_status) && local?.status !== 'installing' && !(embedding?.mode === 'local' && local?.runtime_status === 'loaded' && local?.status === 'ready' && local?.index && local.index.done < local.index.total && !local.index_reason_code)) return;
    const timer = setInterval(() => setRefresh(old => old + 1), 1000);
    return () => clearInterval(timer);
  }, [value?.model?.embedding?.mode, value?.model?.embedding?.local]);
  async function run(operation) {
    if (busy) return;
    const token = gate.issue('write', scope);
    if (!token) return;
    gate.invalidate('load');
    setBusy(true); setError('');
    try { await operation();  }
    catch (reason) { if (gate.isCurrent(token)) setError(reason.status === 409 || reason.code === 'revision_conflict' ? '设置已变化 · 刷新' : ['server_unavailable', 'invalid_response'].includes(reason.code) ? reason.message : '操作未完成 · 重试'); }
    finally {
      if (gate.isCurrent(token)) {
        try {
          const [settings, list] = await Promise.all([api.load(), api.projects()]);
          if (gate.isCurrent(token)) { gate.invalidate('load'); setValue(settings); setProjects(list.items || []); }
        } catch { if (gate.isCurrent(token)) setError('读取未完成 · 重试'); }
        finally { if (gate.isCurrent(token)) setBusy(false); }
      }
    }
  }
  return <div className="settings-page"><nav aria-label="设置分组">{Object.entries(groups).map(([key, label]) => <button type="button" key={key} aria-pressed={section === key} onClick={() => setSection(key)}><Icon name={groupIcons[key]} size={17}/>{label}</button>)}
    <div role="group" aria-label="主题" className="settings-theme">{[['light', '浅色', 'sun'], ['system', '跟随系统', 'settings'], ['dark', '深色', 'moon']].map(([key, label, icon]) =>
      <button type="button" key={key} aria-label={label} title={label} aria-pressed={theme === key} onClick={() => setTheme(applyTheme(key))}><Icon name={icon} size={16}/></button>)}</div></nav>
    <main>{error && <p role="alert">{error} <button type="button" onClick={() => { setError(''); setRefresh(old => old + 1); }}>重试</button></p>}{!value ? <span role="status">读取中</span> : <>
      {section === 'model' && <ModelRows key={`${projectId}:${JSON.stringify(Object.values(value.model).map(row => row?.server_model_binding))}`} projectId={projectId} model={value.model} focus={modelFocus} onRecords={() => setSection('privacy')} api={api} busy={busy} run={run} reload={() => setRefresh(old => old + 1)}/>}
      {section === 'connect' && <ConnectionRows key={projectId} projectId={projectId} value={value} api={api} run={run} busy={busy} showAgentTools={showAgentTools} onConfigureGeneration={() => { setSection('model'); setModelFocus('generation'); }}/>}
      {section === 'privacy' && <PrivacyRows key={projectId} model={value.model} externalAgent={showAgentTools ? value.external_agent || { allow_remote: false, clients: {} } : null} privacy={value.privacy} projects={projects} api={api} run={run} busy={busy}/>}
      {section === 'project' && <><ProjectRows key={projectId} projectId={projectId} projects={projects} expandProject={expandProject} openCreation={createProject} api={api} run={run} busy={busy} showAgentTools={showAgentTools}/><SharedProjectManagement/></>}
      {section === 'data' && <SettingsData projectId={projectId} scheduleRefresh={scheduleRefresh}/>}
      {section === 'devices' && <SettingsDevices key={projectId} projectId={projectId}/>}
      <QuickSettings key={`quick:${projectId}`} value={value} projects={projects} api={api} run={run} busy={busy} showAgentTools={showAgentTools}
        onBackupScheduleChange={() => setScheduleRefresh(value => value + 1)}/>
    </>}</main></div>;
}

function ConnectionRows({ projectId, value, api, run, busy, showAgentTools, onConfigureGeneration }) {
  const [open, setOpen] = useState(true);
  return <div className="settings-connections">{showAgentTools && value.external_agent && <><h2 className="settings-caption">外部 agent</h2><div className="settings-card"><ExternalAgentSettings projectId={projectId} value={value.external_agent} proxy={value.external_proxy} expanded={open} onOpen={() => setOpen(old => !old)} onConfigureGeneration={onConfigureGeneration} api={api} run={run} busy={busy}/></div></>}<h2 className="settings-caption">网站</h2><PlatformSessionSettings/></div>;
}

function ModelRows({ projectId, model, focus, onRecords, api, run, busy, reload }) {
  const [open, setOpen] = useState(null), [modeView, setModeView] = useState(null), [test, setTest] = useState({});
  const [connections, setConnections] = useState(null), [connectionTarget, setConnectionTarget] = useState(null);
  const providerOperations = useProviderOperations();
  const modelArea = useRef(null), connectionsButton = useRef(null);
  const connectionsId = useId(), egressHintId = useId();
  function focusControl(control) {
    control?.focus({ preventScroll: true });
    control?.scrollIntoView?.({ block: 'nearest' });
  }
  function configurePurpose(purpose, type) {
    setOpen(purpose);
    if (type === 'subscription') setModeView('subscription');
    const row = Array.from(modelArea.current?.querySelectorAll('[data-model-purpose]') || []).find(item => item.dataset.modelPurpose === purpose);
    focusControl(row?.querySelector('.ui-row-open'));
  }
  useEffect(() => { if (focus) configurePurpose(focus); }, [focus]);
  function configureConnection(purpose) {
    setConnectionTarget({ purpose });
    setConnections(true);
    focusControl(connectionsButton.current);
  }
  const actualMode = model.generation_mode.mode, mode = modeView || actualMode;
  const localState = localGenerationStatus(model.generation_mode.local_readiness);
  useEffect(() => { setModeView(null); }, [actualMode]);
  async function toggle(purpose, enabled) {
    const row = model[purpose] || {revision:0, enabled:false, allow_remote:false};
    if (purpose === 'asr') return run(() => enabled ? api.enableAsr() : api.disableAsr());
    if (purpose === 'generation' && actualMode === 'subscription') return run(() => api.toggleSubscription(enabled, row.revision));
    return run(() => api.saveModel({ purpose, serverManaged: row.server_managed === true, baseUrl: row.base_url, model: row.model, apiKey: '', enabled: enabled || row.enabled === true, allowRemote: enabled, expectedRevision: row.revision }));
  }
  function pickMode(next) {
    setModeView(next === 'subscription' ? next : null);
    if (next !== 'subscription') run(() => api.saveMode({ mode: next, localEnabled: next === 'local' || model.generation_mode.local_enabled, localBaseUrl: localBaseUrlForMode(next, model.generation_mode), expectedRevision: model.generation_mode.revision, clearSubscription: actualMode === 'subscription' }));
  }
  function purposeRow(purpose) {
    const label = purposes[purpose];
    if (purpose === 'embedding') return <EmbeddingRow key={purpose} row={model.embedding} open={open === purpose}
      onOpen={() => setOpen(old => old === purpose ? null : purpose)} onConnection={configureConnection} hintId={egressHintId} api={api} run={run} busy={busy}/>;
    const row = model[purpose] || {}, asr = purpose === 'asr', local = purpose === 'generation' && actualMode === 'local';
    const enabled = asr ? row.enabled === true && row.egress_manifest?.consented === true : row.allow_remote === true;
    const sharedAsr = asr && row.server_managed;
    const changeOpen = () => setOpen(old => old === purpose ? null : purpose);
    const summary = local ? model.generation_mode.local_model : [row.provider_name || row.provider, row.model].filter(Boolean).join(' · ') || '—';
    const configured = Boolean(row.configured || row.status === 'ready' || asr && row.has_api_key);
    const modeLabel = local ? '本机' : row.server_managed ? '服务器共享' : purpose === 'generation' && actualMode === 'subscription' ? '订阅' : 'API';
    const configLabel = local ? localState.label : row.provider_connection?.available === false ? '连接不可用' : asr && row.available === false ? '暂不可用' : configured ? '配置已保存' : '待接入';
    const details = `${modeLabel} · ${configLabel}${summary !== '—' ? ` · ${summary}` : ''}`;
    const currentTest = test[purpose]?.configuration === configurationIdentity(row) && test[purpose]?.mode === actualMode ? test[purpose].result : '';
    return <section key={purpose} className="settings-item" data-model-purpose={purpose}><Row dot={<SettingsGlyph name={purposeIcons[purpose]}/>} title={<SettingName label={label}/>} sub={<ModelSummary configured={configured}>{details}</ModelSummary>} expanded={open === purpose} onOpen={changeOpen} trailing={<><AccessibleModelSummary value={details}/>{!configured && !local && !sharedAsr && <button type="button" className="settings-connect" onClick={changeOpen}>接入</button>}<EgressSwitch label={`${label}外发`} aria-describedby={egressHintId} title={sharedAsr && row.available === false ? '共享转写缺少运行条件' : local ? LOCAL_GENERATION_HINT : REMOTE_PROCESSING_HINT} checked={!local && enabled} disabled={busy || local || sharedAsr && row.available === false && !enabled} onChange={next => toggle(purpose, next)}/><Expander open={open === purpose} onOpen={changeOpen}/></>}/>
      {open === purpose && <div className="settings-expand">
        {purpose === 'generation' && !row.server_managed && <div role="group" aria-label="方式" className="settings-modes">{[['api', 'API'], ['subscription', '订阅'], ['local', '本机']].map(([key, name]) => <button type="button" key={key} title={key === 'local' ? [LOCAL_GENERATION_HINT, localState.detail].filter(Boolean).join(' ') : undefined} aria-pressed={mode === key} disabled={busy || key === 'local' && !localState.selectable} onClick={() => pickMode(key)}>{name}</button>)}</div>}
        {purpose === 'generation' && mode === 'subscription' && <ChatGPTSubscriptionSettings showEgressSwitch={false} onChanged={reload}/>}
        {purpose === 'generation' && !row.server_managed && <Row readOnly title={model.generation_mode.local_model} meta={localState.label} sub={localState.detail || '沿现有模型目录手动维护资源'}/>}
        {!asr && !row.server_managed && !(purpose === 'generation' && mode !== 'api') && <button type="button" className="settings-choose-connection" onClick={() => configureConnection(purpose)}>选择服务商</button>}
        <ModelPanel key={`${purpose}:${mode}`} row={row} purpose={purpose} modeRevision={model.generation_mode.revision} modeSelection={model.generation_mode} api={api} run={run} busy={busy}>
        {asr ? <AsrCredentialSettings row={row} api={api} onSaved={reload}/> : !(purpose === 'generation' && ['subscription', 'local'].includes(mode)) && <ModelFields key={`${purpose}:${purpose === 'generation' ? actualMode : ''}`} purpose={purpose} label={label} row={row} readonly={purpose === 'generation' && actualMode !== 'api'} busy={busy} api={api} run={run}/>}
        {purpose === 'generation' && <FastModelField key={`${mode}:${row.subscription_binding?.account}`} row={row} mode={actualMode} modeRevision={model.generation_mode.revision} api={api} run={run} busy={busy}/>}
        {purpose === 'generation' && <ProviderStoreField projectId={projectId} mode={mode} generationRevision={row.revision} modeRevision={model.generation_mode.revision} busy={busy}/>}
        {!asr && <PriceFields key={`${purpose}:${purpose === 'generation' ? mode : ''}:${row.subscription_binding?.account}`} purpose={purpose} label={label} row={row} modeRevision={model.generation_mode.revision} busy={busy} api={api} run={run}/>}
        </ModelPanel>
        {asr && <>{sharedAsr && <Row readOnly title="共享转写" meta={row.available ? '按音频时长计费' : '缺少运行条件'} sub={row.available ? '中央服务与用途费率已配置 · 本人外发单独控制' : ({ server_model_metering_price_unavailable: '管理员尚未配置用途费率', server_asr_wire_owner_unavailable: '等待服务器运行接入', server_asr_protocol_unavailable: '分配的服务缺少已核验转写协议', server_model_purpose_unavailable: '尚未分配转写服务' })[row.unavailable_reason] || '检查服务器分配、费率与运行状态'}/>}<div className="settings-facts"><Row readOnly title="长音频" meta="60s · 重叠 5s"/><Row readOnly title="上限" meta={formatBytes(row.max_audio_bytes)}/></div><AsrLexicon projectId={projectId} api={api}/></>}
        <div className="settings-test">{!asr && purpose !== 'search' && <button type="button" disabled={busy || row.server_managed || local && !localState.selectable} title={row.server_managed ? '服务器共享 API 通过实际任务校验' : undefined} onClick={() => run(async () => {
          if (local && localState.retry) await api.saveMode({ mode: 'local', localEnabled: true,
            localBaseUrl: model.generation_mode.local_base_url, expectedRevision: model.generation_mode.revision });
          const start = performance.now(); const result = await api.testModel({ purpose });
          setTest(old => ({ ...old, [purpose]: { configuration: configurationIdentity(row), mode: actualMode,
            result: result.status === 'complete' ? `实际测试通过 · ${Math.round(performance.now() - start)} ms` : '实际测试未完成' } }));
        })}>测试</button>}{!asr && purpose !== 'search' && <span role="status">{currentTest || '本页暂无测试结果'}</span>}</div>
        {(asr || purpose === 'search') && <PurposeTest purpose={purpose} projectId={projectId} row={row} api={api} disabled={busy}/>}
      </div>}
    </section>;
  }
  return <div ref={modelArea} className="settings-models"><h2 className="settings-caption">用途</h2>
    <p className="settings-egress-note" id={egressHintId}>开启外发后，该用途可自动处理普通资料。私密资料及其派生内容仍不外发。</p><div className="settings-card">
    {purposeRow('generation')}<VisionRow projectId={projectId} row={model.vision} open={open === 'vision'} onOpen={() => setOpen(old => old === 'vision' ? null : 'vision')} onConnection={configureConnection} hintId={egressHintId} api={api} run={run} busy={busy}/>{purposeRow('asr')}
  </div><h2 className="settings-caption">检索</h2><div className="settings-card">{['embedding', 'rerank', 'search'].map(purposeRow)}</div>
    <section className="settings-service-panel" aria-label="连接管理"><button ref={connectionsButton} type="button" className="settings-disclosure" aria-controls={connections !== null ? connectionsId : undefined} aria-expanded={connections === true} onClick={() => setConnections(old => !old)}>
      <span>服务商连接{providerOperations.error || providerOperations.commands.length > 0 ? ' · 有操作待核对' : ''}</span><Icon name="chevron-down" size={16}/></button>
      {connections !== null && <div id={connectionsId} hidden={!connections}><ProviderConnections projectId={projectId} model={model} run={run} busy={busy} target={connectionTarget} onConfigure={configurePurpose}/></div>}
    </section>
    <ModelTrialPanel projectId={projectId} model={model} run={run} busy={busy}/><SettingsQuestion projectId={projectId} label="带资料试问" model={model.generation.model || model.generation_mode.local_model}/><RecentModelUsage api={api} onRecords={onRecords}/></div>;
}
function FastModelField({ row, mode, modeRevision, api, run, busy }) {
  const [model, setModel] = useState(row.fast_model?.configured ? row.fast_model.model : '');
  const [catalog, setCatalog] = useState([]), [error, setError] = useState(''), [retry, setRetry] = useState(0);
  const edited = useRef(false);
  const baseline = useRef({ revision: row.fast_model?.revision ?? 0, parent: configurationIdentity(row), modeRevision });
  useEffect(() => {
    if (!edited.current) { setModel(row.fast_model?.configured ? row.fast_model.model : ''); baseline.current = { revision: row.fast_model?.revision ?? 0, parent: configurationIdentity(row), modeRevision }; }
  }, [row.fast_model?.configured, row.fast_model?.model, row.fast_model?.revision, row.revision, modeRevision]);
  useEffect(() => {
    if (mode !== 'subscription' || !row.configured) return undefined;
    let live = true; const controller = new AbortController();
    api.fastModels(controller.signal).then(value => { if (live) { setCatalog(value.items || []); setError(''); } })
      .catch(() => { if (live) setError('读取未完成 · 重试'); });
    return () => { live = false; controller.abort(); };
  }, [mode, row.configured, api, retry]);
  usePanelSave('fast', async (current, currentModeRevision) => {
    if (mode === 'local' || model.trim() === (row.fast_model?.configured ? row.fast_model.model : '')) return false;
    if (baseline.current.parent !== configurationIdentity(current) || baseline.current.modeRevision !== currentModeRevision) throw Object.assign(new Error('settings_changed'), { status: 409 });
    await api.saveFastModel({ model: model.trim() || null, expectedRevision: baseline.current.revision,
      expectedGenerationRevision: current.revision, expectedModeRevision: currentModeRevision ?? modeRevision });
    edited.current = false;
    return true;
  }, (current, currentModeRevision) => {
    if (edited.current) { baseline.current.parent = configurationIdentity(current); baseline.current.modeRevision = currentModeRevision; }
  });
  if (mode === 'local') return <Row readOnly title="快模型" meta={row.model || '主模型'}/>;
  return <div className="settings-fields">
    <label title="追问与检索；留空用主模型">快模型{row.server_managed ? <select aria-label="快模型" value={model} disabled={busy || !row.configured} onChange={event => { if (!edited.current) baseline.current = { revision: row.fast_model?.revision ?? 0, parent: configurationIdentity(row), modeRevision }; edited.current = true; setModel(event.target.value); }}><option value="">主模型</option>{(row.allowed_models || []).map(id => <option key={id} value={id}>{id}</option>)}</select> : mode === 'subscription'
      ? <select aria-label="快模型" value={model} disabled={busy || !row.configured} onChange={event => { if (!edited.current) baseline.current = { revision: row.fast_model?.revision ?? 0, parent: configurationIdentity(row), modeRevision }; edited.current = true; setModel(event.target.value); }}>
        <option value="">主模型</option>{model && !catalog.some(item => item.id === model) && <option value={model}>{model}</option>}
        {catalog.map(item => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select>
      : <input aria-label="快模型" placeholder="主模型" value={model} disabled={busy} onChange={event => { if (!edited.current) baseline.current = { revision: row.fast_model?.revision ?? 0, parent: configurationIdentity(row), modeRevision }; edited.current = true; setModel(event.target.value); }}/>}</label>
    {error && <p role="alert">{error} <button type="button" onClick={() => setRetry(old => old + 1)}>重试</button></p>}
  </div>;
}
function VisionRow({ projectId, row = { mode: 'local', mode_revision: 0, local: { status: 'unavailable' } }, open, onOpen, onConnection, hintId, api, run, busy }) {
  const originalApi = api;
  if (row.server_managed) api = { ...api, saveModel: args => originalApi.saveModel({ ...args, serverManaged: true }) };
  const remote = row.mode === 'remote';
  const configured = remote ? row.configured : row.local?.status === 'ready';
  const mode = remote ? row.server_managed ? '服务器共享' : '外接' : '本机';
  const status = remote ? row.provider_connection?.available === false ? '连接不可用' : configured ? '配置已保存' : '待接入' : configured ? '可用' : '不可用';
  const details = [mode, status, remote ? row.model : row.local?.provider].filter(Boolean).join(' · ');
  return <section className="settings-item" data-model-purpose="vision"><Row dot={<SettingsGlyph name="book"/>} title={<SettingName label="识图"/>} sub={<ModelSummary configured={configured}>{details}</ModelSummary>} expanded={open} onOpen={onOpen}
    trailing={<><AccessibleModelSummary value={details}/>{!row.configured && remote && !row.server_managed && <button type="button" className="settings-connect" onClick={onOpen}>接入</button>}{remote && <EgressSwitch label="识图外发" aria-describedby={hintId} title={REMOTE_PROCESSING_HINT} checked={row.allow_remote === true} disabled={busy} onChange={allowRemote => run(() => api.saveModel({ purpose: 'vision', baseUrl: row.base_url, model: row.model, apiKey: '', enabled: row.enabled === true, allowRemote, expectedRevision: row.revision }))}/>}<Expander open={open} onOpen={onOpen}/></>}/>
    {open && <div className="settings-expand"><div role="group" aria-label="识图方式" className="settings-modes">{[['local', '本机'], ['remote', '外接']].map(([mode, label]) => <button type="button" key={mode} disabled={busy} aria-pressed={row.mode === mode} onClick={() => run(() => api.saveVisionMode({ mode, expectedRevision: row.mode_revision ?? 0 }))}>{label}</button>)}</div>
      {remote && !row.server_managed && <button type="button" className="settings-choose-connection" onClick={() => onConnection('vision')}>选择服务商</button>}
      {remote ? <ModelPanel key="vision:remote" row={row} purpose="vision" api={api} run={run} busy={busy}><ModelFields purpose="vision" label="识图" row={row} busy={busy} api={api} run={run}/><PriceFields purpose="vision" label="识图" row={row} busy={busy} api={api} run={run}/></ModelPanel> : <Row readOnly title={row.local?.provider || '—'} meta={row.local?.status === 'ready' ? '可用' : '不可用'}/>}
      <PurposeTest purpose="vision" projectId={projectId} row={row} api={api} disabled={busy}/></div>}
  </section>;
}
function EmbeddingRow({ row = {}, open, onOpen, onConnection, hintId, api, run, busy }) {
  const originalApi = api;
  if (row.server_managed) api = { ...api, saveModel: args => originalApi.saveModel({ ...args, serverManaged: true }) };
  const [test, setTest] = useState(null);
  const local = row.mode === 'local', state = row.local || {}, progress = state.status === 'installing' ? state.progress : state.index;
  const percent = progress?.total > 0 ? Math.min(100, Math.floor(progress.done / progress.total * 100)) : 0;
  const indexing = state.runtime_status === 'loaded' && state.status === 'ready' && progress && progress.done < progress.total;
  const runtimeLabel = { unloaded: '未加载', loading: '加载中', loaded: '已加载', unloading: '卸载中', failed: '加载失败' }[state.runtime_status] || '未加载';
  const configured = local ? state.runtime_status === 'loaded' : row.configured;
  const modeLabel = local ? '本机' : row.server_managed ? '服务器共享' : row.mode === 'server' ? '自建' : '外接';
  const status = local ? runtimeLabel : row.provider_connection?.available === false ? '连接不可用' : configured ? '配置已保存' : '待接入';
  const details = [modeLabel, status, local ? 'EmbeddingGemma 2' : row.model].filter(Boolean).join(' · ');
  const currentTest = test?.configuration === configurationIdentity(row) && test?.mode === row.mode && test?.runtime === state.runtime_revision ? test.result : '';
  return <section className="settings-item settings-vector-row" data-model-purpose="embedding"><Row dot={<SettingsGlyph name="library"/>} title={<SettingName label="向量"/>} expanded={open} onOpen={onOpen}
    sub={<ModelSummary configured={configured}>{details}</ModelSummary>}
    trailing={<><AccessibleModelSummary value={details}/>{!local && !row.configured && !row.server_managed && <button type="button" className="settings-connect" onClick={onOpen}>接入</button>}{!local && <EgressSwitch label="向量外发" aria-describedby={hintId} title={REMOTE_PROCESSING_HINT} checked={row.allow_remote === true} disabled={busy} onChange={allowRemote => run(() => api.saveModel({ purpose: 'embedding', baseUrl: row.base_url, model: row.model, apiKey: '', enabled: allowRemote || row.enabled === true, allowRemote, expectedRevision: row.revision }))}/>}<Expander open={open} onOpen={onOpen}/></>}/>
    {open && <div className="settings-expand"><div role="group" aria-label="向量方式" className="settings-modes">{[['local', '本机'], ['server', '自建'], ['remote', '外接']].map(([mode, label]) =>
      <button type="button" key={mode} title={mode === 'local' ? LOCAL_VECTOR_HINT : mode === 'server' ? '使用自建私有向量服务；仍按外发开关和私密规则处理' : undefined} aria-pressed={(row.mode || 'remote') === mode} disabled={busy}
        onClick={() => run(() => api.saveEmbeddingMode({ mode, expectedRevision: row.mode_revision ?? 0 }))}>{label}</button>)}</div>
      {!local && !row.server_managed && <button type="button" className="settings-choose-connection" onClick={() => onConnection('embedding')}>选择服务商</button>}
      {!local ? null :
        state.status === 'installing' ? <span className="settings-vector-progress" title={state.index_reason_code || (indexing ? '已索引' : undefined)}>
          <span role="progressbar" aria-label={indexing ? '已索引' : '下载'} aria-valuemin={0} aria-valuemax={progress?.total || 0} aria-valuenow={progress?.done || 0}><span style={{ width: `${percent}%` }}/></span>
          <span>{indexing ? `${progress.done.toLocaleString()} / ${progress.total.toLocaleString()}` : `${percent}%`}</span></span> :
        state.status === 'failed' ? <><span className="settings-vector-failed" title={state.reason_code} aria-label="向量安装失败">✕</span>
          {state.can_install !== false && <button type="button" className="settings-vector-install" disabled={busy} onClick={() => run(() => api.installEmbedding(row.mode_revision ?? 0))}>重试</button>}</> :
        state.status === 'ready' ? <><span className="ui-row-meta" title={state.runtime_reason_code}>EmbeddingGemma 2 · {state.dims} · {{ unloaded: '未加载', loading: '加载中', loaded: '已加载', unloading: '卸载中', failed: '加载失败' }[state.runtime_status] || '未加载'}</span>
          <StatusDot state={state.runtime_status === 'loaded' ? 'done' : ['loading', 'unloading'].includes(state.runtime_status) ? 'processing' : state.runtime_status === 'failed' ? 'failed' : 'unverified'}/>
          {state.can_install !== false && <button type="button" className="settings-vector-install" disabled={busy || state.runtime_status === 'unloading'}
            title="卸载释放模型引用，保留权重和向量缓存；下次需手动加载。"
            onClick={() => run(() => ['loading', 'loaded'].includes(state.runtime_status) ? api.unloadEmbedding(state.runtime_revision) : api.loadEmbedding(state.runtime_revision))}>{['loading', 'loaded'].includes(state.runtime_status) ? '卸载' : '加载'}</button>}</> :
        <><span className="ui-row-meta" title="安装后约 579 MB，文字权重约 542 MB">≈ 1.53 GB</span>{state.can_install !== false && <button type="button" className="settings-vector-install" disabled={busy}
          title={state.reason_code} onClick={() => run(() => api.installEmbedding(row.mode_revision ?? 0))}>安装</button>}</>}
    {!local ? <ModelPanel key={`embedding:${row.mode || 'remote'}`} row={row} purpose="embedding" api={api} run={run} busy={busy}><ModelFields purpose="embedding" label="向量" row={row} api={api} run={run} busy={busy}/>
      <PriceFields purpose="embedding" label="向量" row={row} api={api} run={run} busy={busy}/></ModelPanel> :
      state.index && <Row readOnly title={indexing ? '索引中' : '已索引'} meta={`${state.index.done.toLocaleString()} / ${state.index.total.toLocaleString()}`} trailing={state.index_reason_code && <span className="settings-vector-failed" title={state.index_reason_code}>✕</span>}/>}
      {(!local || state.status === 'ready') && <div className="settings-test"><button type="button" disabled={busy || local && state.runtime_status !== 'loaded' || !local && row.server_managed} title={!local && row.server_managed ? '服务器共享 API 通过实际任务校验' : undefined} onClick={() => run(async () => {
        const start = performance.now(), result = await api.testModel({ purpose: 'embedding' });
        setTest({ configuration: configurationIdentity(row), mode: row.mode, runtime: state.runtime_revision,
          result: result.status === 'complete' ? `实际测试通过 · ${Math.round(performance.now() - start)} ms` : '实际测试未完成' });
      })}>测试</button><span role="status">{currentTest || '本页暂无测试结果'}</span></div>}</div>}</section>;
}
function ModelFields({ purpose, label, row, readonly, busy, run, api }) {
  readonly = readonly || row.server_managed === true;
  const capture = !row.server_managed && purpose === 'asr' && typeof globalThis.electronAPI?.captureCredential === 'function';
  const owner = () => userStorageKey(`model-fields:${purpose}`), fields = value => ({ baseUrl: value.base_url || value.endpoint || '', model: value.model || '', key: '' });
  const [space, setSpace] = useState(owner), gate = useRequestScope(space), scope = gate.scope;
  const identity = configurationIdentity(row);
  const [draft, setDraft] = useState(() => beginCredentialDraft(space, identity, fields(row)));
  const currentDraft = receiveCredentialDraft(draft, owner(), identity, fields(row));
  useEffect(() => {
    const changed = () => { gate.invalidate('save'); setSpace(owner()); setDraft(beginCredentialDraft(owner(), identity, fields(row))); };
    window.addEventListener('chriptmas-user-space', changed); window.addEventListener('chriptmas-device-required', changed);
    return () => { window.removeEventListener('chriptmas-user-space', changed); window.removeEventListener('chriptmas-device-required', changed); };
  }, [purpose, identity]);
  useEffect(() => { setDraft(old => receiveCredentialDraft(old, space, identity, fields(row))); }, [space, identity]);
  const edit = (name, value) => setDraft(old => editCredentialDraft(receiveCredentialDraft(old, owner(), identity, fields(row)), name, value));
  usePanelSave('model', async current => {
    if (owner() !== space || !gate.isScopeCurrent(scope)) return false;
    const submitted = submitCredentialDraft(receiveCredentialDraft(currentDraft, space, configurationIdentity(current), fields(current)));
    const { key, baseUrl, model } = submitted.fields;
    if (readonly && !capture || !key && baseUrl === (row.base_url || row.endpoint || '') && model === (row.model || '')) return false;
    const token = gate.issue('save', scope); if (!token) return false;
    const saved = await (capture ? api.saveAsrKey(key) : api.saveModel({ purpose, baseUrl, model, apiKey: key, allowRemote: current.allow_remote === true, enabled: current.enabled === true, expectedRevision: current.revision }));
    if (!capture && (!Number.isInteger(saved?.revision) || saved.revision <= current.revision)) throw Object.assign(new Error('保存结果未核对 · 保留输入并重试'), { code: 'invalid_response' });
    if (owner() === space && gate.isCurrent(token)) setDraft(old => completeCredentialDraft(old, submitted,
      configurationIdentity(saved || current), fields(saved || current)));
    return { configuration: saved };
  });
  if (owner() !== space) return null;
  const { baseUrl, model, key } = currentDraft.fields;
  return <div className="settings-fields">
    <label>地址{purpose === 'asr' ? <span className="ui-row-meta">{baseUrl || '—'}</span> : <input aria-label={`${label}地址`} value={baseUrl} disabled={readonly || busy} onChange={event => edit('baseUrl', event.target.value)}/>}</label>
    <label>模型{purpose === 'asr' ? <span className="ui-row-meta">{model || '—'}</span> : <input aria-label={`${label}模型`} value={model} disabled={readonly || busy} onChange={event => edit('model', event.target.value)}/>}</label>
    <label>密钥<input aria-label={`${label}密钥`} type="password" autoComplete="new-password" value={key} placeholder={row.has_api_key ? '已设置' : '未设置'} disabled={readonly && !capture || busy} onChange={event => edit('key', event.target.value)}/></label>
    {currentDraft.stale && <section className="settings-provider-revision" aria-label={`${label}配置变更`}><p role="status">设置已变化 · 核对后保存</p>
      <Row readOnly title="当前地址" meta={row.base_url || row.endpoint || '—'}/><Row readOnly title="当前模型" meta={row.model || '—'}/>
      <button type="button" disabled={busy || readonly} onClick={() => setDraft(old => reviewCredentialDraft(receiveCredentialDraft(old, owner(), identity, fields(row))))}>重新核对</button></section>}
    {row.server_managed && <span className="settings-login-hint">服务器管理 · API 与单价由管理员配置</span>}
  </div>;
}
function PriceFields({ purpose, label, row, modeRevision, busy, run, api }) {
  const fields = { input_per_million: '输入', output_per_million: '输出', cache_read_per_million: '缓存命中' };
  const pricing = row.pricing ?? {};
  const [rates, setRates] = useState(Object.fromEntries(Object.keys(fields).map(key => [key, pricing.rates?.[key] ?? ''])));
  const edited = useRef(new Set());
  const baseline = useRef({ revision: pricing.revision ?? 0, parent: configurationIdentity(row), modeRevision });
  useEffect(() => {
    if (!edited.current.size) baseline.current = { revision: pricing.revision ?? 0, parent: configurationIdentity(row), modeRevision };
    setRates(old => Object.fromEntries(Object.keys(fields).map(key => [key, edited.current.has(key) ? old[key] : pricing.rates?.[key] ?? ''])));
  }, [row.revision, pricing.revision, pricing.rates?.input_per_million, pricing.rates?.output_per_million, pricing.rates?.cache_read_per_million, modeRevision]);
  const readonly = row.server_managed === true || pricing.source === 'server' || pricing.editable === false;
  usePanelSave('prices', async (current, _modeRevision) => {
    if (readonly || Object.keys(fields).every(key => String(rates[key]) === String(pricing.rates?.[key] ?? ''))) return false;
    if (baseline.current.parent !== configurationIdentity(current) || purpose === 'generation' && baseline.current.modeRevision !== _modeRevision) throw Object.assign(new Error('settings_changed'), { status: 409 });
    await api.savePrices({ purpose,
    rates: Object.fromEntries(Object.entries(rates).map(([key, value]) => [key, value === '' ? null : String(value)])),
    expectedRevision: baseline.current.revision, expectedConfigurationRevision: current.revision ?? 0 }); edited.current.clear();
    return true;
  }, (current, currentModeRevision) => {
    if (edited.current.size) { baseline.current.parent = configurationIdentity(current); baseline.current.modeRevision = currentModeRevision; }
  });
  return <div>
    <fieldset className="settings-prices"><legend>单价 · <span>¥ / 百万 token</span></legend>
      {Object.entries(fields).map(([key, title]) => <label key={key}>{title}<input type="number" min="0" max="1000000" step="0.000001"
        aria-label={`${label}${title}单价`} placeholder="—" value={readonly ? pricing.rates?.[key] ?? '' : rates[key]} disabled={readonly || busy}
        onChange={event => { if (!edited.current.size) baseline.current = { revision: pricing.revision ?? 0, parent: configurationIdentity(row), modeRevision }; edited.current.add(key); setRates(old => ({ ...old, [key]: event.target.value })); }}/></label>)}
    </fieldset>
  </div>;
}
function PrivacyRows({ model, externalAgent, privacy, projects, api, run, busy }) {
  const [open, setOpen] = useState(null), [sources, setSources] = useState(null), [add, setAdd] = useState('');
  const [readError, setReadError] = useState(false), [refresh, setRefresh] = useState(0);
  const gate = useRequestScope('privacy'), scope = gate.scope;
  const usage = useMonthlyUsage(api, 'privacy-usage');
  useEffect(() => {
    const token = gate.issue('read', scope); setReadError(false);
    Promise.resolve().then(() => api.privateSources()).then(value => { if (gate.isCurrent(token)) setSources(value); })
      .catch(() => { if (gate.isCurrent(token)) setReadError(true); });
    return () => gate.invalidate('read');
  }, [api, scope, refresh]);
  const name = id => projects.find(row => row.id === id)?.name || id;
  async function show(key) { setOpen(old => old === key ? null : key); if (key === 'sources') await run(async () => setSources(await api.privateSources())); }
  return <>
    {readError && <p role="alert">读取未完成 · 重试 <button type="button" onClick={() => setRefresh(old => old + 1)}>重试</button></p>}
    <h2 className="settings-caption">范围</h2><div className="settings-card">
    <Row dot={<SettingsGlyph name="library"/>} readOnly title="私密项目" sub="取消项目私密后，普通资料按各用途的外发设置处理。单独设为私密的资料继续受保护。"/>
    <div className="settings-tags">{privacy.private_projects.map(id => <div className="settings-private-project" key={id}><span>{name(id)}</span><button type="button" disabled={busy} aria-label={`取消私密 · ${name(id)}`} onClick={() => run(() => api.savePrivacy(privacy.private_projects.filter(value => value !== id), privacy.revision))}>取消私密</button></div>)}
      <select aria-label="添加私密项目" title={PRIVATE_MATERIAL_HINT} value={add} disabled={busy} onChange={event => { const id = event.target.value; setAdd(''); if (id) run(() => api.savePrivacy([...privacy.private_projects, id], privacy.revision)); }}><option value="">添加私密项目</option>{projects.filter(row => !privacy.private_projects.includes(row.id) && row.id !== 'me' && row.id !== 'inbox').map(row => <option key={row.id} value={row.id}>{row.name}</option>)}</select>
    </div>
    <Row dot={<SettingsGlyph name="book"/>} title={<span title={PRIVATE_MATERIAL_HINT}>私密资料</span>} expanded={open === 'sources'} meta={sources ? String(sources.length) : '—'} onOpen={() => show('sources')}/>
    {open === 'sources' && <div className="settings-expand">{(sources || []).map(row => <Row readOnly key={`${row.type}:${row.source_id}`} title={row.title} sub={row.inherited ? '随项目保持私密' : row.type === 'recognition' ? '私密认识' : '私密原件'} meta={name(row.project_id)} trailing={!row.inherited && Number.isInteger(row.source_revision) && Number.isInteger(row.policy_revision) && <button className="settings-cancel-private" type="button" disabled={busy} aria-label={`取消私密 · ${row.title}`} onClick={() => run(async () => { await api.cancelPrivate(row); setSources(await api.privateSources()); })}>取消私密</button>}/>)}</div>}
    </div>
    <PrivacyMap model={model} externalAgent={externalAgent} privacy={privacy} projects={projects} api={api} run={run} busy={busy}/>
    <h2 className="settings-caption">本月外发</h2><div className="settings-card settings-stats"><div><strong>{usage.data?.receipt_count ?? '—'}</strong><span>外发记录</span></div><div><strong>{formatReceiptCost(usage.data?.cost)}</strong><span>花费</span></div><div><strong>{privacy.private_projects.length} · {sources?.length ?? '—'}</strong><span>私密项目 · 资料数</span></div></div>
    <ReceiptScopeNote summary={usage.data}/>
    {usage.data && <p className="settings-support-empty">已登记模型调用 {usage.data.model_call_count} 次{externalAgent && <> · 外部记忆交付 {usage.data.external_delivery_count} 次</>}</p>}
    {usage.error && <p role="alert">本月记录未读取 · <button type="button" onClick={usage.refresh}>重试</button></p>}
    <ReceiptInspection api={api} timezone={usage.timezone} month={usage.month} scopeKey={scope.key}/>
  </>;
}
function ProjectRows({ projectId, projects, expandProject, openCreation, api, run, busy, showAgentTools }) {
  const [open, setOpen] = useState(expandProject || projectId || null), [newName, setNewName] = useState('');
  const [template, setTemplate] = useState(PROJECT_TEMPLATES[0]);
  const [creating, setCreating] = useState(openCreation === true), [created, setCreated] = useState(null);
  const createFormId = useId(), createButton = useRef(null), createName = useRef(null), returnToCreate = useRef(false);
  const [counts, setCounts] = useState({}), [refresh, setRefresh] = useState(0);
  const gate = useRequestScope('project-counts'), scope = gate.scope;
  useEffect(() => {
    const token = gate.issue('read', scope);
    for (const row of projects) {
      if (row.id === 'inbox') continue;
      Promise.resolve().then(() => api.loadConstraints({ projectId: row.id })).then(value => { if (gate.isCurrent(token)) setCounts(old => ({ ...old, [row.id]: value.items.length })); })
        .catch(() => { if (gate.isCurrent(token)) setCounts(old => ({ ...old, [row.id]: null })); });
    }
    return () => gate.invalidate('read');
  }, [projects, api, scope, refresh]);
  useEffect(() => { if (openCreation) setCreating(true); }, [openCreation]);
  useEffect(() => { if (creating) createName.current?.focus(); }, [creating]);
  useEffect(() => {
    if (!creating && !busy && returnToCreate.current) {
      returnToCreate.current = false;
      createButton.current?.focus();
    }
  }, [creating, busy]);
  const latestProject = created && (projects.find(row => row.id === created.id) || created);
  const sameScenes = row => (row.scenes || []).join('\n') === template.scenes.join('\n');
  const scenesChanged = latestProject && latestProject.revision !== created.revision && !sameScenes(latestProject);
  function closeCreation() {
    returnToCreate.current = true;
    setCreating(false); setCreated(null); setNewName(''); setTemplate(PROJECT_TEMPLATES[0]);
  }
  function createProject(event) {
    event.preventDefault();
    if (busy || scenesChanged || !newName.trim()) return;
    run(async () => {
      const project = created || await api.createProject(newName.trim());
      if (!gate.isScopeCurrent(scope)) return;
      setCreated(project); setOpen(project.id);
      if (template.scenes.length && !sameScenes(latestProject || project)) {
        await api.saveProject(project, { scenes: template.scenes });
      }
      if (gate.isScopeCurrent(scope)) closeCreation();
    });
  }
  return <><div className="settings-project-heading"><h2 className="settings-caption">项目 <span>{projects.length}</span></h2>
    <button ref={createButton} className="settings-disclosure" type="button" aria-expanded={creating} aria-controls={createFormId} disabled={busy} onClick={() => setCreating(old => !old)}>新建项目</button></div>
    <section id={createFormId} hidden={!creating} className="settings-card settings-project-create" aria-label="新建项目">
      <div className="settings-project-templates" role="group" aria-label="项目模板">{PROJECT_TEMPLATES.map(item => <button type="button" key={item.name} disabled={busy || Boolean(created)} aria-pressed={template === item} onClick={() => {
        setTemplate(item); if (!newName || newName === template.name) setNewName(item.scenes.length ? item.name : '');
      }}><strong>{item.name}</strong><span>{item.scenes.join(' · ') || '自己安排'}</span></button>)}</div>
      <form className="settings-new-project" onSubmit={createProject}><input ref={createName} aria-label="项目名称" placeholder="项目名称" value={newName} disabled={busy || Boolean(created)} onChange={event => setNewName(event.target.value)}/><button type="submit" disabled={busy || scenesChanged || !newName.trim()}>{created ? '保存场景' : '创建'}</button></form>
      {created && <p className="settings-login-hint" role="status">{created.name}已创建。{scenesChanged ? '项目已有新修改，在下方详情核对场景。' : '场景可继续保存。'} <button type="button" disabled={busy} onClick={closeCreation}>关闭新建</button></p>}
    </section><div className="settings-card settings-project-list">{projects.map(row => <section className="settings-item" key={row.id}><Row dot={<SettingsGlyph text={Array.from(row.name)[0]}/>} title={<SettingName label={row.name}/>} sub={<span aria-hidden="true">{row.scenes?.join(' · ') || '—'} · {row.id === 'inbox' ? 0 : counts[row.id] ?? '—'} 条约束{row.private && ' · 私密'}</span>} expanded={open === row.id} onOpen={() => setOpen(old => old === row.id ? null : row.id)} trailing={<Expander open={open === row.id} onOpen={() => setOpen(old => old === row.id ? null : row.id)}/>}/>
    {open === row.id && <section role="region" aria-label={`${row.name}设置`} className="settings-expand">
      {row.id !== 'me' && <ProjectFields key={`fields:${row.id}`} row={row} run={run} api={api} busy={busy}/>}
      {row.id !== 'inbox' && <ProjectConstraints key={row.id} projectId={row.id} api={api} onChanged={() => setRefresh(old => old + 1)}/>}
      <SettingsQuestion key={`question:${row.id}`} projectId={row.id} scenes={row.scenes || []}/>
      {showAgentTools && row.id !== 'inbox' && <SkillExports key={`skills:${row.id}`} projectId={row.id} scenes={row.scenes || []}/>}
    </section>}
  </section>)}</div></>;
}
function ProjectFields({ row, api, run, busy }) {
  const [name, setName] = useState(row.name);
  const edited = useRef({ name: 0 });
  useEffect(() => { if (!edited.current.name) setName(row.name); }, [row.name, row.revision]);
  return <><form onSubmit={event => { event.preventDefault(); const edit = edited.current.name; run(async () => {
    const saved = await api.saveProject(row, { name });
    if (edited.current.name === edit) { edited.current.name = 0; setName(saved.name); }
  }); }}><label>名称<input aria-label="名称" value={name} onChange={event => { edited.current.name += 1; setName(event.target.value); }}/></label><button disabled={busy}>保存</button></form>
    <ProjectScenes row={row} api={api} run={run} busy={busy}/>
    <Row readOnly title="私密" sub={row.private ? '该项目的资料和派生内容保持私密。' : '该项目的普通资料按各用途的外发设置处理。'} trailing={<Switch label="私密" title={PRIVATE_MATERIAL_HINT} checked={row.private === true} disabled={busy} onChange={value => run(() => api.saveProject(row, { private: value }))}/>}/>
  </>;
}
