import { useEffect, useRef, useState } from 'react';
import { FocusPanel } from '../../shared/ui/FocusPanel';
import { MarkdownBody } from '../../shared/ui/MarkdownBody';
import { Row } from '../../shared/ui/Row';
import { StatusDot } from '../../shared/ui/StatusDot';
import { ProductFileLink } from '../../shared/ui/ProductFileLink';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { settingsApi } from '../settings/settingsApi';
import { sharedProjectsApi as api } from './sharedProjectsApi';
import { completeExtractionSelection, extractionCommandResult, extractionCopy, extractionPreview, extractionProgress, extractionTargets } from './sharedExtractionViews';
import { confirmExtractionManifest, createExtractionCommand, extractionCommands, extractionLink, linkExtractionPreview, markExtractionCommand } from './sharedExtractionJournal';
import './sharedProjects.css';

export function ExtractionManifest({ preview, selected, onSelect, locked = false }) {
  return <section aria-label="提取完整清单">
    <h3>认识 {preview.items.length}</h3>
    {preview.items.map(item => <div key={item.id} className="shared-extraction-insight">
      <label><input type="checkbox" aria-label={`选择认识 ${item.text}`} checked={selected.includes(item.id)} disabled={locked} onChange={event => onSelect(item.id, event.target.checked)}/><span>{item.text}</span></label>
      {!!item.conditions.length && <ul>{item.conditions.map((text, index) => <li key={index}>{text}</li>)}</ul>}
    </div>)}
    <h3>资料 {preview.materials.length}</h3>
    {preview.materials.map(item => <details key={item.id} className="shared-extraction-material">
      <summary>{item.title}{item.private && <span className="ui-count"> · 私密</span>}{item.file_names.length > 0 && <span className="ui-count"> · 附件 {item.file_names.length}</span>}</summary>
      <MarkdownBody>{item.text}</MarkdownBody>
      {!!item.file_names.length && <ul aria-label="将保留的附件">{item.file_names.map((name, index) => <li key={index}>{name}</li>)}</ul>}
    </details>)}
  </section>;
}

export function ExtractionProgress({ value, onCopy, disabled = false }) {
  return <section aria-label="个人提取进度">
    <Row readOnly title={value.personal_state === 'completed' ? '个人副本已保存' : value.state === 'preview' ? '尚未提取' : '正在提取'} meta={`${value.completed}/${value.total}`}/>
    <progress aria-label="已保存认识" max={value.total} value={value.completed}/>
    {value.personal_state === 'completed' && <p role="status">{value.activity_state === 'completed' ? '操作记录已同步' : '操作记录待同步'}</p>}
    {value.copies.map((copy, index) => <Row key={copy.id} title={`本人副本 ${index + 1}`} readOnly={!onCopy || disabled} onOpen={() => onCopy(copy)}
      trailing={disabled ? <StatusDot state="processing"/> : undefined}/>)}
  </section>;
}

