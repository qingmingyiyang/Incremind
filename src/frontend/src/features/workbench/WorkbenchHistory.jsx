import { useEffect, useState } from 'react';
import { FocusPanel, Icon, Row, StatusDot } from '../../shared/ui';
import { deviceIdentity, isDeviceIdentity } from '../../shared/api/deviceTransport';
import { scopedText } from '../../shared/lib/scopeTag';
import { formatLocalShortDate } from '../../shared/lib/time';
import { workbenchApi } from './workbenchApi';

const PAGE_SIZE = 12;
const PARALLEL_READS = 4;
const singleLine = text => typeof text === 'string' ? scopedText(text).text.replace(/\s+/g, ' ').trim() : '';
const snippetOf = text => text.length > 120 ? `${text.slice(0, 120)}…` : text;

function threadPreview(thread) {
  for (const turn of [...thread.turns].reverse()) {
    const text = singleLine(turn.receipt?.ask?.answer) || singleLine(turn.user_text);
    if (text) return snippetOf(text);
  }
  return '';
}

export function ThreadRow({ thread, preview, onOpen }) {
  return <Row className="workbench-history-row" dot={<Icon name="workbench" size={14}/>}
    title={singleLine(thread.title) || '对话'} sub={preview || undefined}
    meta={thread.updated_at ? formatLocalShortDate(thread.updated_at) : undefined} onOpen={onOpen}/>;
}

export function WorkbenchHistory({ projectId, threads, onOpen, onClose }) {
  const [query, setQuery] = useState(''), [limit, setLimit] = useState(PAGE_SIZE), [reload, setReload] = useState(0);
  const [previews, setPreviews] = useState({}), [errors, setErrors] = useState({});
  const search = query.trim().toLocaleLowerCase();
  const requested = search ? threads : threads.slice(0, limit);
  const key = row => `${row.id}:${row.updated_at || ''}`;
  const requestKey = requested.map(key).join('\0');

  useEffect(() => {
    const actor = deviceIdentity(), controller = new AbortController();
    const current = () => !controller.signal.aborted && isDeviceIdentity(actor);
    const missing = requested.filter(thread => !Object.hasOwn(previews, key(thread)));
    async function read() {
      for (let index = 0; index < missing.length && current(); index += PARALLEL_READS) {
        const batch = missing.slice(index, index + PARALLEL_READS);
        const rows = await Promise.allSettled(batch.map(thread => workbenchApi.thread(projectId, thread.id, { signal: controller.signal })));
        if (!current()) return;
        setPreviews(previous => ({ ...previous, ...Object.fromEntries(rows.flatMap((row, offset) => row.status === 'fulfilled'
          ? [[key(batch[offset]), threadPreview(row.value)]] : [])) }));
        setErrors(previous => ({ ...previous, ...Object.fromEntries(rows.map((row, offset) => [key(batch[offset]), row.status === 'rejected'])) }));
      }
    }
    read();
    return () => controller.abort();
  }, [projectId, requestKey, reload]);

  const loading = requested.some(row => !Object.hasOwn(previews, key(row)) && !errors[key(row)]);
  const failed = requested.some(row => errors[key(row)]);
  const matches = requested.filter(row => !search || `${singleLine(row.title)} ${previews[key(row)] || ''}`.toLocaleLowerCase().includes(search));
  return <FocusPanel title="全部对话" className="workbench-history-panel" onClose={onClose}>
    <label className="workbench-history-search"><Icon name="search" size={16}/><input type="search" aria-label="搜索对话"
      placeholder="搜标题或对话片段" value={query} onChange={event => setQuery(event.target.value)}/></label>
    {matches.map(thread => <ThreadRow key={thread.id} thread={thread} preview={previews[key(thread)]} onOpen={() => onOpen(thread.id)}/>)}
    {loading && <p role="status"><StatusDot state="processing"/> 正在读取片段</p>}
    {failed && <p role="alert">部分片段未读取 <button type="button" onClick={() => { setErrors({}); setReload(value => value + 1); }}>重试</button></p>}
    {!matches.length && !loading && <p>{search ? '未找到匹配的对话' : '暂无对话'}</p>}
    {!search && requested.length < threads.length && <button type="button" className="ui-filter" onClick={() => setLimit(value => value + PAGE_SIZE)}>显示更多</button>}
  </FocusPanel>;
}
