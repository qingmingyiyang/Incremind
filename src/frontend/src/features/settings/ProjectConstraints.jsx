import { useEffect, useRef, useState } from 'react';
import { Row, Switch } from '../../shared/ui';
import { recognitionApi } from '../../shared/api/recognitionApi';

export function ProjectConstraints({ projectId, onChanged = () => {}, api = recognitionApi }) {
  const [items, setItems] = useState([]), [editor, setEditor] = useState(null), [content, setContent] = useState('');
  const [busy, setBusy] = useState(false), [error, setError] = useState('');
  const active = useRef(projectId), mounted = useRef(true);
  active.current = projectId;
  useEffect(() => {
    mounted.current = true;
    let current = true;
    setItems([]); setEditor(null); setBusy(false); setError('');
    api.loadConstraints({ projectId }).then(result => { if (current) setItems(result.items || []); })
      .catch(() => { if (current) setError('读取未完成 · 重试'); });
    return () => { current = false; mounted.current = false; };
  }, [projectId]);
  async function save(row, enabled, text = row.content) {
    if (busy) return;
    setBusy(true); setError('');
    try {
      const result = await api.saveConstraint({ projectId, constraintId: row.id, expectedRevision: row.revision,
        content: text, enabled, validFrom: row.valid_from || null, validUntil: row.valid_until || null });
      if (!mounted.current || active.current !== projectId) return;
      setItems(old => [...old.filter(item => item.id !== result.id), result]); setEditor(null); onChanged(); return true;
    } catch (failure) { if (mounted.current && active.current === projectId) setError(failure.status === 409 ? '内容已变化 · 重新读取' : '操作未完成 · 重试'); }
    finally { if (mounted.current && active.current === projectId) setBusy(false); }
  }
  return <section className="library-tools">
    {error && <p role="alert">{error}</p>}
    <div className="settings-constraints-head">约束 <span>{items.filter(row => row.enabled).length}/{items.length}</span></div>
    {editor && <form className="library-edit" onSubmit={event => { event.preventDefault(); save(editor, editor.enabled, content); }}>
      <label>约束正文<textarea aria-label="约束正文" value={content} onChange={event => setContent(event.target.value)}/></label>
      <button type="submit" disabled={busy || !content.trim()}>保存</button><button type="button" onClick={() => setEditor(null)}>取消</button>
    </form>}
    {items.map(row => <Row key={row.id} title={row.content} meta={row.valid_until ? String(row.valid_until).slice(0, 10) : '—'}
      onOpen={() => { setEditor(row); setContent(row.content); }}
      trailing={<Switch label={row.content} checked={row.enabled} disabled={busy} onChange={enabled => save(row, enabled)}/>}/>)}
    <form onSubmit={event => { event.preventDefault(); if (content.trim() && !editor) save({ id: `constraint-${crypto.randomUUID()}`, revision: 0, enabled: true }, true, content).then(saved => { if (saved) setContent(''); }); }}>
      {!editor && <label>新约束<input aria-label="新约束" value={content} disabled={busy} onChange={event => setContent(event.target.value)} onKeyDown={event => { if (event.key === 'Enter' && (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229)) event.preventDefault(); }}/></label>}
    </form>
  </section>;
}
