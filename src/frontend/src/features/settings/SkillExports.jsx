import { useEffect, useState } from 'react';
import { Row } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { libraryApi } from '../library/libraryApi';
import { SkillExportPanel, SkillNeedsUpdate } from '../library/SkillExportPanel';

export function SkillExports({ projectId, scenes = [] }) {
  const [open, setOpen] = useState(false), [items, setItems] = useState(null), [panel, setPanel] = useState(null);
  const [scene, setScene] = useState(''), [error, setError] = useState(''), [refresh, setRefresh] = useState(0);
  const gate = useRequestScope(projectId), scope = gate.scope;
  useEffect(() => {
    if (!open) return;
    const token = gate.issue('list', scope), controller = new AbortController();
    setError(''); setItems(null);
    libraryApi.skillExports(projectId, { signal: controller.signal }).then(value => {
      if (gate.isCurrent(token)) setItems(value.items || []);
    }).catch(() => { if (gate.isCurrent(token)) setError('读取未完成 · 重试'); });
    return () => controller.abort();
  }, [projectId, open, refresh]);
  return <section>
    <Row title="导出为 skill" expanded={open} meta={items ? String(items.length) : undefined} onOpen={() => { setOpen(old => !old); setPanel(null); }}/>
    {open && <section aria-label="skill 列表">
      {error && <p role="alert">{error} <button type="button" onClick={() => setRefresh(old => old + 1)}>重试</button></p>}
      {!items && !error && <span role="status">读取中</span>}
      {items?.map(row => <Row key={row.id} title={row.document.name} meta={row.needs_update ? <SkillNeedsUpdate/> : `v${row.document_version}`} onOpen={() => setPanel({ id: row.id, scene: row.scene })}/>)}
      <div className="library-actions"><label>场景<select aria-label="skill 场景" value={scene} onChange={event => setScene(event.target.value)}><option value="">项目</option>{scenes.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
        <button type="button" onClick={() => setPanel({ id: null, scene: scene || null })}>新建草稿</button></div>
      {panel && <SkillExportPanel key={`${projectId}:${panel.id || 'new'}:${panel.scene || ''}`} projectId={projectId} scene={panel.scene} exportId={panel.id} onClose={() => setPanel(null)} onChanged={() => setRefresh(old => old + 1)}/>}
    </section>}
  </section>;
}
