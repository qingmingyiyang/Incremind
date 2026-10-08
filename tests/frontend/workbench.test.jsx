import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { Workbench } from '@src/features/workbench/Workbench';
const insight = { id: 'i1', text: '保留数字', state: 'pending', revision: 3 };
const turn = (state = 'done') => ({ id: 'turn-1', thread_id: 'thread-1', intent: 'remember', user_text: '材料', receipt: { remember: { title: '材料标题', state, progress: { done: state === 'done' ? 4 : 1, total: 4 }, insights: [insight], related: [] } } });
let current, threads;
beforeEach(() => {
 localStorage.clear(); current = []; threads = [];
 vi.stubGlobal('fetch', vi.fn(async (url, options = {}) => {
  const path = String(url);
  let value = path.endsWith('/turns') ? { thread_id: 'thread-1', turn: turn('processing') }
   : path.includes('/confirm') ? { ...insight, state: 'active' }
   : path.includes('/drop') ? { dropped: true }
   : path.includes('/threads/') ? { id: 'thread-1', turns: current } : { items: threads };
  return { ok: true, json: async () => value };
 }));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });
const props = { projectId: 'alpha', projects: [{ id: 'alpha', name: '甲' }, { id: 'beta', name: '乙' }, { id: 'inbox', name: '收件箱' }] };
it('shows the empty title and sends the Composer intent without composition submission', async () => {
 render(<Workbench {...props}/>); await act(async () => {});
 expect(screen.getByText('今天想记住什么？')).toBeInTheDocument();
 const input = screen.getByRole('textbox', { name: '输入' });
 fireEvent.change(input, { target: { value: 'https://example.com' } });
 fireEvent.compositionStart(input); fireEvent.keyDown(input, { key: 'Enter' });
 expect(fetch.mock.calls.filter(([url]) => String(url).endsWith('/turns'))).toHaveLength(0);
 fireEvent.compositionEnd(input); fireEvent.keyDown(input, { key: 'Enter' }); await act(async () => {});
 const request = fetch.mock.calls.find(([url]) => String(url).endsWith('/turns'));
 expect(JSON.parse(request[1].body)).toEqual({ project_id: 'alpha', text: 'https://example.com', intent: 'remember' });
 expect(screen.getByRole('img', { name: '原件 · 正文 · 整理 · 入库' })).toBeInTheDocument();
 expect(screen.getByText('材料标题')).toHaveAttribute('title', '材料标题');
});
it('loads a thread and confirms by exact project and revision', async () => {
 threads = [{ id: 'thread-1', title: '材料' }]; current = [turn()];
 render(<Workbench {...props}/>); await act(async () => {});
 fireEvent.click(screen.getByRole('button', { name: '确认' })); await act(async () => {});
 expect(JSON.parse(fetch.mock.calls.find(([url]) => String(url).includes('/confirm'))[1].body)).toEqual({ project_id: 'alpha', expected_revision: 3 });
 expect(screen.getByLabelText('已确认')).toBeInTheDocument();
});
it('navigates to a new thread after starting a new conversation from an old thread URL', async () => {
 threads = [{ id: 'thread-old', title: '旧对话' }]; current = [{ ...turn(), thread_id: 'thread-old' }];
 const navigate = vi.fn(); render(<Workbench {...props} threadId="thread-old" turnId="turn-old" onNavigate={navigate}/>); await act(async () => {});
 fireEvent.click(screen.getByRole('button', { name: '新对话' })); fireEvent.change(screen.getByRole('textbox'), { target: { value: '新材料' } }); fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
 const body = JSON.parse(fetch.mock.calls.find(([url]) => String(url).endsWith('/turns'))[1].body);
 expect(body).not.toHaveProperty('thread_id'); expect(navigate).toHaveBeenCalledWith('workbench', { project_id: 'alpha', thread_id: 'thread-1' });
 expect(JSON.parse(localStorage.getItem('chriptmas-v2-thread:alpha'))).toBe('thread-1');
});
it('keeps the current URL when adding a turn to the same thread', async () => {
 threads = [{ id: 'thread-1', title: '材料' }]; current = [turn()];
 const navigate = vi.fn(); render(<Workbench {...props} threadId="thread-1" onNavigate={navigate}/>); await act(async () => {});
 fireEvent.change(screen.getByRole('textbox'), { target: { value: '更多材料' } }); fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
 expect(JSON.parse(fetch.mock.calls.find(([url]) => String(url).endsWith('/turns'))[1].body)).toMatchObject({ thread_id: 'thread-1' }); expect(navigate).not.toHaveBeenCalled();
});
it('drops an inspiration with exact revision and removes its chip', async () => {
 threads = [{ id: 'thread-1', title: '灵感' }]; current = [{ ...turn(), intent: 'inspiration', receipt: { inspiration: { insight } } }];
 render(<Workbench {...props}/>); await act(async () => {}); fireEvent.click(screen.getByRole('button', { name: '丢弃' })); await act(async () => {});
 expect(JSON.parse(fetch.mock.calls.find(([url]) => String(url).includes('/drop'))[1].body)).toEqual({ project_id: 'alpha', expected_revision: 3 });
 expect(screen.queryByText('保留数字')).not.toBeInTheDocument();
});
it('ignores a late old project thread and does not display it in the next project', async () => {
 let resolveAlpha;
 fetch.mockImplementation(url => String(url).includes('project_id=alpha') ? new Promise(resolve => { resolveAlpha = resolve; }) : Promise.resolve({ ok: true, json: async () => ({ items: [] }) }));
 const { rerender } = render(<Workbench {...props}/>); await act(async () => {});
 rerender(<Workbench {...props} projectId="beta"/>); await act(async () => {});
 await act(async () => resolveAlpha({ ok: true, json: async () => ({ items: [{ id: 'old', title: '旧材料' }] }) }));
 expect(screen.queryByText('旧材料')).not.toBeInTheDocument();
 expect(fetch.mock.calls.some(([url]) => String(url).includes('/threads/old'))).toBe(false);
});
it('uploads an attached file through the workbench endpoint before creating its turn', async () => {
 fetch.mockImplementation(async (url) => ({ ok: true, json: async () => String(url).endsWith('/files') ? { id: 'item-file' } : String(url).endsWith('/turns') ? { thread_id: 'thread-1', turn: { ...turn('processing'), user_text: '' } } : { items: [] } }));
 const { container } = render(<Workbench {...props}/>); await act(async () => {});
 const file = new File(['text'], '笔记.txt'); fireEvent.change(container.querySelector('input[type=file]'), { target: { files: [file] } });
 fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
 const upload = fetch.mock.calls.find(([url]) => String(url).endsWith('/files'));
 expect(upload[1].body.get('project_id')).toBe('alpha'); expect(upload[1].body.get('file').name).toBe('笔记.txt');
 expect(JSON.parse(fetch.mock.calls.find(([url]) => String(url).endsWith('/turns'))[1].body)).toMatchObject({ item_id: 'item-file', intent: 'remember' });
 expect(container.querySelector('.workbench-bubble')).toBeNull();
});
it('combines selected images in their order into one original and one turn', async () => {
 fetch.mockImplementation(async url => ({ ok: true, json: async () => String(url).endsWith('/files') ? { id: 'image-group' } : String(url).endsWith('/turns') ? { thread_id: 'thread-1', turn: { ...turn('processing'), user_text: '' } } : { items: [] } }));
 const { container } = render(<Workbench {...props}/>); await act(async () => {});
 const files = [new File(['one'], 'first.png', { type: 'image/png' }), new File(['two'], 'second.jpg', { type: 'image/jpeg' })];
 fireEvent.change(container.querySelector('input[type=file]'), { target: { files } });
 fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
 const uploads = fetch.mock.calls.filter(([url]) => String(url).endsWith('/files'));
 expect(uploads).toHaveLength(1);
 expect(uploads[0][1].body.get('file').name).toBe('first.png');
 expect(uploads[0][1].body.getAll('files').map(file => file.name)).toEqual(['second.jpg']);
 const turns = fetch.mock.calls.filter(([url]) => String(url).endsWith('/turns'));
 expect(turns).toHaveLength(1);
 expect(JSON.parse(turns[0][1].body)).toMatchObject({ item_id: 'image-group', intent: 'remember' });
});
it('keeps rejected input for correction without creating a turn', async () => {
 render(<Workbench {...props}/>); await act(async () => {});
 const input = screen.getByRole('textbox'); fireEvent.change(input, { target: { value: '#未知 材料' } }); fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
 expect(screen.getByRole('alert')).toHaveTextContent('项目未找到 · 修改标签'); expect(input).toHaveValue('#未知 材料');
 expect(fetch.mock.calls.some(([url]) => String(url).endsWith('/turns'))).toBe(false);
});
it('ignores a pending initial list after sending a new turn', async () => {
 let resolveList;
 fetch.mockImplementation(async url => String(url).endsWith('/turns') ? { ok: true, json: async () => ({ thread_id: 'thread-1', turn: turn('processing') }) } : new Promise(resolve => { resolveList = resolve; }));
 render(<Workbench {...props}/>); await act(async () => {});
 fireEvent.change(screen.getByRole('textbox'), { target: { value: '材料' } }); fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
 await act(async () => resolveList({ ok: true, json: async () => ({ items: [] }) }));
 expect(screen.getByText('材料标题')).toBeInTheDocument();
});
it('stops a recorded stream and attaches its audio file', async () => {
 const stopTrack = vi.fn(), stream = { getTracks: () => [{ stop: stopTrack }] };
 Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: vi.fn(async () => stream) } });
 class Recorder { constructor(value) { this.stream = value; this.state = 'inactive'; this.mimeType = 'audio/webm'; } start() { this.state = 'recording'; } stop() { this.state = 'inactive'; this.ondataavailable?.({ data: new Blob(['audio']) }); this.onstop?.(); } }
 vi.stubGlobal('MediaRecorder', Recorder);
 render(<Workbench {...props}/>); await act(async () => {}); fireEvent.click(screen.getByRole('button', { name: '录音' })); await act(async () => {});
 expect(screen.getByRole('button', { name: '发送' })).toBeDisabled(); fireEvent.click(screen.getByRole('button', { name: '停止录音' })); await act(async () => {});
 expect(stopTrack).toHaveBeenCalledTimes(1); expect(screen.getByRole('button', { name: /移除 录音-/ })).toBeInTheDocument();
});
it('stops the stream on project changes without keeping old recordings', async () => {
 const stopTrack = vi.fn(), stopRecorder = vi.fn();
 Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop: stopTrack }] })) } });
 class Recorder { constructor(stream) { this.stream = stream; this.state = 'inactive'; } start() { this.state = 'recording'; } stop() { stopRecorder(); this.state = 'inactive'; this.onstop?.(); } }
 vi.stubGlobal('MediaRecorder', Recorder);
 const { rerender } = render(<Workbench {...props}/>); await act(async () => {}); fireEvent.click(screen.getByRole('button', { name: '录音' })); await act(async () => {});
 rerender(<Workbench {...props} projectId="beta"/>); await act(async () => {});
 expect(stopTrack).toHaveBeenCalledTimes(1); expect(stopRecorder).toHaveBeenCalledTimes(1); expect(screen.getByRole('button', { name: '录音' })).toBeInTheDocument();
});
it('releases acquired audio tracks when recorder construction fails', async () => {
 const stopTrack = vi.fn(); Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia: vi.fn(async () => ({ getTracks: () => [{ stop: stopTrack }] })) } });
 vi.stubGlobal('MediaRecorder', class { constructor() { throw new Error('unavailable'); } });
 render(<Workbench {...props}/>); await act(async () => {}); fireEvent.click(screen.getByRole('button', { name: '录音' })); await act(async () => {});
 expect(stopTrack).toHaveBeenCalledTimes(1); expect(screen.getByRole('alert')).toHaveTextContent('录音不可用 · 添加文件');
});
it('ignores a late poll after confirming an insight', async () => {
 vi.useFakeTimers(); threads = [{ id: 'thread-1', title: '材料' }]; current = [turn('processing')];
 render(<Workbench {...props}/>); await act(async () => {});
 let resolvePoll;
 fetch.mockImplementation(url => String(url).includes('/confirm') ? Promise.resolve({ ok: true, json: async () => ({ ...insight, state: 'active' }) }) : new Promise(resolve => { resolvePoll = resolve; }));
 await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
 fireEvent.click(screen.getByRole('button', { name: '确认' })); await act(async () => {});
 await act(async () => resolvePoll({ ok: true, json: async () => ({ turns: [turn('processing')] }) }));
 expect(screen.getByLabelText('已确认')).toBeInTheDocument(); expect(screen.queryByRole('button', { name: '确认' })).not.toBeInTheDocument();
});
it('polls a processing thread at 1.5 seconds and stops when settled', async () => {
 vi.useFakeTimers(); threads = [{ id: 'thread-1', title: '材料' }]; current = [turn('processing')];
 render(<Workbench {...props}/>); await act(async () => {});
 const calls = () => fetch.mock.calls.filter(([url]) => String(url).includes('/threads/thread-1')).length;
 expect(calls()).toBe(1); await act(async () => { await vi.advanceTimersByTimeAsync(1499); }); expect(calls()).toBe(1);
 current = [turn()]; await act(async () => { await vi.advanceTimersByTimeAsync(1); }); expect(calls()).toBe(2);
 await act(async () => { await vi.advanceTimersByTimeAsync(10000); }); expect(calls()).toBe(2);
});
it('sends untagged inspiration to inbox and navigates to its actual scope', async () => {
 const navigate = vi.fn(); render(<Workbench {...props} onNavigate={navigate}/>); await act(async () => {});
 fireEvent.change(screen.getByRole('textbox'), { target: { value: '灵感 数字更清晰' } }); fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
 expect(JSON.parse(fetch.mock.calls.find(([url]) => String(url).endsWith('/turns'))[1].body)).toMatchObject({ project_id: 'inbox', intent: 'inspiration' });
 expect(navigate).toHaveBeenCalledWith('workbench', { project_id: 'inbox', thread_id: 'thread-1' });
});
it('resolves project names and scene tags before uploading files', async () => {
 const navigate = vi.fn(); render(<Workbench {...props} onNavigate={navigate}/>); await act(async () => {});
 fireEvent.change(screen.getByRole('textbox'), { target: { value: '#乙/工作 材料' } });
 expect(screen.getByText('#乙/工作')).toBeInTheDocument(); fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
 expect(JSON.parse(fetch.mock.calls.find(([url]) => String(url).endsWith('/turns'))[1].body)).toMatchObject({ project_id: 'beta', text: '#乙/工作 材料' });
 expect(navigate).toHaveBeenCalledWith('workbench', { project_id: 'beta', thread_id: 'thread-1' });
});
it('resolves the builtin default project from another scope when no default record is loaded', async () => {
 const navigate = vi.fn(); render(<Workbench projectId="me" projects={[{ id: 'me', name: '我' }]} onNavigate={navigate}/>); await act(async () => {});
 fireEvent.change(screen.getByRole('textbox'), { target: { value: '#默认/阅读 灵感 数字更清晰' } }); fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
 expect(JSON.parse(fetch.mock.calls.find(([url]) => String(url).endsWith('/turns'))[1].body)).toMatchObject({ project_id: 'default', intent: 'inspiration' });
 expect(navigate).toHaveBeenCalledWith('workbench', { project_id: 'default', thread_id: 'thread-1' });
});
it('uses the actual renamed builtin project and rejects its old display name', async () => {
 const navigate = vi.fn(); render(<Workbench projectId="me" projects={[{ id: 'me', name: '我' }, { id: 'inbox', name: '待整理' }]} onNavigate={navigate}/>); await act(async () => {});
 const input = screen.getByRole('textbox'); fireEvent.change(input, { target: { value: '#收件箱 灵感 材料' } }); fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
 expect(screen.getByRole('alert')).toHaveTextContent('项目未找到 · 修改标签'); expect(input).toHaveValue('#收件箱 灵感 材料');
 expect(fetch.mock.calls.some(([url]) => String(url).endsWith('/turns'))).toBe(false);
 fireEvent.change(input, { target: { value: '#待整理 灵感 材料' } }); fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
 expect(navigate).toHaveBeenCalledWith('workbench', { project_id: 'inbox', thread_id: 'thread-1' });
});

