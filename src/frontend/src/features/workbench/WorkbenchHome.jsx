import { useEffect, useState } from 'react';
import { Icon, Row, StatusDot } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { formatScopeTag } from '../../shared/lib/scopeTag';
import { compareDateValues, formatLocalShortDate } from '../../shared/lib/time';
import { libraryApi } from '../library/libraryApi';
import { workbenchApi } from './workbenchApi';
import { ThreadRow } from './WorkbenchHistory';

const FIRST_USE_PROJECTS = 4;

async function checkFirstUse(projectIds, signal) {
  for (const project of projectIds) {
    if (signal.aborted) return false;
    const threads = await workbenchApi.threads(project, { signal });
    if (!Array.isArray(threads?.items) || threads.items.length) return false;
    const originals = await libraryApi.list('source', project, { signal });
    if (!Array.isArray(originals?.items) || originals.items.length) return false;
    const insights = await libraryApi.list('insight', project, { signal });
    if (!Array.isArray(insights?.items) || insights.items.length) return false;
  }
  return true;
}

export function WorkbenchHome({ projectId, projects, threads, threadListReady, onNavigate, onFirstInput, onHistory, hasDraft }) {
  const actor = deviceIdentity();
  const requests = useRequestScope(`${userStorageKey(projectId)}:${actor.revision}`), scope = requests.scope;
  const [loaded, setLoaded] = useState(null), [refresh, setRefresh] = useState(0);
  const [firstRead, setFirstRead] = useState(null);
  const value = loaded?.scope === scope ? loaded : null;
  const { pending, originals } = value || {};
  const project = projects.find(item => item.id === projectId);
  const otherProjects = projects.filter(item => item.id !== projectId).map(item => item.id).join('\0');
  const firstKey = `${scope}:${otherProjects}`;
  const mayBeFirst = threadListReady && projectId === 'default' && projects.length > 0 && !threads.length
    && originals?.length === 0 && value?.insightCount === 0;

  useEffect(() => {
    const token = requests.issue('home', scope), controller = new AbortController();
    const read = layer => libraryApi.list(layer, projectId, { signal: controller.signal });
    Promise.allSettled([read('insight'), read('source')]).then(([insights, sources]) => {
      if (!requests.isCurrent(token) || !isDeviceIdentity(actor)) return;
      setLoaded({ scope,
        insightCount: insights.status === 'fulfilled' && Array.isArray(insights.value?.items) ? insights.value.items.length : null,
        pending: insights.status === 'fulfilled' && Array.isArray(insights.value?.items)
          ? insights.value.items.filter(item => item.state === 'pending') : null,
        originals: sources.status === 'fulfilled' && Array.isArray(sources.value?.items)
          ? [...sources.value.items].sort((left, right) => compareDateValues(right.created_at, left.created_at)) : null });
    });
    return () => { controller.abort(); requests.invalidate('home'); };
  }, [projectId, scope, refresh]);

  useEffect(() => {
    if (!mayBeFirst) return;
    const ids = otherProjects ? otherProjects.split('\0') : [];
    if (ids.length > FIRST_USE_PROJECTS) return;
    const token = requests.issue('first-use', scope), controller = new AbortController();
    checkFirstUse(ids, controller.signal).then(first => {
      if (requests.isCurrent(token) && isDeviceIdentity(actor)) setFirstRead({ key: firstKey, first });
    }).catch(() => {
      if (requests.isCurrent(token) && isDeviceIdentity(actor)) setFirstRead({ key: firstKey, first: false });
    });
    return () => { controller.abort(); requests.invalidate('first-use'); };
  }, [mayBeFirst, firstKey, scope]);

  const fresh = mayBeFirst && firstRead?.key === firstKey && firstRead.first;
  const current = pending?.[0];
  const review = () => onNavigate?.('library', { layer: 'insight', filter: 'pending_memory', review: '1' });
  return <>
    {fresh && !hasDraft && <div className="workbench-first-action">
      <button type="button" className="workbench-primary" onClick={onFirstInput}>记一条</button>
      <span>文字、链接和文件都可以先保存原件。</span>
      <button type="button" onClick={() => onNavigate?.('settings', { section: 'project', create_project: '1' })}>建项目</button>
    </div>}
    {value && (pending === null || originals === null) && <div role="alert" className="workbench-error">资料未读取 <button type="button" onClick={() => setRefresh(old => old + 1)}>重试</button></div>}
    {(current || Boolean(threads.length) || Boolean(originals?.length)) && <div className="workbench-home-grid">
      {current && <section aria-label="待确认">
        <h2><StatusDot state="pending"/>待确认 <span className="ui-count">{pending.length}</span>
          <button type="button" onClick={review}>逐条确认 <Icon name="arrow-up-right" size={12}/></button>
        </h2>
        <Row title={current.text} sub={formatScopeTag(project?.name || projectId, current.scene || null)} onOpen={review}/>
      </section>}
      {!!threads.length && <section aria-label="最近对话">
        <h2>最近对话<button type="button" onClick={onHistory}>全部对话 <Icon name="arrow-up-right" size={12}/></button></h2>
        {threads.slice(0, 4).map(thread => <ThreadRow key={thread.id} thread={thread}
          onOpen={() => onNavigate?.('workbench', { thread_id: thread.id })}/>)}
      </section>}
      {!!originals?.length && <section aria-label="最近原件">
        <h2>最近原件</h2>
        {originals.slice(0, 3).map(original => <Row key={original.id} title={original.title}
          meta={original.created_at ? formatLocalShortDate(original.created_at) : null}
          onOpen={() => onNavigate?.('library', { layer: 'source', source_id: original.id })}/>)}
      </section>}
    </div>}
  </>;
}
