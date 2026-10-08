import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import SettingsPage from '@src/features/settings/SettingsPage';
import { CitedAnswer } from '@src/shared/ui/CitedAnswer';
import { ContextPanel } from '@src/features/workbench/ContextPanel';

afterEach(cleanup);

it('configures the separately off search purpose without using the rerank connection test', async () => {
  const value = { model:{generation_mode:{mode:'api', revision:1},
    generation:{revision:1}, embedding:{revision:1}, rerank:{revision:1}, asr:{settings_revision:1},
    search:{revision:0, enabled:false, allow_remote:false, base_url:'', model:'', has_api_key:false}},
    privacy:{revision:0, private_projects:[]} };
  const api = {load:vi.fn(async () => value), projects:vi.fn(async () => ({items:[]})),
    saveModel:vi.fn(async () => ({}))};
  render(<SettingsPage api={api}/>);
  await act(async () => {});
  expect(screen.getByRole('switch', {name:'搜索外发'})).toHaveAttribute('aria-checked', 'false');
  fireEvent.click(screen.getByRole('button', {name:'搜索', exact:true}));
  const form = screen.getByLabelText('搜索地址').closest('form');
  fireEvent.change(screen.getByLabelText('搜索地址'), {target:{value:'https://example.test/v1'}});
  fireEvent.change(screen.getByLabelText('搜索模型'), {target:{value:'search-native'}});
  fireEvent.change(screen.getByLabelText('搜索密钥'), {target:{value:'synthetic-private-value'}});
  fireEvent.submit(form);
  await act(async () => {});
  expect(api.saveModel).toHaveBeenCalledExactlyOnceWith({purpose:'search', baseUrl:'https://example.test/v1',
    model:'search-native', apiKey:'synthetic-private-value', allowRemote:false, enabled:false, expectedRevision:0});
  expect(screen.getByLabelText('搜索密钥')).toHaveValue('');
  expect(within(form.closest('section')).queryByRole('button', {name:'测试', exact:true})).toBeNull();
});

it('opens a saved search URL as a native link and keeps local navigation separate', () => {
  const open = vi.fn(), url = 'https://example.test/current/price';
  render(<CitedAnswer answer="最新费用120元[1]" citations={[{n:1, layer:'source', id:'real-original',
    title:'来自搜索 官网', quote:'费用120元', url}]} onOpenCitation={open} citationHref="#library-focus"/>);
  const link = screen.getByRole('link', {name:new RegExp('引用 1')});
  expect(link).toHaveAttribute('href', url);
  expect(link).toHaveAttribute('rel', 'noopener noreferrer');
  expect(link).toHaveAttribute('target', '_blank');
  expect(link.getAttribute('title')).toContain(url);
  expect(within(link).getByText('example.test')).toBeVisible();
  fireEvent.click(link);
  expect(open).not.toHaveBeenCalled();
});

it('does not turn an invalid external URL into a link', () => {
  render(<CitedAnswer answer="内容[1]" citations={[{n:1, layer:'source', id:'original',
    title:'原件', quote:'正文', url:'javascript:alert(1)'}]}/>);
  expect(screen.queryByRole('link')).toBeNull();
  expect(screen.queryByText('alert(1)')).toBeNull();
});

it('shows the complete hostname after the unchanged citation number and stale label', () => {
  const hostname = `${'long'.repeat(15)}.example.test`;
  render(<CitedAnswer answer="费用[3]和未知[9]" citations={[{n:3, layer:'source', id:'original',
    title:'来自搜索 官网', quote:'正文', stale:true, url:`https://${hostname}/price`}]} />);
  const link = screen.getByRole('link', {name:'引用 3 · 原件 · 来自搜索 官网 · 过时'});
  expect(within(link).getByText(hostname)).toBeVisible();
  expect(link).toHaveTextContent(`3过时 ${hostname}`);
  expect(screen.getByText(/和未知\[9\]/)).toBeInTheDocument();
});

it.each(['https://user:password@example.test/private', 'https://example.test:wrong/path',
  'javascript:example.test', 'https://example.test/a b'])('does not display a hostname from unsafe URL %s', url => {
  render(<CitedAnswer answer="正文[1]" citations={[{n:1, layer:'source', id:'original',
    title:'原件', quote:'正文', url}]} />);
  expect(screen.queryByRole('link')).toBeNull();
  expect(screen.queryByText('example.test')).toBeNull();
  expect(screen.getByLabelText('引用 1 · 原件 · 原件')).toHaveTextContent('1');
});

it('does not drill an uncited transient search result as a stored original', () => {
  const open = vi.fn(), url = 'https://example.test/unselected';
  render(<ContextPanel receipt={{context:{entries:[{layer:'source', id:'memory-search-0',
    title:'来自搜索 官网', tokens:20, url}]}}} onOpenCitation={open}/>);
  fireEvent.click(screen.getByText('条目'));
  const link = screen.getByRole('link', {name:'https://example.test/unselected'});
  expect(link).toHaveAttribute('href', url);
  expect(screen.queryByRole('button', {name:'来自搜索 官网'})).toBeNull();
  expect(open).not.toHaveBeenCalled();
});