it('uploads multiple files independently into the same workbench thread', async () => {
 let uploaded = 0, submitted = 0;
 fetch.mockImplementation(async (url) => ({ ok: true, json: async () => String(url).endsWith('/files')
  ? { id: `item-file-${++uploaded}` }
  : String(url).endsWith('/turns') ? { thread_id: 'thread-batch', turn: { ...turn('processing'), id: `turn-${++submitted}`, user_text: '' } } : { items: [] } }));
 const { container } = render(<Workbench {...props}/>); await act(async () => {});
 const files = [new File(['one'], 'one.txt'), new File(['two'], 'two.md')];
 fireEvent.change(container.querySelector('input[type=file]'), { target: { files } });
 fireEvent.click(screen.getByRole('button', { name: '发送' })); await act(async () => {});
 const uploads = fetch.mock.calls.filter(([url]) => String(url).endsWith('/files'));
 const bodies = fetch.mock.calls.filter(([url]) => String(url).endsWith('/turns')).map(([, options]) => JSON.parse(options.body));
 expect(uploads.map(([, options]) => options.body.get('file').name)).toEqual(['one.txt', 'two.md']);
 expect(bodies).toHaveLength(2);
 expect(bodies[0]).toMatchObject({ project_id: 'alpha', item_id: 'item-file-1', intent: 'remember' });
 expect(bodies[1]).toMatchObject({ project_id: 'alpha', thread_id: 'thread-batch', item_id: 'item-file-2', intent: 'remember' });
 expect(submitted).toBe(2);
});

