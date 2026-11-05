import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { act, cleanup, renderHook } from '@testing-library/react';
import { ExtractionManifest, ExtractionProgress } from '../../src/frontend/src/features/shared-projects/SharedExtraction';
import { completeExtractionSelection, extractionCommandResult, extractionCopy, extractionPreview, extractionProgress, extractionTargets } from '../../src/frontend/src/features/shared-projects/sharedExtractionViews';
import { confirmExtractionManifest, createExtractionCommand, extractionCommands, extractionLink, linkExtractionPreview, markExtractionCommand } from '../../src/frontend/src/features/shared-projects/sharedExtractionJournal';
import { useRequestScope } from '../../src/frontend/src/shared/lib/useRequestScope';
import { deviceIdentity, forgetDeviceCredential, isDeviceIdentity, saveDeviceCredential, selectUserSpace, userStorageKey } from '../../src/frontend/src/shared/api/deviceTransport';

const preview = () => ({ id: 'extract-one', revision: 1, state: 'preview', shared_project_id: 'shared-one', target_project_id: 'project-one',
  recognition_ids: ['recognition-a', 'recognition-b'], items: [
    { id: 'recognition-a', revision: 2, text: '下雨时保留室内路线', conditions: ['当天有雨'], state: 'pending', recall_state: 'normal' },
    { id: 'recognition-b', revision: 1, text: '为同行人留出休息时间', conditions: [], state: 'pending', recall_state: 'normal' },
  ], materials: [{ id: 'material-one', title: '路线记录', text: '合成资料全文', private: true, file_names: ['路线.txt'] }] });
const progress = () => ({ id: 'extract-one', state: 'pending', personal_state: 'pending', activity_state: 'pending', completed: 1, remaining: 1, total: 2,
  copies: [{ id: 'copy-one', target_project_id: 'project-one', target_recognition_id: 'recognition-copy' }] });
const retained = () => ({ id: 'copy-one', revision: 1, state: 'retained', target_project_id: 'project-one', target_recognition_id: 'recognition-copy',
  materials: [{ item_id: 'item-one', document_id: null, title: '路线记录', text: '合成资料全文', private: true,
    files: [{ name: '路线.txt', size: 8, url: '/api/workspace/v1/items/item-one/original?project_id=project-one' }] }] });

