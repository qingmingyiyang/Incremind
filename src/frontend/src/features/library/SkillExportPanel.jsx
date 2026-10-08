import { useEffect, useRef, useState } from 'react';
import { FocusPanel, Row, Icon } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { libraryApi } from './libraryApi';
import './Library.css';

const emptyDocument = () => ({ name: '', description: '', trigger: '', steps: [{ text: '', sources: [] }], validation: [''] });
const descriptors = sources => sources.map(({ id, revision }) => ({ id, revision }));
export const SkillNeedsUpdate = () => <span className="library-review-effect" style={{ fontSize: '11px', fontFamily: 'var(--mono)', color: 'var(--red)' }}>需更新</span>;

export function SkillExportPanel({ projectId, scene = null, sourceId = null, exportId = null, onClose, onChanged }) {
  const gate = useRequestScope(`${projectId}\0${scene || ''}\0${sourceId || ''}\0${exportId || ''}`), scope = gate.scope;
  const [loaded, setLoaded] = useState(false), [methods, setMethods] = useState([]), [sources, setSources] = useState([]);
  const [draft, setDraft] = useState(null), [document, setDocument] = useState(emptyDocument);
  const [available, setAvailable] = useState(false), [error, setError] = useState(''), [blocked, setBlocked] = useState(false), [dirty, setDirty] = useState(false);
  const [folder, setFolder] = useState({ available: false }), [directory, setDirectory] = useState(''), [confirmFolder, setConfirmFolder] = useState(false), [folderPath, setFolderPath] = useState('');
  const [busy, setBusy] = useState(false), [retry, setRetry] = useState(0);
  const pending = useRef(null), urls = useRef(new Map()), savedId = useRef(exportId), generation = useRef(null);
  function adopt(value) {
    savedId.current = value.id;
    setDraft(value); setSources(value.sources); setDocument(value.document);
    setDirty(false); setBlocked(false); generation.current = null;
  }
  useEffect(() => {
    const token = gate.issue('load', scope), controller = new AbortController();
    setLoaded(false); setError(''); setBlocked(false); pending.current = null; setBusy(false);
    // 新建后的重读继续使用服务返回的身份，避免另开一份空白草稿。
    const identity = exportId || savedId.current;
    Promise.all([libraryApi.skillExportMethods(projectId, { scene, signal: controller.signal }),
      libraryApi.skillExports(projectId, { signal: controller.signal }),
      identity ? libraryApi.skillExport(projectId, identity, { signal: controller.signal }) : Promise.resolve(null)])
      .then(([choices, catalog, saved]) => {
        if (!gate.isCurrent(token)) return;
        const rows = choices.items || []; setMethods(rows); setAvailable(catalog.generation_available === true); setFolder(catalog.local_folder || { available: false });
        if (saved) adopt(saved);
        else if (sourceId && !rows.some(row => row.id === sourceId)) {
          setBlocked(true); setError('认识已变化 · 重新读取');
        } else {
          const selected = rows.filter(row => row.id === sourceId);
          const value = emptyDocument(); value.steps[0].sources = selected.length ? [1] : [];
          // 成功重读未保存草稿后重新冻结素材；普通生成重试仍沿用原键。
          generation.current = null;
          setDraft(null); setSources(selected); setDocument(value); setDirty(false);
        }
        setLoaded(true);
      }).catch(() => { if (gate.isCurrent(token)) { setError('读取未完成 · 重试'); setLoaded(false); } });
    return () => { controller.abort(); };
  }, [projectId, scene, sourceId, exportId, retry]);
  useEffect(() => () => { for (const [href, timer] of urls.current) { clearTimeout(timer); URL.revokeObjectURL(href); } urls.current.clear(); }, []);
  const body = document;
  const complete = sources.length > 0 && [body.name, body.description, body.trigger].every(value => value.trim())
    && body.validation.length > 0 && body.validation.every(rule => rule.trim()) && body.steps.length > 0 && body.steps.every(step => step.text.trim() && step.sources.length > 0);
  const readOnly = busy || blocked;
  function change(next) { setDocument(next); setDirty(true); }
  function toggleSource(row, checked) {
    if (draft) return;
    const index = sources.findIndex(source => source.id === row.id);
    if (!checked && document.steps.some(step => step.sources.includes(index + 1))) return;
    if (checked) {
      setSources(old => [...old, row]);
      change({ ...document, steps: document.steps.map(step => step.sources.length ? step : { ...step, sources: [sources.length + 1] }) });
    } else {
      setSources(old => old.filter(source => source.id !== row.id));
      change({ ...document, steps: document.steps.map(step => ({ ...step, sources: step.sources.map(n => n > index + 1 ? n - 1 : n) })) });
    }
  }
  function cite(index, number, checked) {
    const step = document.steps[index];
    if (!checked && step.sources.length === 1) return;
    change({ ...document, steps: document.steps.map((row, position) => position !== index ? row : {
      ...row, sources: checked ? [...row.sources, number].sort((a, b) => a - b) : row.sources.filter(n => n !== number),
    }) });
  }
  async function run(operation) {
    if (pending.current) return;
    const token = gate.issue('write', scope); if (!token) return;
    pending.current = token; setBusy(true); setError('');
    try { await operation(token); }
    catch (reason) { if (gate.isCurrent(token)) { setError(reason.message || '操作未完成 · 重试'); if (reason.status === 409) setBlocked(true); } }
    finally { if (pending.current === token) pending.current = null; if (gate.isCurrent(token)) setBusy(false); }
  }
  function accept(value, token) { if (gate.isCurrent(token)) { adopt(value); onChanged?.(); } }
  function save() { return run(async token => accept(draft
    ? await libraryApi.saveSkillExport(projectId, draft.id, draft.revision, body)
    : await libraryApi.createSkillExport(projectId, { sources: descriptors(sources), document: body, scene }), token)); }
  function generate() { return run(async token => {
    const current = await libraryApi.skillExportMethods(projectId, { scene });
    if (!gate.isCurrent(token)) return;
    const selected = sources.map(source => current.items.find(row => row.id === source.id));
    if (selected.some(source => !source)) { setBlocked(true); setError('认识已变化 · 重新读取'); return; }
    const arguments_ = { sources: descriptors(selected), scene, ...(draft ? { expected_revision: draft.revision } : {}) };
    const identity = JSON.stringify(arguments_);
    if (generation.current?.identity !== identity) generation.current = { identity, key: crypto.randomUUID() };
    const body = { ...arguments_, key: generation.current.key };
    accept(draft ? await libraryApi.regenerateSkillExport(projectId, draft.id, { ...body, expected_revision: draft.revision })
      : await libraryApi.generateSkillExport(projectId, body), token);
  }); }
  function download() { return run(async token => {
    const blob = await libraryApi.downloadSkillExport(projectId, draft.id, draft.revision);
    if (!gate.isCurrent(token)) return;
    const href = URL.createObjectURL(blob), link = globalThis.document.createElement('a');
    link.href = href; link.download = `${draft.document.name}.zip`; link.rel = 'noreferrer';
    globalThis.document.body.appendChild(link); link.click(); link.remove();
    const timer = setTimeout(() => { URL.revokeObjectURL(href); urls.current.delete(href); }, 1000); urls.current.set(href, timer);
    // 下载已经完成后只重读修订，不重发写入。
    setBlocked(true);
    const latest = await libraryApi.skillExport(projectId, draft.id);
    accept(latest, token);
  }); }
  function exportFolder() { return run(async token => {
    const value = await libraryApi.folderSkillExport(projectId, draft.id, {
      expected_revision: draft.revision, directory, confirm_first_export: folder.confirmed !== true && confirmFolder,
      expected_confirmation_revision: folder.revision,
    });
    if (!gate.isCurrent(token)) return;
    accept(value, token); setFolderPath(value.folder_path); setBlocked(true);
    const catalog = await libraryApi.skillExports(projectId);
    if (gate.isCurrent(token)) { setFolder(catalog.local_folder || { available: false }); setConfirmFolder(false); setBlocked(false); }
  }); }
  const exportable = !readOnly && draft?.reviewed && !dirty && !draft.needs_update;
  return <FocusPanel className="library-focus" title="skill 草稿" onClose={onClose}>
    {error && <p className="library-error" role="alert">{error} <button type="button" disabled={busy} onClick={() => setRetry(old => old + 1)}>重新读取</button></p>}
    {!loaded && !error && <span role="status">读取中</span>}
    {loaded && (!blocked || draft) && <>
      <p className="library-meta">{[draft ? `v${draft.document_version}` : '手写', draft?.scene || scene].filter(Boolean).join(' · ')} {draft?.needs_update && <SkillNeedsUpdate/>}</p>
      <section aria-label="来源认识">{draft ? sources.map((row, i) => <Row key={row.id} readOnly title={row.text || row.id} meta={String(row.number || i + 1)} sub={row.conditions?.join(' · ')}/>) : methods.map(row => <label className="library-meta" key={row.id}>
        <input type="checkbox" aria-label={row.text} checked={sources.some(source => source.id === row.id)} disabled={readOnly} onChange={event => toggleSource(row, event.target.checked)}/>{row.text}
        {row.conditions?.length > 0 && <span> · {row.conditions.join(' · ')}</span>}
      </label>)}</section>
      <form className="library-edit" onSubmit={event => { event.preventDefault(); save(); }}>
        {[['name', '名称'], ['description', '什么时候用'], ['trigger', '触发边界']].map(([field, label]) => <label key={field}>{label}<textarea rows={field === 'name' ? 1 : 3} aria-label={label} disabled={readOnly} value={document[field]} onChange={event => change({ ...document, [field]: event.target.value })}/></label>)}
        {document.steps.map((step, index) => <section key={index} aria-label={`步骤 ${index + 1}`}>
          <label>步骤 {index + 1}<textarea aria-label={`步骤 ${index + 1}`} disabled={readOnly} value={step.text} onChange={event => change({ ...document, steps: document.steps.map((row, i) => i === index ? { ...row, text: event.target.value } : row) })}/></label>
          <div className="library-meta">{sources.map((source, i) => <label key={source.id}><input type="checkbox" aria-label={`步骤 ${index + 1} 来源 ${i + 1}`} checked={step.sources.includes(i + 1)} disabled={readOnly} onChange={event => cite(index, i + 1, event.target.checked)}/>{i + 1}</label>)}</div>
          <p className="library-meta">来源 {step.sources.join(' · ') || '—'}</p>
          {document.steps.length > 1 && <button type="button" disabled={readOnly} aria-label={`删除步骤 ${index + 1}`} onClick={() => change({ ...document, steps: document.steps.filter((_, i) => i !== index) })}><Icon name="close" size={14}/></button>}
        </section>)}
        <button type="button" disabled={readOnly} onClick={() => change({ ...document, steps: [...document.steps, { text: '', sources: sources.length ? [1] : [] }] })}><Icon name="plus" size={14}/> 步骤</button>
        {document.validation.map((rule, index) => <section key={index}>
          <label>验证规则 {index + 1}<textarea aria-label={index ? `验证规则 ${index + 1}` : '验证规则'} disabled={readOnly} value={rule} onChange={event => change({ ...document, validation: document.validation.map((value, i) => i === index ? event.target.value : value) })}/></label>
          {document.validation.length > 1 && <button type="button" aria-label={`删除验证规则 ${index + 1}`} disabled={readOnly} onClick={() => change({ ...document, validation: document.validation.filter((_, i) => i !== index) })}><Icon name="close" size={14}/></button>}
        </section>)}
        <button type="button" disabled={readOnly} onClick={() => change({ ...document, validation: [...document.validation, ''] })}><Icon name="plus" size={14}/> 验证规则</button>
        <div className="library-actions">
          <button type="submit" disabled={readOnly || !complete || Boolean(draft && !dirty)}>保存草稿</button>
          <button type="button" disabled={readOnly || !available || !sources.length} onClick={generate}>{draft ? '重新生成' : '生成草稿'}</button>
          <button type="button" disabled={readOnly || !draft || dirty || draft.reviewed || draft.needs_update} onClick={() => run(async token => accept(await libraryApi.reviewSkillExport(projectId, draft.id, draft.revision), token))}>审阅</button>
          <button type="button" disabled={!exportable} onClick={download}>下载 ZIP</button>
        </div>
      </form>
      {folder.available === true && <section className="library-edit" aria-label="目录导出">
        <label>导出目录<textarea aria-label="导出目录" placeholder="已有文件夹的绝对路径" disabled={readOnly} value={directory} onChange={event => { setDirectory(event.target.value); setFolderPath(''); }}/></label>
        {folder.confirmed !== true && <label><input type="checkbox" aria-label="确认首次目录导出" disabled={readOnly} checked={confirmFolder} onChange={event => setConfirmFolder(event.target.checked)}/>确认首次目录导出</label>}
        <button type="button" disabled={!exportable || !directory.trim() || !Number.isInteger(folder.revision) || folder.confirmed !== true && !confirmFolder} onClick={exportFolder}>导出到文件夹</button>
        {folderPath && <p className="library-meta" role="status">{folderPath}</p>}
      </section>}
      <details className="library-grown"><summary>连接说明</summary>
        <Row readOnly title="Codex" sub={`.agents/skills/${document.name || '名称'}/SKILL.md`}/>
        <Row readOnly title="Claude Code" sub={`.claude/skills/${document.name || '名称'}/SKILL.md`}/>
        <p className="library-meta">将完整文件夹放入项目的对应目录，保留 references。</p>
      </details>
    </>}
  </FocusPanel>;
}