it('opens a delivered kernel result without an approval action', async () => {
 threads = [{ id: 'thread-1', title: '任务' }];
 const task = state => ({ ...turn(), intent: 'do', receipt: { do: { kernel_turn_id: 'kernel-1', task_id: null, title: '任务标题', state, progress: { done: state === 'done' ? 3 : 1, total: 3 }, document_id: state === 'done' ? 'doc-1' : null } } });
 current = [task('done')];
 fetch.mockImplementation(async (url, options) => {
  if (String(url).includes('/documents/')) return { ok: true, json: async () => ({ markdown: '# 实际成果\n\n成果正文' }) };
  return { ok: true, json: async () => String(url).includes('/threads/') ? { turns: current } : { items: threads } };
 });
 render(<Workbench {...props}/>); await act(async () => {});
 expect(fetch.mock.calls.some(([url]) => String(url).includes('/approve'))).toBe(false);
 expect(screen.queryByRole('button', { name: '批准' })).not.toBeInTheDocument();
 fireEvent.click(screen.getByRole('button', { name: '打开成果' })); await act(async () => {});
 expect(screen.getByRole('heading', { name: '实际成果' })).toBeInTheDocument();
 expect(screen.getByText('成果正文')).toBeInTheDocument();
 expect(fetch.mock.calls.find(([url]) => String(url).includes('/documents/'))[0]).toContain('/documents/doc-1?project_id=alpha');
});

