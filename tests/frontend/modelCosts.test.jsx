import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import SettingsPage from '@src/features/settings/SettingsPage';
import { ContextPanel } from '@src/features/workbench/ContextPanel';
import { settingsApi } from '@src/features/settings/settingsApi';
import { formatModelCost } from '@src/shared/lib/modelCost';

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const settle = async () => act(async () => {});
const rates = { input_per_million: '2', output_per_million: '8', cache_read_per_million: '0.04' };

it.each([
  ['0.00015', '¥0.0002'], ['0.00105', '¥0.0011'], ['0.00014', '¥0.0001'],
  ['0', '¥0.0000'], ['999999999999999999999999.99995', '¥1000000000000000000000000.0000'],
])('rounds exact decimal cost %s to four places without binary or exponential conversion', (amount, shown) => {
  expect(formatModelCost({currency: 'CNY', amount})).toBe(shown);
});

function api(pricing = { rates: null, source: null, revision: 0, editable: true }) {
  return { load: vi.fn(async () => ({ model: { generation: { model: 'writer', revision: 3, pricing },
    generation_mode: { mode: 'api', revision: 0 } }, privacy: { private_projects: [] } })),
    projects: vi.fn(async () => ({ items: [] })), savePrices: vi.fn(async () => ({})),
    saveModel: vi.fn(async () => ({})), receipts: vi.fn(async () => [
      { purpose: '问', model_cost: { currency: 'CNY', amount: '0.01234' } }, { purpose: '干活' }]) };
}

it('saves nullable unknown model prices with their own CAS and explicit CNY unit', async () => {
  const injected = api(); render(<SettingsPage api={injected}/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '生成', exact: true }));
  expect(screen.getByText('¥ / 百万 token')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('生成输入单价'), { target: { value: '2' } });
  fireEvent.change(screen.getByLabelText('生成输出单价'), { target: { value: '8' } });
  fireEvent.click(screen.getByRole('button', { name: '保存单价' })); await settle();
  expect(injected.savePrices).toHaveBeenCalledWith({ purpose: 'generation',
    rates: { ...rates, cache_read_per_million: null }, expectedRevision: 0, expectedConfigurationRevision: 3 });
  expect(injected.saveModel).not.toHaveBeenCalled();
});

it('shows documented official prices without enabling manual edits', async () => {
  const injected = api({ rates, source: 'deepseek-cny-2026-10-04', revision: 0, editable: false });
  render(<SettingsPage api={injected}/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '生成', exact: true }));
  expect(screen.getByLabelText('生成输入单价')).toHaveValue(2);
  expect(screen.getByLabelText('生成缓存命中单价')).toHaveValue(0.04);
  expect(screen.getByLabelText('生成输入单价')).toBeDisabled();
  expect(screen.queryByRole('button', { name: '保存单价' })).not.toBeInTheDocument();
});

it('refreshes readonly official rates when the peak period changes', async () => {
  const injected = api({ rates, source: 'deepseek-cny-2026-10-04', revision: 0, editable: false });
  injected.testModel = vi.fn(async () => ({status: 'complete'}));
  render(<SettingsPage api={injected}/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '生成', exact: true }));
  expect(screen.getByLabelText('生成输入单价')).toHaveValue(2);
  injected.load.mockImplementation(api({ rates: {...rates, input_per_million: '1',
    output_per_million: '4', cache_read_per_million: '0.02'}, revision: 0, editable: false }).load);
  fireEvent.click(screen.getByRole('button', { name: '测试', exact: true })); await settle();
  expect(screen.getByLabelText('生成输入单价')).toHaveValue(1);
  expect(screen.getByLabelText('生成输出单价')).toHaveValue(4);
  expect(screen.getByLabelText('生成缓存命中单价')).toHaveValue(0.02);
  expect(screen.getByLabelText('生成输入单价')).toBeDisabled();
});

it.each(['ask', 'do'])('formats the complete cost in the %s context row', kind => {
  render(<ContextPanel kind={kind} receipt={{ model_cost: { currency: 'CNY', amount: '0.01234' } }}/>);
  expect(within(screen.getByLabelText('外发')).getByText('花费').closest('.ui-row')).toHaveTextContent('¥0.0123');
});

it.each([null, {currency:'USD',amount:'1'}, {currency:'CNY',amount:null}, {currency:'CNY',amount:'NaN'},
         {currency:'CNY',amount:'0.01',observed_only:true}])('keeps unknown or partial cost as a dash', cost => {
  render(<ContextPanel receipt={{ model_cost: cost }}/>);
  expect(screen.getByText('花费').closest('.ui-row')).toHaveTextContent('—');
});

it('shows known and historical unknown expenses after usage in egress records', async () => {
  render(<SettingsPage api={api()} initialSection="privacy"/>); await settle();
  fireEvent.click(screen.getByRole('button', { name: '外发记录' })); await settle();
  const table = screen.getByRole('table');
  expect(within(table).getByRole('columnheader', { name: '花费' })).toBeInTheDocument();
  expect(within(table).getByText('¥0.0123')).toBeInTheDocument();
  expect(within(table).getAllByRole('row')[2].lastElementChild).toHaveTextContent('—');
});

it('sends independent price revisions without credentials or a model-config write', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ({ok:true,status:200,json:async()=>({})})));
  await settingsApi.savePrices({purpose:'generation',rates,expectedRevision:2,expectedConfigurationRevision:3});
  const [url, request] = fetch.mock.calls[0];
  expect(url).toBe('/api/v2/settings/model-prices'); expect(request.method).toBe('PATCH');
  expect(JSON.parse(request.body)).toEqual({purpose:'generation',rates,expected_revision:2,expected_configuration_revision:3});
});