export function SharedExtraction({ enabled = false, refreshToken = 0 }) {
  const [epoch, setEpoch] = useState(0), [open, setOpen] = useState(false), [commands, setCommands] = useState([]);
  const [commandId, setCommandId] = useState(null), [catalog, setCatalog] = useState(null), [refresh, setRefresh] = useState(0);
  const [source, setSource] = useState(''), [target, setTarget] = useState(''), [view, setView] = useState(null), [copy, setCopy] = useState(null);
  const [selected, setSelected] = useState([]), [confirmed, setConfirmed] = useState(false), [working, setWorking] = useState(false), [error, setError] = useState('');
  const immediate = useRef(false);
  const storageKey = userStorageKey('shared-extractions-v1');
  const gate = useRequestScope(`${storageKey}\0${epoch}`), scope = gate.scope;
  const actor = deviceIdentity();
  const current = token => gate.isCurrent(token) && isDeviceIdentity(actor) && userStorageKey('shared-extractions-v1') === storageKey;
  const command = commands.find(item => item.id === commandId);
  const visible = view?.scope === scope && view.id === commandId ? view : null;
  const preview = visible?.preview, progress = visible?.progress;
  const ready = catalog?.scope === scope;
  const sources = ready ? catalog.sources : [], targets = ready ? catalog.targets : [];
  const sourceAllowed = id => ready && sources.some(item => item.id === id && item.state === 'active');
  const writable = Boolean(command && sourceAllowed(command.expected.source) && !visible?.forbidden);
  const sameSelection = Boolean(preview && completeExtractionSelection(preview, selected));
  const linked = command ? extractionLink(command) : null;
  const unresolved = Boolean(command && !linked && !visible?.unreceived);

  function sync(preferred) {
    const rows = extractionCommands(storageKey);
    setCommands(rows);
    if (preferred) setCommandId(preferred);
    return rows;
  }
  useEffect(() => {
    const changed = () => {
      gate.invalidate('operation'); gate.invalidate('catalog'); setEpoch(value => value + 1);
      immediate.current = false; setWorking(false); setOpen(false); setCommands([]); setCommandId(null);
      setCatalog(null); setView(null); setCopy(null); setSelected([]); setConfirmed(false); setError('');
      setSource(''); setTarget('');
    };
    window.addEventListener('chriptmas-user-space', changed); window.addEventListener('chriptmas-device-required', changed);
    return () => { window.removeEventListener('chriptmas-user-space', changed); window.removeEventListener('chriptmas-device-required', changed); };
  }, [gate]);
  useEffect(() => {
    if (!enabled) return;
    const changed = event => {
      if (!gate.isScopeCurrent(scope) || !isDeviceIdentity(actor) || userStorageKey('shared-extractions-v1') !== storageKey) return;
      if (event?.key && !event.key.startsWith(storageKey)) return;
      try { sync(); } catch (reason) { setError(reason.message); }
    };
    changed(); window.addEventListener('storage', changed);
    return () => window.removeEventListener('storage', changed);
  }, [enabled, scope]);
  useEffect(() => {
    if (!enabled || !open) return;
    const token = gate.issue('catalog', scope), controller = new AbortController();
    if (!current(token)) return;
    setCatalog(null);
    Promise.all([api.list({ signal: controller.signal }), settingsApi.projects()]).then(([rows, projects]) => {
      if (!current(token)) return;
      const personal = extractionTargets(projects);
      setCatalog({ scope, sources: rows, targets: personal });
      setSource(old => old || rows.find(item => item.state === 'active')?.id || '');
      setTarget(old => personal.some(item => item.id === old) ? old : personal[0]?.id || '');
    }).catch(reason => { if (!current(token)) return; setError(reason.message); });
    return () => controller.abort();
  }, [enabled, open, scope, refresh, refreshToken]);

  async function readOperation(item, token) {
    if (!current(token)) return;
    let link = extractionLink(item);
    if (!link) {
      const response = await api.extractionCommand(item.id);
      if (!current(token)) return;
      const result = extractionCommandResult(response, item.id);
      if (result.state === 'unreceived') {
        markExtractionCommand(item, 'not_received'); sync();
        setView({ scope, id: item.id, unreceived: true }); return;
      }
      link = linkExtractionPreview(item, result.preview_id);
    }
    const response = await api.extractionProgress(link.preview_id);
    if (!current(token)) return;
    const value = extractionProgress(response, link.preview_id, item.expected.target);
    setView({ scope, id: item.id, progress: value });
    if (sourceAllowed(item.expected.source)) {
      try {
        const response = await api.extraction(link.preview_id);
        if (!current(token)) return;
        const manifest = extractionPreview(response, { id: link.preview_id, source: item.expected.source, target: item.expected.target, ids: item.body.recognition_ids });
        setView({ scope, id: item.id, progress: value, preview: manifest });
        setSelected(manifest.recognition_ids); setConfirmed(false);
      } catch (reason) {
        if (!current(token)) return;
        setView({ scope, id: item.id, progress: value, forbidden: reason.status === 403 }); setError(reason.message);
      }
    }
    if (!current(token)) return;
    if (value.personal_state !== 'preview') markExtractionCommand(item, 'pending');
    sync();
  }

  async function operate(action) {
    if (immediate.current) return;
    const token = gate.issue('operation', scope);
    if (!current(token)) return;
    immediate.current = true; setWorking(true); setError('');
    try { await action(token); }
    catch (reason) { if (!current(token)) return; setError(reason.code === 'shared_extraction_preview_in_progress' ? '预览仍在准备，请读取原操作'
      : reason.status === 409 ? '资格或内容已变化，请读取原操作核对' : reason.message); }
    finally { if (current(token)) { immediate.current = false; setWorking(false); } }
  }
  useEffect(() => {
    if (enabled && open && command && ready) operate(token => readOperation(command, token));
  }, [enabled, open, commandId, catalog]);
  useEffect(() => {
    if (!working || !command || !linked) return;
    const token = gate.issue('progress', scope), controller = new AbortController(); let timer;
    async function poll() {
      if (!current(token)) return;
      try {
        const response = await api.extractionProgress(linked.preview_id, controller.signal);
        if (!current(token)) return;
        const value = extractionProgress(response, linked.preview_id, command.expected.target);
        setView(old => old?.scope === scope && old.id === command.id ? { ...old, progress: value } : old);
      } catch { if (!current(token)) return; /* 轮询失败保留最近实际进度，原请求完成后仍可读取同一编号。 */ }
      if (current(token) && !controller.signal.aborted) timer = setTimeout(poll, 1500);
    }
    timer = setTimeout(poll, 1500);
    return () => { controller.abort(); clearTimeout(timer); gate.invalidate('progress'); };
  }, [working, commandId, linked?.preview_id, scope]);

  async function requestPreview(token, item) {
    if (!current(token)) return;
    markExtractionCommand(item, 'sending'); sync(item.id);
    try {
      const response = await api.extractionPreview(item.expected.source, item.body, item.id);
      if (!current(token)) return;
      const value = extractionPreview(response, { source: item.expected.source, target: item.expected.target, ids: item.body.recognition_ids });
      linkExtractionPreview(item, value.id); markExtractionCommand(item, 'prepared'); sync(item.id);
      setView({ scope, id: item.id, preview: value }); setSelected(value.recognition_ids); setConfirmed(false);
      await readOperation(item, token);
    } catch (reason) {
      if (!current(token)) return;
      markExtractionCommand(item, 'unknown'); sync(item.id);
      // 未知结果只回读原客户端编号，明确未受理后才允许原键重试。
      try { await readOperation(item, token); }
      catch { if (!current(token)) return; setView({ scope, id: item.id }); }
      if (!current(token)) return;
      setError(reason.message);
    }
  }
  function createPreview(ids, sourceId = source, targetId = target) {
    if (!sourceAllowed(sourceId) || !targets.some(item => item.id === targetId) || working) return;
    operate(async token => {
      const previous = commands.find(item => item.expected.source === sourceId && item.expected.target === targetId
        && JSON.stringify(item.body.recognition_ids) === JSON.stringify(ids === null ? null : [...ids].sort()) && !extractionLink(item));
      if (previous) { sync(previous.id); await readOperation(previous, token); return; }
      const item = createExtractionCommand(storageKey, sourceId, targetId, ids);
      sync(item.id); await requestPreview(token, item);
    });
  }
  function adopt() {
    if (!command || !preview || !writable || working || !sameSelection || !linked?.adoption && !confirmed) return;
    operate(async token => {
      const link = linked.adoption ? linked : confirmExtractionManifest(command, preview);
      markExtractionCommand(command, 'sending'); sync();
      try {
        const response = await api.adoptExtraction(link.preview_id, link.adoption);
        if (!current(token)) return;
        const value = extractionProgress(response, link.preview_id, command.expected.target);
        markExtractionCommand(command, 'pending'); setView({ scope, id: command.id, preview, progress: value }); sync();
      } catch (reason) {
        if (!current(token)) return;
        markExtractionCommand(command, 'unknown'); sync();
        await readOperation(command, token);
        if (!current(token)) return;
        setError(reason.message);
      }
    });
  }
  function openCopy(item) {
    operate(async token => {
      const response = await api.extractedInsight(item.id);
      if (!current(token)) return;
      const value = extractionCopy(response, item);
      setCopy({ scope, value });
    });
  }
  function newForm() {
    if (working || unresolved) return;
    setCommandId(null); setView(null); setCopy(null); setSelected([]); setConfirmed(false); setError('');
  }
  if (!enabled) return null;
  return <section className="shared-projects-panel" aria-label="共享认识提取">
    <header><h3>提取到个人项目</h3><button type="button" disabled={working} onClick={() => { setOpen(true); newForm(); }}>提取</button></header>
    {!!commands.length && <label>本人提取记录<select aria-label="本人提取记录" disabled={working} value={commandId || ''} onChange={event => { setCommandId(event.target.value || null); setOpen(true); setCopy(null); setView(null); }}>
      <option value="">选择记录</option>{commands.map(item => <option key={item.id} value={item.id}>{item.expected.source} → {item.expected.target} · {item.id}</option>)}
    </select></label>}
    {error && !open && <p role="alert">{error}</p>}
    {open && <FocusPanel title={copy?.scope === scope ? '本人副本' : '提取到个人项目'} onClose={() => setOpen(false)} className="shared-extraction-focus">
      {copy?.scope === scope ? <>
        <button type="button" className="ui-filter" onClick={() => setCopy(null)}>返回提取</button>
        <a href={`#${new URLSearchParams({ view: 'library', project_id: copy.value.target_project_id, layer: 'insight', insight_id: copy.value.target_recognition_id })}`}>查看本人认识</a>
        {copy.value.materials.map(item => <section key={item.item_id}><h3>{item.title}{item.private && <span className="ui-count"> · 私密</span>}</h3><MarkdownBody>{item.text}</MarkdownBody>
          {item.files.map(file => <ProductFileLink key={file.url} href={file.url} name={file.name} scopeKey={scope}>{file.name}</ProductFileLink>)}
        </section>)}
      </> : <>
        <div className="shared-projects-actions"><button type="button" disabled={working} onClick={() => setRefresh(value => value + 1)}>刷新资格</button><button type="button" disabled={working || unresolved} onClick={newForm}>新预览</button></div>
        {!ready ? <StatusDot state="processing"/> : <div className="shared-extraction-targets">
          <label>共享项目<select aria-label="提取来源共享项目" disabled={working || Boolean(command)} value={command?.expected.source || source} onChange={event => { newForm(); setSource(event.target.value); }}>
            <option value="">选择共享项目</option>{sources.filter(item => item.state === 'active').map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}
          </select></label>
          <label>个人项目<select aria-label="提取目标个人项目" disabled={working || Boolean(command)} value={command?.expected.target || target} onChange={event => { newForm(); setTarget(event.target.value); }}>
            <option value="">选择个人项目</option>{targets.map(item => <option key={item.id} value={item.id}>{item.name} · {item.id}</option>)}
          </select></label>
          {!command && <button type="button" disabled={working || !sourceAllowed(source) || !targets.some(item => item.id === target)} onClick={() => createPreview(null)}>预览全部</button>}
        </div>}
        {command && <div className="shared-projects-actions"><span className="ui-count">{command.id}</span><button type="button" disabled={working} onClick={() => operate(token => readOperation(command, token))}>读取原操作</button>
          {visible?.unreceived && <button type="button" disabled={working || !sourceAllowed(command.expected.source) || !targets.some(item => item.id === command.expected.target)} onClick={() => operate(token => requestPreview(token, command))}>重试原预览</button>}
        </div>}
        {working && <StatusDot state="processing"/>}
        {progress && <ExtractionProgress value={progress} onCopy={openCopy} disabled={working}/>}
        {command && ready && !writable && <p role="status">当前共享资格不可用，可读取本人副本和进度</p>}
        {preview && writable && <>
          <p>目标 #{targets.find(item => item.id === preview.target_project_id)?.name || preview.target_project_id}</p>
          <ExtractionManifest preview={preview} selected={selected} locked={working || Boolean(linked?.adoption)} onSelect={(id, checked) => { setConfirmed(false); setSelected(old => checked ? [...old, id] : old.filter(value => value !== id)); }}/>
          {!linked?.adoption && <div className="shared-projects-actions"><button type="button" disabled={working || !selected.length || sameSelection} onClick={() => {
            setSource(command.expected.source); setTarget(command.expected.target);
            createPreview(selected, command.expected.source, command.expected.target);
          }}>预览所选 {selected.length}</button>
            <label><input type="checkbox" checked={confirmed} disabled={working || !sameSelection} onChange={event => setConfirmed(event.target.checked)}/>确认以上全部 {preview.items.length} 条认识和 {preview.materials.length} 份资料</label>
          </div>}
          {progress?.personal_state !== 'completed' && <button type="button" disabled={working || !sameSelection || !linked?.adoption && !confirmed} onClick={adopt}>{linked?.adoption ? '继续原提取' : '确认提取'}</button>}
        </>}
        {progress?.personal_state === 'completed' && progress.activity_state === 'pending' && writable && <button type="button" disabled={working} onClick={() => operate(async token => {
          const response = await api.reconcileExtraction(linked.preview_id);
          if (!current(token)) return;
          const value = extractionProgress(response, linked.preview_id, command.expected.target);
          setView(old => ({ ...old, progress: value }));
        })}>同步原操作记录</button>}
      </>}
      {error && <p role="alert">{error}</p>}
    </FocusPanel>}
  </section>;
}