it('ignores a late do document after the project changes', async () => {
 threads = [{ id: 'thread-1', title: '任务' }];
 current = [{ ...turn(), intent: 'do', receipt: { do: { title: '成果', state: 'done', document_id: 'doc-1', progress: { done: 3, total: 3 } } } }];
 let finish;
 fetch.mockImplementation(async url => String(url).includes('/documents/') ? new Promise(resolve => { finish = resolve; }) : ({ ok: true, json: async () => String(url).includes('/threads/') ? { turns: current } : { items: threads } }));
 const view = render(<Workbench {...props}/>); await act(async () => {});
 fireEvent.click(screen.getByRole('button', { name: '打开成果' })); await act(async () => {});
 current = []; threads = []; view.rerender(<Workbench {...props} projectId="beta"/>); await act(async () => {});
 await act(async () => finish({ ok: true, json: async () => ({ markdown: '# 旧项目成果' }) }));
 expect(screen.queryByText('旧项目成果')).not.toBeInTheDocument();
 expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
});
it('reports a document read failure without inventing output', async () => {
 threads = [{ id: 'thread-1', title: '任务' }];
 current = [{ ...turn(), intent: 'do', receipt: { do: { title: '成果', state: 'done', document_id: 'doc-1', progress: { done: 3, total: 3 } } } }];
 fetch.mockImplementation(async url => String(url).includes('/documents/') ? ({ ok: false, status: 404, json: async () => ({ detail: 'missing' }) }) : ({ ok: true, json: async () => String(url).includes('/threads/') ? { turns: current } : { items: threads } }));
 render(<Workbench {...props}/>); await act(async () => {});
 fireEvent.click(screen.getByRole('button', { name: '打开成果' })); await act(async () => {});
 expect(screen.getByRole('alert')).toHaveTextContent('成果未打开');
});
it('renders organization progress and expert states without visible role text', async () => {
 threads = [{ id: 'thread-1', title: '研究任务' }];
 current = [{ id: 'do-agents', intent: 'do', receipt: { do: { title: '研究任务', state: 'researching', task_id: null, research_turn_id: 'research-do', progress: { done: 0, total: 4 }, experts: [{ role: '研究员', state: 'running' }, { role: '审阅员', state: 'done' }, { role: '分析员', state: 'failed' }] } } }];
 render(<Workbench {...props}/>); await act(async () => {});
 const progress = screen.getByRole('img', { name: '研究 · 准备 · 批准 · 成果' });
 expect(progress.children).toHaveLength(4);
 for (const role of ['研究员', '审阅员', '分析员']) {
  expect(screen.getByRole('img', { name: role })).toHaveAttribute('title', role);
  expect(screen.queryByText(role)).not.toBeInTheDocument();
 }
 expect(screen.getByRole('img', { name: '研究员' }).querySelector('[data-state]')).toHaveAttribute('data-state', 'processing');
 expect(screen.getByRole('img', { name: '审阅员' }).querySelector('[data-state]')).toHaveAttribute('data-state', 'done');
 expect(screen.getByRole('img', { name: '分析员' }).querySelector('[data-state]')).toHaveAttribute('data-state', 'failed');
});

