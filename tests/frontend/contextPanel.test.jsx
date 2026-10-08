import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ContextPanel } from '@src/features/workbench/ContextPanel';
afterEach(cleanup);
it.each(['ask', 'do'])('shows the confirmed profile count and tokens for %s', kind => {
 render(<ContextPanel kind={kind} receipt={{context:{parts:[{key:'persona',count:3,tokens:178}]},citations:[]}}/>);
 const row = within(screen.getByLabelText('分类')).getByText('我').closest('.context-part');
 expect(row).toHaveTextContent('3'); expect(row).toHaveTextContent('178');
 expect(screen.queryByRole('button', {name:/画像/})).not.toBeInTheDocument();
});
it.each(['ask', 'do'])('omits an explicitly empty profile row for %s without changing totals', kind => {
 render(<ContextPanel kind={kind} receipt={{context:{window:16000,parts:[{key:'persona',count:0,tokens:0},{key:'question',count:1,tokens:15}]}}}/>);
 expect(within(screen.getByLabelText('分类')).queryByText('我')).not.toBeInTheDocument();
 expect(screen.getByLabelText('总量')).toHaveTextContent('15 / 16000');
 expect(screen.getByLabelText('分类')).toHaveTextContent('问题');
});
it.each(['ask', 'do'])('keeps an unknown profile budget visible for %s', kind => {
 render(<ContextPanel kind={kind} receipt={{context:{parts:[{key:'persona',count:1}]}}}/>);
 const row = within(screen.getByLabelText('分类')).getByText('我').closest('.context-part');
 expect(row).toHaveTextContent('1');
 expect(row).toHaveTextContent('—');
 expect(screen.getByLabelText('总量')).toHaveTextContent('— / —');
});
it('lists bookshelf spines without counting them as model input', () => {
 const onOpenCitation = vi.fn();
 const spine = { id: 'forgotten', layer: 'insight', title: '旧暗号', date: '2026-01-01', scene: '展厅' };
 render(<ContextPanel receipt={{ context: { parts: [{ key: 'question', count: 1, tokens: 20 }], bookshelf: { hits: 1, used: 0, spines: [spine] } } }} onOpenCitation={onOpenCitation}/>);
 expect(screen.getByLabelText('总量')).toHaveTextContent('20 / —');
 fireEvent.click(screen.getByText('书架 1'));
 fireEvent.click(screen.getByRole('button', { name: '旧暗号' }));
 expect(onOpenCitation).toHaveBeenCalledWith(spine);
});
const receipt = {
 context: { window: 10000, reserve: 1000, parts: [{ key: 'insight', count: 2, tokens: 300 }, { key: 'question', count: 1, tokens: 200 }] },
 trace: [{ layer: 'insight', selected: 2, stopped: true }], layers: { insight: 2 },
 citations: [{ n: 3, id: 'a', layer: 'insight', title: '数字表达', quote: '保留依据' }],
 model_usage: { input_tokens: 510, output_tokens: 80 }, excluded_sources: [{ id: 'private' }],
 model: 'local-model', consent_basis: { scope: 'global_setting' },
};
it('renders actual ask budget, reserve, classification and remote usage', () => {
 render(<ContextPanel receipt={receipt}/>);
 expect(screen.getByRole('dialog', { name: '上下文' })).toBeInTheDocument();
 expect(screen.getByLabelText('总量')).toHaveTextContent('500 / 10000');
 expect(screen.getByLabelText('总量')).toHaveTextContent('5%');
 expect(screen.getByLabelText('分类')).toHaveTextContent('认识');
 expect(screen.getByLabelText('分类')).toHaveTextContent('300');
 expect(screen.getByLabelText('分类')).toHaveTextContent('8500');
 expect(screen.getByLabelText('外发')).toHaveTextContent('入 510 · 出 80');
 expect(screen.getByLabelText('外发')).not.toHaveTextContent('private');
});
it('reuses the ladder and marks the actual stop layer with a red point', () => {
 render(<ContextPanel receipt={receipt}/>);
 fireEvent.click(screen.getByText('阶梯'));
 const ladder = screen.getByRole('list', { name: '本次用了什么' });
 expect(within(ladder).getByLabelText('已足够')).toBeInTheDocument();
 expect(ladder.closest('.context-panel')).toBeInTheDocument();
});
it('opens a real citation using its complete locator and original number', () => {
 const onOpenCitation = vi.fn(); render(<ContextPanel receipt={receipt} onOpenCitation={onOpenCitation}/>);
 fireEvent.click(screen.getByText('条目'));
 fireEvent.click(within(screen.getByLabelText('条目')).getByRole('button', { name: '数字表达' }));
 expect(onOpenCitation).toHaveBeenCalledWith(receipt.citations[0]);
});
it('keeps missing budget and egress values unknown without invented defaults', () => {
 render(<ContextPanel receipt={{}}/>);
 expect(screen.getByLabelText('总量')).toHaveTextContent('— / —');
 expect(screen.getByLabelText('外发')).toHaveTextContent('模型—');
 expect(screen.getByLabelText('外发')).toHaveTextContent('私密排除—');
 expect(screen.queryByRole('progressbar')).not.toBeInTheDocument();
});
it('does not fabricate a ladder or budget for do receipts', () => {
 render(<ContextPanel kind="do" receipt={{ status: 'completed' }}/>);
 expect(screen.queryByText('阶梯')).not.toBeInTheDocument();
 expect(screen.getByLabelText('总量')).toHaveTextContent('— / —');
});
it('keeps citations read only when no drill callback exists and preserves zero', () => {
 render(<ContextPanel receipt={{ ...receipt, context: { window: 10000, reserve: 0, parts: [{ key: 'insight', count: 0, tokens: 0 }] }, excluded_sources: [] }}/>);
 expect(screen.getByLabelText('总量')).toHaveTextContent('0 / 10000');
 fireEvent.click(screen.getByText('条目'));
 expect(within(screen.getByLabelText('条目')).queryByRole('button')).not.toBeInTheDocument();
 expect(screen.getByLabelText('外发')).toHaveTextContent('私密排除—');
});
it('does not label evidence budget exclusions as private sources', () => {
 render(<ContextPanel receipt={{ ...receipt, excluded_sources: [{ reason: 'recognition_evidence_budget_insufficient', id: 'a' }] }}/>);
 expect(screen.getByLabelText('外发')).toHaveTextContent('私密排除—');
});

