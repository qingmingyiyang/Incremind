import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from '@modelcontextprotocol/ext-apps';
import { Row } from '../src/shared/ui/Row';
import 'virtual:incremind-theme.css';
import './styles.css';

function MemoryCard() {
  const bridge = useRef(null), sequence = useRef(0);
  const [value, setValue] = useState(null), [meta, setMeta] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  useEffect(() => {
    const app = new App({ name: 'Incremind Memory', version: '1.0.0' }, {});
    bridge.current = app;
    app.ontoolresult = result => { sequence.current += 1; setValue(result.structuredContent); setMeta(result._meta?.native_mcp || null); setError(result.isError ? '记忆操作未完成' : ''); setBusy(false); };
    app.onhostcontextchanged = context => { if (context.theme) document.documentElement.dataset.theme = context.theme; };
    app.connect().catch(() => setError('无法连接当前应用宿主'));
    return () => { sequence.current += 1; bridge.current = null; app.close(); };
  }, []);
  async function read(entry) {
    if (!bridge.current || busy || !value?.turn_id) return;
    const id = ++sequence.current; setBusy(true); setError('');
    try {
      const response = await bridge.current.callServerTool({ name: 'read', arguments: { id: { turn_id: value.turn_id, id: entry.id } } });
      if (sequence.current !== id) return;
      if (response.isError) throw new Error('记忆读取未完成');
      setValue(response.structuredContent);
    } catch (reason) { if (sequence.current === id) setError(reason.message); }
    finally { if (sequence.current === id) setBusy(false); }
  }
  async function openReview() {
    if (!bridge.current || busy || !meta?.review_url) return;
    const id = ++sequence.current; setBusy(true); setError('');
    try {
      const result = await bridge.current.openLink({ url: meta.review_url });
      if (sequence.current === id && result.isError) setError('主应用尚未打开');
    } catch { if (sequence.current === id) setError('主应用尚未打开'); }
    finally { if (sequence.current === id) setBusy(false); }
  }
  const result = value?.result;
  return <section className="native-memory-card" aria-label="记忆结果">
    <h2>{result?.candidate_id ? '待确认认识' : '召回记忆'}</h2>
    {error && <p role="alert">{error}</p>}
    {!result && !error && <p role="status">等待真实记忆结果</p>}
    {result?.candidate_id ? <>
      <p>已提出待确认认识 · 修订 {result.revision}</p><p>进入主应用查看内容并决定确认或丢弃。</p>
      {meta?.review_url && <button type="button" disabled={busy} onClick={openReview}>打开主应用审核</button>}
    </> : <>
      {[...(result?.entries || []), ...(result?.profile || [])].map(entry => <article key={entry.id}><Row readOnly title={`${entry.id} · ${entry.title}`} sub={entry.conditions?.join(' · ')}/><p>{entry.excerpt}</p><button type="button" disabled={busy} onClick={() => read(entry)}>查看证据</button></article>)}
      {result && !(result.entries?.length || result.profile?.length) && <p>本次没有可交付的记忆</p>}
    </>}
  </section>;
}

createRoot(document.getElementById('root')).render(<MemoryCard/>);