describe('共享提取正式读模型', () => {
  it('核对原预览的来源、目标、全部认识及真实修订', () => {
    const value = preview();
    expect(extractionPreview(value, { id: value.id, source: 'shared-one', target: 'project-one', ids: ['recognition-b', 'recognition-a'] })).toBe(value);
    for (const expected of [{ id: 'extract-other' }, { source: 'shared-other' }, { target: 'project-other' }, { ids: ['recognition-a'] }])
      expect(() => extractionPreview(value, expected)).toThrow();
    for (const invalid of [{ ...value, revision: 0 }, { ...value, recognition_ids: [] }, { ...value, items: [...value.items].reverse() },
      { ...value, items: [null] }, { ...value, materials: [null] }, { ...value, materials: [{ ...value.materials[0], private: undefined }] }])
      expect(() => extractionPreview(invalid)).toThrow();
  });
  it('完整清单确认与单条、多条选择保持不同语义', () => {
    expect(completeExtractionSelection(preview(), ['recognition-b', 'recognition-a'])).toBe(true);
    for (const selected of [[], ['recognition-a'], ['recognition-a', 'recognition-a'], ['recognition-a', 'other']])
      expect(completeExtractionSelection(preview(), selected)).toBe(false);
  });
  it('明确未受理仅由同命令的正式unreceived结果决定', () => {
    expect(extractionCommandResult({ command_id: 'command-one', state: 'unreceived' }, 'command-one').state).toBe('unreceived');
    expect(extractionCommandResult({ command_id: 'command-one', state: 'received', preview_id: 'extract-one' }, 'command-one').preview_id).toBe('extract-one');
    for (const value of [{ command_id: 'other', state: 'unreceived' }, { command_id: 'command-one', state: 'busy' },
      { command_id: 'command-one', state: 'received' }, { detail: 'shared_extraction_preview_in_progress' }])
      expect(() => extractionCommandResult(value, 'command-one')).toThrow();
  });
  it('个人副本完成与共享操作记录同步分别读取', () => {
    const value = { ...progress(), completed: 2, remaining: 0, personal_state: 'completed', copies: [...progress().copies,
      { id: 'copy-two', target_project_id: 'project-one', target_recognition_id: 'recognition-copy-two' }] };
    expect(extractionProgress(value, 'extract-one').activity_state).toBe('pending');
    expect(extractionProgress({ ...value, state: 'completed', activity_state: 'completed' }, 'extract-one').state).toBe('completed');
    expect(() => extractionProgress({ ...value, state: 'completed' }, 'extract-one')).toThrow();
    expect(() => extractionProgress(value, 'extract-one', 'project-other')).toThrow();
    for (const invalid of [{ ...value, remaining: 1 }, { ...value, copies: [] }, { ...value, copies: [null, null] }])
      expect(() => extractionProgress(invalid, 'extract-one')).toThrow();
  });
  it('本人副本按原目标身份和实际附件路径读取', () => {
    const value = retained(), expected = progress().copies[0];
    expect(extractionCopy(value, expected)).toBe(value);
    expect(() => extractionCopy({ ...value, target_project_id: 'project-other' }, expected)).toThrow();
    for (const url of ['https://example.com/file', '/api/workspace/v1/items/other/original?project_id=project-one',
      '/api/workspace/v1/items/item-one/original?project_id=other', '/api/workspace/v1/items/item-one/original?project_id=project-one&project_id=other']) {
      const file = { ...value.materials[0].files[0], url };
      expect(() => extractionCopy({ ...value, materials: [{ ...value.materials[0], files: [file] }] }, expected)).toThrow();
    }
  });
  it('个人目标排除画像、收件箱及共享项目且保留同名真实身份', () => {
    const items = [{ id: 'me', name: '我' }, { id: 'inbox', name: '收件箱' }, { id: 'shared-one', name: '旅行', shared: true },
      { id: 'shared-two', name: '旅行', scope_kind: 'shared' }, { id: 'project-one', name: '旅行' }, { id: 'project-two', name: '旅行' }];
    expect(extractionTargets({ items }).map(item => item.id)).toEqual(['project-one', 'project-two']);
  });
});

describe('原操作日志与本地持久化', () => {
  const key = 'g09-extraction-test-owner';
  beforeEach(() => localStorage.clear());
  it('发送前保存原命令，未知结果保持同编号、来源、目标及选择', () => {
    const command = createExtractionCommand(key, 'shared-one', 'project-one', ['recognition-b', 'recognition-a']);
    markExtractionCommand(command, 'sending'); markExtractionCommand(command, 'unknown');
    const [restored] = extractionCommands(key);
    expect(restored.id).toBe(command.id);
    expect(restored.body).toEqual({ target_project_id: 'project-one', recognition_ids: ['recognition-a', 'recognition-b'] });
    expect(restored.expected).toEqual({ action: 'extraction', source: 'shared-one', target: 'project-one' });
    expect(restored.phase).toBe('unknown'); expect(extractionLink(restored)).toBeNull();
    expect(extractionCommands('g09-extraction-other-owner')).toEqual([]);
  });
  it('全部预览保留null选择，明确未受理保持原命令可恢复', () => {
    const command = createExtractionCommand(key, 'shared-one', 'project-one', null);
    markExtractionCommand(command, 'not_received');
    expect(extractionCommands(key)[0].body.recognition_ids).toBeNull();
    expect(extractionCommands(key)[0].id).toBe(command.id);
  });
  it('原预览绑定不可替换，完整确认正文冻结供批次继续使用', () => {
    const command = createExtractionCommand(key, 'shared-one', 'project-one', null);
    linkExtractionPreview(command, 'extract-one');
    const linked = confirmExtractionManifest(command, preview());
    expect(linked.adoption).toEqual({ expected_revision: 1, confirmed_ids: ['recognition-a', 'recognition-b'], batch_size: 8 });
    expect(() => linkExtractionPreview(command, 'extract-other')).toThrow();
    expect(() => confirmExtractionManifest(command, { ...preview(), revision: 2 })).toThrow();
    expect(extractionLink(extractionCommands(key)[0])).toEqual(linked);
    expect(() => confirmExtractionManifest(command, { ...preview(), target_project_id: 'other' })).toThrow();
  });
  it('完成后的原命令继续保留本人副本入口且多条记录互相独立', () => {
    const first = createExtractionCommand(key, 'shared-one', 'project-one', null);
    const second = createExtractionCommand(key, 'shared-two', 'project-one', ['recognition-c']);
    linkExtractionPreview(first, 'extract-one'); markExtractionCommand(first, 'pending');
    expect(extractionCommands(key).map(row => row.id)).toContain(second.id);
    expect(extractionLink(extractionCommands(key).find(row => row.id === first.id)).preview_id).toBe('extract-one');
  });
});