it('renders task expert context without ask-only entries or a ladder', () => {
 render(<ContextPanel kind="do" receipt={{ context: { window: 16000, parts: [{ key: 'expert_brief', count: 1, tokens: 100 }, { key: 'question', count: 1, tokens: 20 }] }, model_usage: { input_tokens: 250, output_tokens: 80 } }}/>);
 expect(screen.getByLabelText('总量')).toHaveTextContent('120 / 16000');
 expect(screen.getByLabelText('分类')).toHaveTextContent('专家结论');
 expect(screen.queryByText('阶梯')).not.toBeInTheDocument();
 expect(screen.queryByText('条目')).not.toBeInTheDocument();
 expect(screen.queryByText('预留回答')).not.toBeInTheDocument();
 expect(screen.getByLabelText('外发')).toHaveTextContent('入 250 · 出 80');
});

it('lists uncited sent entries and displays copied egress metadata', () => {
 const entry = {id: 'uncited', layer: 'source', title: '未引用原件', tokens: 45, persona: false};
 const onOpen = vi.fn();
 render(<ContextPanel receipt={{...receipt, context: {...receipt.context, entries: [entry], egress: {model: 'actual-model', consent_scope: 'global_setting', settings_revision: {generation: 4, mode: 2}, excluded_private: 3}}}} onOpenCitation={onOpen}/>);
 fireEvent.click(screen.getByText('条目')); fireEvent.click(screen.getByRole('button', {name: '未引用原件'}));
 expect(onOpen).toHaveBeenCalledWith(entry);
 expect(within(screen.getByLabelText('条目')).queryByText('数字表达')).not.toBeInTheDocument();
 expect(screen.getByLabelText('外发')).toHaveTextContent('actual-model');
 expect(screen.getByLabelText('外发')).toHaveTextContent('全局');
 expect(screen.getByLabelText('外发')).toHaveTextContent('私密排除3');
});


it('counts recent dialogue separately and never offers it as a citation', () => {
 render(<ContextPanel receipt={{ context: { window: 6000, reserve: 1000, parts: [{ key: 'history', count: 2, tokens: 200 }] }, citations: [] }}/>);
 const row = screen.getByText('对话').closest('.context-part');
 expect(row).toHaveTextContent('2'); expect(row).toHaveTextContent('200');
 expect(screen.getByLabelText('总量')).toHaveTextContent('200 / 6000');
 expect(screen.getByLabelText('条目').querySelectorAll('button')).toHaveLength(0);
});
