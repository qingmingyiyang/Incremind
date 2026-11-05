import { useEffect, useRef, useState } from 'react';
import { Row } from '../../shared/ui';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { beginLexiconDraft, editLexiconDraft, lexiconDraftOperations, reconcileLexiconDraft, reviewLexiconDraft } from './lexiconDraft';

const describe = term => `${term.term} · 权重 ${term.weight} · ${term.status === 'active' ? '启用' : '停用'}`;

export function LexiconTermEditor({ term, revision, userSpace, projectId, api, disabled, onChanged }) {
  const gate = useRequestScope(`${userSpace}:${projectId}:${term.id}`), scope = gate.scope;
  const [open, setOpen] = useState(false), [draft, setDraft] = useState(() => beginLexiconDraft(term, revision));
  const { name, weight } = draft;
  const [preview, setPreview] = useState(null), [busy, setBusy] = useState(false), [error, setError] = useState('');
  const [unknown, setUnknown] = useState(false), controller = useRef(null), pending = useRef(false);
  const [needsRead, setNeedsRead] = useState(false);
  const current = token => userStorageKey('asr-lexicon') === userSpace && gate.isCurrent(token);
  useEffect(() => {
    setDraft(beginLexiconDraft(term, revision)); setPreview(null); setError(''); setUnknown(false); setNeedsRead(false); setBusy(false); pending.current = false;
    return () => { controller.current?.abort(); gate.invalidate('term'); };
  }, [scope]);
  useEffect(() => {
    setDraft(value => reconcileLexiconDraft(value, term, revision)); setPreview(null);
    controller.current?.abort(); gate.invalidate('term'); pending.current = false; setBusy(false);
  }, [revision, term.term, term.weight, term.status]);
  const edit = changes => { setPreview(null); setError(''); setDraft(value => editLexiconDraft(value, changes)); };
  async function perform(action) {
    if (busy || pending.current || disabled || action !== 'read' && (unknown || needsRead || draft.conflict)) return;
    const token = gate.issue('term', scope); if (!token || !current(token)) return;
    const request = new AbortController(); controller.current = request; pending.current = true; setBusy(true); setError('');
    try {
      if (action === 'read') {
        const value = await api.loadAsrLexicon(request.signal);
        if (current(token)) {
          const fresh = value.terms.find(row => row.id === term.id);
          if (!fresh) throw new Error('lexicon_term_unavailable');
          setDraft(old => reconcileLexiconDraft(old, fresh, value.revision));
          setPreview(null); setUnknown(false); setNeedsRead(false); onChanged(value); setError('已读取当前词表 · 重新预览后确认');
        }
        return;
      }
      const reviewed = draft.stale ? reviewLexiconDraft(draft, 'keep') : draft;
      const operations = action === 'confirm' ? preview?.operations : lexiconDraftOperations(reviewed, action);
      if (!operations || action === 'confirm' && unknown) return;
      if (action === 'confirm' && (preview.revision !== revision || draft.stale)) { setPreview(null); setNeedsRead(true); return; }
      if (action === 'confirm') setUnknown(true);
      const value = await api.changeAsrLexicon({ operations, expected_revision: action === 'confirm' ? preview.revision : reviewed.base.revision,
        preview: action !== 'confirm', confirm: action === 'confirm' }, request.signal);
      if (!current(token)) return;
      if (action !== 'confirm') { setDraft(reviewed); setPreview({ ...value, operations }); }
      else {
        setPreview(null); setUnknown(false);
        const updated = await api.loadAsrLexicon(request.signal);
        if (current(token)) {
          const fresh = updated.terms.find(row => row.id === term.id);
          if (!fresh) throw new Error('lexicon_term_unavailable');
          setDraft(old => ['retire', 'restore'].includes(operations[0].action) && old.dirty
            ? reconcileLexiconDraft(old, fresh, updated.revision) : beginLexiconDraft(fresh, updated.revision));
          onChanged(updated); setOpen(false);
        }
      }
    } catch (reason) {
      if (current(token)) {
        if (reason.status === 409) { setPreview(null); setUnknown(false); setNeedsRead(true); setError('词表已变化 · 读取当前词表后重新预览'); }
        else if (reason.status === 400) { setPreview(null); setUnknown(false); setError('词语或权重有误 · 检查后预览'); }
        else setError(action === 'confirm' ? '保存结果待核对 · 读取当前词表' : '操作未完成 · 重试');
      }
    } finally { if (current(token)) { pending.current = false; setBusy(false); controller.current = null; } }
  }
  return <section>
    <Row title={term.term} expanded={open} onOpen={() => setOpen(value => !value)}
      meta={`权重 ${term.weight} · ${term.status === 'active' ? '启用' : '停用'}`}
      trailing={<button type="button" disabled={disabled || busy || unknown || needsRead || draft.conflict} onClick={() => { setOpen(true); perform('toggle'); }}>{term.status === 'active' ? '停用' : '启用'}</button>}/>
    {open && <div className="settings-expand" aria-busy={busy}>
      <div className="settings-fields"><label>词语<input aria-label={`编辑热词 ${term.term}`} value={name} disabled={disabled || busy || unknown}
        onChange={event => edit({ name: event.target.value })}/></label>
      <label>权重<select aria-label={`${term.term} 权重`} value={weight} disabled={disabled || busy || unknown || term.status !== 'active'}
        onChange={event => edit({ weight: Number(event.target.value) })}>{[1, 2, 3, 4, 5, 50].map(value => <option key={value} value={value}>{value === 50 ? '重点词 · 50' : value}</option>)}</select></label></div>
      {draft.stale && <p role="status">词表已变化 · 输入保留 · 重新预览后确认</p>}
      {draft.conflict && <><Row readOnly title="当前版本" sub={describe(draft.current)}/><div className="settings-asr-actions">
        <button type="button" disabled={busy || unknown || needsRead} onClick={() => setDraft(value => reviewLexiconDraft(value, 'current'))}>采用当前版本</button>
        <button type="button" disabled={busy || unknown || needsRead} onClick={() => setDraft(value => reviewLexiconDraft(value, 'keep'))}>保留输入</button></div></>}
      <div className="settings-asr-actions"><button type="button" disabled={disabled || busy || unknown || needsRead || draft.conflict || !name.trim() || term.status !== 'active'
        || name.trim() === term.term && weight === term.weight} onClick={() => perform('preview')}>{draft.stale ? '重新预览' : '预览修改'}</button>
        {preview && !unknown && !draft.stale && preview.revision === revision && <><button type="button" disabled={disabled || busy} title={`词表 r${preview.revision}`} onClick={() => perform('confirm')}>确认保存</button>
          <button type="button" disabled={busy} onClick={() => setPreview(null)}>取消</button></>}
        {(unknown || needsRead || error) && <button type="button" disabled={busy} onClick={() => perform('read')}>读取当前词表</button>}
      </div>
      {preview && <div aria-label="热词变更预览">{preview.changes.map((change, index) => <Row readOnly key={index}
        title={describe(change.before)} sub={`→ ${describe(change.after)}`}/>)}</div>}
      {error && <p role="alert">{error}</p>}
    </div>}
  </section>;
}
