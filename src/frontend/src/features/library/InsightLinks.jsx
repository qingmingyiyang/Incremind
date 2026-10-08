import { useEffect, useState } from 'react';
import { Icon } from '../../shared/ui/Icon';
import { libraryApi } from './libraryApi';

const names = { related: '相关', supports: '支持', refutes: '矛盾', supersedes: '取代' };
export function InsightLinks({ projectId, insightId, insights = [], onOpen, onReviewed, readonly = false }) {
  const [links, setLinks] = useState([]);
  const [expanded, setExpanded] = useState(null);
  const [proposals, setProposals] = useState({});
  const [texts, setTexts] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setLinks([]); setExpanded(null); setProposals({}); setTexts({}); setError('');
    libraryApi.links(projectId, insightId, { signal: controller.signal }).then(result => {
      if (!controller.signal.aborted) setLinks(result.links || []);
    }).catch(() => { if (!controller.signal.aborted) setError('读取未完成 · 重试'); });
    return () => controller.abort();
  }, [projectId, insightId, refresh]);
  useEffect(() => {
    if (!expanded) return;
    const controller = new AbortController();
    if (links.some(row => row.kind === expanded && row.state === 'suggested')) {
      libraryApi.linkProposals(projectId, { signal: controller.signal }).then(result => {
        if (!controller.signal.aborted) setProposals(Object.fromEntries((result.proposals || []).map(row => [row.id, row])));
      }).catch(reason => { if (!controller.signal.aborted) setError(reason.message); });
    }
    const missing = [...new Set(links.filter(row => row.kind === expanded).map(row => row.other_id))]
      .filter(id => !insights.some(row => row.id === id));
    if (missing.length) {
      // An omitted neighbor may be filtered locally or belong to the persona.
      // Resolve its text from the allowed, unfiltered scopes without a failed drill probe.
      const scopes = projectId === 'me' ? ['me'] : [projectId, 'me'];
      Promise.all(scopes.map(project => libraryApi.list('insight', project, { q: '', signal: controller.signal })))
        .then(results => {
          if (controller.signal.aborted) return;
          const rows = results.flatMap(result => result.items || []);
          const found = Object.fromEntries(missing.map(id => [id, rows.find(row => row.id === id)]));
          setTexts(old => ({ ...old, ...found }));
          if (missing.some(id => !found[id])) setError('读取未完成 · 重试');
        }).catch(() => { if (!controller.signal.aborted) setError('读取未完成 · 重试'); });
    }
    return () => controller.abort();
  }, [expanded, projectId, links, insights]);
  async function review(row, accept) {
    if (readonly || busy || !proposals[row.id]) return;
    setBusy(true); setError('');
    try {
      await libraryApi.reviewLink(projectId, row.id, proposals[row.id].revision, accept);
      setLinks(value => accept ? value.map(link => link.id === row.id ? { ...link, state: 'active' } : link) : value.filter(link => link.id !== row.id));
      onReviewed?.();
    } catch (reason) { setError(reason.message); }
    finally { setBusy(false); }
  }
  if (!links.length && !error) return null;
  return <section aria-label="认识连接" className="library-grown library-insight-links">
    <div className="library-actions">{Object.entries(names).map(([kind, label]) => {
      const count = links.filter(row => row.kind === kind).length;
      return count ? <button type="button" key={kind} aria-label={`${label} ${count}`} aria-expanded={expanded === kind} onClick={() => setExpanded(value => value === kind ? null : kind)}><Icon name={kind} size={16}/>{count}</button> : null;
    })}</div>
    {links.filter(row => row.kind === expanded).map(row => {
      const other = insights.find(value => value.id === row.other_id) || texts[row.other_id];
      return <div key={row.id}>
        {insights.some(value => value.id === row.other_id) && onOpen ? <button type="button" onClick={() => onOpen(other)}>{other?.text}</button> : <p>{other?.text || '读取中'}</p>}
        {row.state === 'suggested' && <>
          {proposals[row.id]?.evidence && <p className="library-meta">{proposals[row.id].evidence}</p>}
          {!readonly && <div className="library-actions"><button type="button" aria-label="确认连接" disabled={busy || !proposals[row.id]} onClick={() => review(row, true)}>确认</button><button type="button" aria-label="忽略连接" disabled={busy || !proposals[row.id]} onClick={() => review(row, false)}>忽略</button></div>}
        </>}
      </div>;
    })}
    {error && <p role="alert">{error}<button type="button" onClick={() => setRefresh(value => value + 1)}>刷新</button></p>}
  </section>;
}