describe('实际共享组件服务端渲染', () => {
  it('完整预览显示条件、资料正文、私密和附件清单', () => {
    const html = renderToStaticMarkup(<ExtractionManifest preview={extractionPreview(preview())} selected={preview().recognition_ids} onSelect={() => {}}/>);
    for (const text of ['提取完整清单', '下雨时保留室内路线', '当天有雨', '合成资料全文', '私密', '路线.txt']) expect(html).toContain(text);
    expect((html.match(/type="checkbox"/g) || [])).toHaveLength(2);
  });
  it('个人完成时显示真实待同步记录并保留本人副本按钮', () => {
    const value = { ...progress(), personal_state: 'completed', completed: 2, remaining: 0,
      copies: [...progress().copies, { id: 'copy-two', target_project_id: 'project-one', target_recognition_id: 'recognition-two' }] };
    const html = renderToStaticMarkup(<ExtractionProgress value={extractionProgress(value, value.id)} onCopy={() => {}}/>);
    expect(html).toContain('个人副本已保存'); expect(html).toContain('操作记录待同步'); expect(html).toContain('2/2');
    expect((html.match(/class="ui-row-open" aria-label="本人副本/g) || [])).toHaveLength(2);
  });
});

describe('原身份与请求资格机制', () => {
  afterEach(() => { cleanup(); forgetDeviceCredential(); localStorage.clear(); });
  it('目标空间改变时原身份立即失效，原命令仍只属于原空间', () => {
    saveDeviceCredential({ key: 'a'.repeat(43), device: { device_id: 'device-synthetic', user_id: 'user-synthetic' } });
    const actor = deviceIdentity(), key = userStorageKey('shared-extractions-v1');
    const command = createExtractionCommand(key, 'shared-one', 'project-one', null);
    const { result, rerender } = renderHook(({ ownerKey }) => useRequestScope(ownerKey), { initialProps: { ownerKey: key } });
    const gate = result.current, token = gate.issue('operation', gate.scope);
    expect(gate.isCurrent(token)).toBe(true); expect(isDeviceIdentity(actor)).toBe(true);
    act(() => selectUserSpace({ user_id: 'user-other', username: '合成空间' }));
    expect(isDeviceIdentity(actor)).toBe(false);
    const nextKey = userStorageKey('shared-extractions-v1');
    expect(nextKey).not.toBe(key); expect(extractionCommands(nextKey)).toEqual([]);
    rerender({ ownerKey: nextKey });
    expect(gate.isCurrent(token)).toBe(false);
    expect(extractionCommands(key)[0].id).toBe(command.id);
  });
  it('真实失效、卸载及设备撤销均取消旧请求更新资格', () => {
    saveDeviceCredential({ key: 'b'.repeat(43), device: { device_id: 'device-synthetic', user_id: 'user-synthetic' } });
    const actor = deviceIdentity();
    const { result, unmount } = renderHook(() => useRequestScope('shared-extractions-owner'));
    const gate = result.current, first = gate.issue('operation', gate.scope);
    act(() => gate.invalidate('operation'));
    expect(gate.isCurrent(first)).toBe(false);
    const second = gate.issue('operation', gate.scope);
    expect(gate.isCurrent(second)).toBe(true);
    act(() => forgetDeviceCredential(actor));
    expect(isDeviceIdentity(actor)).toBe(false);
    unmount(); expect(gate.isCurrent(second)).toBe(false);
  });
});
