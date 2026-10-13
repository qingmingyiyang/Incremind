import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { AsrLexicon } from '@src/features/settings/AsrLexicon';
import { forgetDeviceCredential, saveDeviceCredential, selectUserSpace } from '@src/shared/api/deviceTransport';

const settle = async () => act(async () => {});
const lexicon = { revision: 7, terms: [{ term_id: 'a', term: '示例公司', weight: 4, status: 'active' }] };
let api;
beforeEach(() => {
  forgetDeviceCredential();
  api = { loadAsrLexicon: vi.fn(async () => lexicon), importAsrLexicon: vi.fn(async body => body.preview
    ? { added_count: 1, updated_count: 0, unchanged_count: 0, terms: [{ term: '示例术语', weight: 4 }] }
    : { revision: 8, terms: [...lexicon.terms, { term_id: 'b', term: '示例术语', weight: 4 }] }) };
});
afterEach(() => { cleanup(); forgetDeviceCredential(); vi.unstubAllGlobals(); });
async function open() { render(<AsrLexicon projectId="inbox" api={api}/>); await settle(); }
function fill(text = '示例术语') { fireEvent.change(screen.getByLabelText('批量热词'), { target: { value: text } }); }
it('shows existing terms and requires an explicit preview before committing the original revision', async () => {
  await open(); expect(screen.getByLabelText('当前热词')).toHaveTextContent('示例公司');
  fill(); expect(screen.queryByRole('button', { name: '导入' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '预览' })); await settle();
  expect(api.importAsrLexicon.mock.calls[0][0]).toEqual({ expected_revision: 7, text: '示例术语', format: 'txt', preview: true });
  api.loadAsrLexicon.mockResolvedValue({ revision: 8, terms: [...lexicon.terms, { term: '示例术语', weight: 4 }] });
  fireEvent.click(screen.getByRole('button', { name: '导入' })); await settle();
  expect(api.importAsrLexicon.mock.calls[1][0].preview).toBe(false);
  expect(screen.getByLabelText('当前热词')).toHaveTextContent('示例术语');
  expect(screen.getByLabelText('批量热词')).toHaveValue('');
});
it('invalidates a preview when the text or selected format changes', async () => {
  await open(); fill(); fireEvent.click(screen.getByRole('button', { name: '预览' })); await settle();
  fill('另一个词'); expect(screen.queryByRole('button', { name: '导入' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '预览' })); await settle();
  fireEvent.change(screen.getByLabelText('热词格式'), { target: { value: 'json' } });
  expect(screen.queryByRole('button', { name: '导入' })).not.toBeInTheDocument();
});
it('rejects oversized text without sending it to the backend', async () => {
  await open(); fill('测'.repeat(90000)); fireEvent.click(screen.getByRole('button', { name: '预览' })); await settle();
  expect(screen.getByRole('alert')).toHaveTextContent('内容超过'); expect(api.importAsrLexicon).not.toHaveBeenCalled();
});
it('rejects unsupported and oversized files before reading or sending content', async () => {
  await open(); const input = screen.getByLabelText('导入热词文件');
  fireEvent.change(input, { target: { files: [new File(['x'], 'terms.exe')] } });
  expect(screen.getByRole('alert')).toHaveTextContent('文件格式不支持');
  fireEvent.change(input, { target: { files: [new File(['x'.repeat(262145)], 'terms.txt')] } });
  expect(screen.getByRole('alert')).toHaveTextContent('文件超过'); expect(api.importAsrLexicon).not.toHaveBeenCalled();
});
it('reads a UTF-8 JSON file locally without submitting until preview is clicked', async () => {
  await open(); fireEvent.change(screen.getByLabelText('导入热词文件'), { target: { files: [new File(['[{"term":"示例术语"}]'], 'terms.json')] } });
  await screen.findByDisplayValue('[{"term":"示例术语"}]');
  expect(screen.getByLabelText('热词格式')).toHaveValue('json'); expect(api.importAsrLexicon).not.toHaveBeenCalled();
});
it('presents malformed imports and conflicts without committing a partial result', async () => {
  await open(); fill(); api.importAsrLexicon.mockRejectedValue({ status: 400 });
  fireEvent.click(screen.getByRole('button', { name: '预览' })); await settle();
  expect(screen.getByRole('alert')).toHaveTextContent('词表格式有误');
  api.importAsrLexicon.mockRejectedValue({ status: 409 });
  fireEvent.click(screen.getByRole('button', { name: '预览' })); await settle();
  expect(screen.getByRole('alert')).toHaveTextContent('词表已变化');
  expect(screen.queryByRole('button', { name: '导入' })).not.toBeInTheDocument();
});
it('discards the previous user draft and a late preview after a user-space switch', async () => {
  saveDeviceCredential({ key: 'a'.repeat(43), device: { device_id: 'test-device', user_id: 'admin' } });
  selectUserSpace({ user_id: 'old-user' }); let release;
  api.importAsrLexicon.mockImplementation(() => new Promise(resolve => { release = resolve; }));
  await open(); fill(); fireEvent.click(screen.getByRole('button', { name: '预览' })); await settle();
  api.loadAsrLexicon.mockResolvedValue({ revision: 9, terms: [] });
  await act(async () => { selectUserSpace({ user_id: 'new-user' }); }); await settle();
  await act(async () => { release({ terms: [{ term: '旧用户私密词' }] }); });
  expect(screen.getByLabelText('批量热词')).toHaveValue('');
  expect(screen.queryByRole('button', { name: '导入' })).not.toBeInTheDocument();
  expect(screen.queryByText('旧用户私密词')).not.toBeInTheDocument();
  expect(api.importAsrLexicon.mock.calls[0][1].aborted).toBe(true);
});
it('rejects invalid UTF-8 file data without a network request', async () => {
  await open();
  fireEvent.change(screen.getByLabelText('导入热词文件'), { target: { files: [new File([new Uint8Array([255])], 'terms.txt')] } });
  expect(await screen.findByRole('alert')).toHaveTextContent('使用 UTF-8');
  expect(api.importAsrLexicon).not.toHaveBeenCalled();
});
it('does not populate the new user draft from an old asynchronous file read', async () => {
  saveDeviceCredential({ key: 'a'.repeat(43), device: { device_id: 'test-device', user_id: 'admin' } });
  selectUserSpace({ user_id: 'old-user' }); let reader;
  // 仅控制浏览器文件读取的完成时间，组件和作用域检查保持真实。
  vi.stubGlobal('FileReader', class {
    constructor() { reader = this; }
    readAsArrayBuffer() {}
  });
  await open();
  fireEvent.change(screen.getByLabelText('导入热词文件'), { target: { files: [new File(['旧用户私密词'], 'terms.txt')] } });
  await act(async () => { selectUserSpace({ user_id: 'new-user' }); }); await settle();
  await act(async () => { reader.result = new TextEncoder().encode('旧用户私密词').buffer; reader.onload(); });
  expect(screen.getByLabelText('批量热词')).toHaveValue('');
  expect(api.importAsrLexicon).not.toHaveBeenCalled();
});