it('rereads a collection thread and displays every independent video receipt', async () => {
 current = [1,2,3].map(n => ({...turn(), id: `video-${n}`, receipt: {remember: {...turn().receipt.remember, title: `视频${n}`, insights: []}}}));
 render(<Workbench {...props}/>); await act(async () => {});
 fireEvent.change(screen.getByRole('textbox', {name: '输入'}), {target: {value: 'https://space.bilibili.com/123/favlist?fid=456'}});
 fireEvent.click(screen.getByRole('button', {name: '发送'})); await act(async () => {});
 for (const n of [1,2,3]) expect(screen.getByText(`视频${n}`)).toBeInTheDocument();
 expect(fetch.mock.calls.some(([url]) => String(url).includes('/threads/thread-1'))).toBe(true);
});

it('shows the do receipt context percentage from the actual frozen parts', async () => {
 threads=[{id:'thread-1',title:'干活'}]; current=[{id:'turn-1',thread_id:'thread-1',intent:'do',
 receipt:{do:{title:'成果',state:'done',progress:{done:3,total:3},
 context:{window:1000,parts:[{key:'instruction',tokens:50},{key:'question',tokens:75}]}}}}];
 render(<Workbench {...props} threadId="thread-1"/>);await act(async()=>{});
 expect(screen.getByRole('button',{name:'本次上下文'})).toHaveTextContent('◯ 12.5%');
});
