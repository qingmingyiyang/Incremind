import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { sharedScopes, sharedHistoryThread, sharedHistoryDetail } from '../../src/frontend/src/features/shared-projects/sharedScopeViews';
import { projectScopeKey, projectScopeLabel } from '../../src/frontend/src/shared/lib/projectScope';
import { ProjectSwitcher } from '../../src/frontend/src/shared/ui/ProjectSwitcher';
import { SharedHistoryReceipt, SharedWorkbenchHistory } from '../../src/frontend/src/features/shared-projects/SharedWorkbenchHistory';
import { useWorkbenchDraft } from '../../src/frontend/src/shared/lib/workbenchDraft';

const shared = () => ({ id: 'shared-one', name: '旅行', scope_kind: 'shared', membership: 'active', role: 'viewer',
  can_ask: true, can_write: false, can_read_sources: true, has_personal_history: true,
  threads: [{ id: 'thread-one', title: '原问题', updated_at: '2026-11-05T00:00:00Z' }] });
afterEach(() => { cleanup(); localStorage.clear(); });

describe('正式共享范围与本人历史', () => {
  it('保留只看成员、管理他人空间的提问资格和失权后的本人线程', () => {
    const viewer = shared(), unavailable = { ...shared(), id: 'shared-old', name: 'shared-old', membership: 'unavailable', role: null,
      can_ask: false, can_write: false, can_read_sources: false };
    const owner = { ...shared(), id: 'shared-owner', role: 'owner', can_write: true, can_ask: false };
    expect(sharedScopes({ items: [viewer, unavailable, owner] })).toEqual([viewer, unavailable, owner]);
    expect(() => sharedScopes({ items: [{ ...viewer, can_write: true }] })).toThrow();
    expect(() => sharedScopes({ items: [{ ...unavailable, can_read_sources: true }] })).toThrow();
  });
  it('共享显式线程须属于正式本人历史，缺失时保留原目标并拒绝替换', () => {
    const value = shared();
    expect(sharedHistoryThread(value, 'thread-one', undefined, value.id)).toBe('thread-one');
    expect(() => sharedHistoryThread(value, 'thread-missing', undefined, value.id)).toThrow('指定对话');
    expect(() => sharedHistoryThread(value, 'thread-one', undefined, 'shared-other')).toThrow();
    const detail = { id: 'thread-one', scope_kind: 'shared', turns: [{ id: 'turn-one', scope_kind: 'shared', project_id: value.id, thread_id: 'thread-one',
      intent: 'ask', user_text: '合成问题', receipt: { ask: { answer: '合成回答', citations: [] } } }] };
    expect(sharedHistoryDetail(detail, 'thread-one', value.id)).toBe(detail);
    expect(() => sharedHistoryDetail(detail, 'thread-other', value.id)).toThrow();
    expect(() => sharedHistoryDetail(detail, 'thread-one', 'shared-other')).toThrow();
  });
  it('同编号同名项目仍保留不同逻辑来源和实际选择标记', () => {
    const personal = { id: 'shared-one', name: '旅行' }, value = shared(), projects = [personal, value];
    expect(projectScopeKey(personal)).toBe('personal:shared-one');
    expect(projectScopeKey(value)).toBe('shared:shared-one');
    expect(projectScopeLabel(personal, projects)).toBe('旅行');
    expect(projectScopeLabel(value, projects)).toBe('旅行 · 共享');
    const html = renderToStaticMarkup(<ProjectSwitcher projects={projects} value={value.id} scopeKind="shared"/>);
    expect(html).toContain('title="旅行 · 共享"');
    expect(renderToStaticMarkup(<ProjectSwitcher projects={projects} value={value.id}/>)).toContain('title="旅行"');
  });
  it('原回执只读展示正文与引用显示信息，来源下钻和处理动作不进入此展示', () => {
    const turn = { id: 'turn-one', intent: 'ask', user_text: '原问题', receipt: { ask: { answer: '原回答[1]', citations: [
      { n: 1, layer: 'insight', id: 'recognition-one', title: '原依据', quote: '原话' },
    ] } } };
    const html = renderToStaticMarkup(<SharedHistoryReceipt turn={turn}/>);
    expect(html).toContain('原问题'); expect(html).toContain('原回答'); expect(html).toContain('原依据');
    expect(html).not.toContain('<button'); expect(html).not.toContain('href=');
    const page = renderToStaticMarkup(<SharedWorkbenchHistory projectId="shared-one" projectScope={shared()} RenderTurn={SharedHistoryReceipt}/>);
    expect(page).toContain('共享 · 只看'); expect(page).toContain('当前不可发送');
    expect(page).toContain('aria-label="发送" disabled');
  });
  it('同编号个人与共享草稿分别保存，切回各自正文且保留个人原存储格式', () => {
    const view = renderHook(({ kind }) => useWorkbenchDraft('same-project', 0, kind), { initialProps: { kind: 'personal' } });
    act(() => view.result.current.saveDraft({ text: '个人草稿', selection: 'ask' }));
    const personalKey = view.result.current.draftKey;
    view.rerender({ kind: 'shared' }); expect(view.result.current.draft).toBeUndefined();
    act(() => view.result.current.saveDraft({ text: '共享草稿', selection: 'ask' }));
    expect(view.result.current.draftKey).not.toBe(personalKey);
    view.rerender({ kind: 'personal' }); expect(view.result.current.draft.text).toBe('个人草稿');
    view.rerender({ kind: 'shared' }); expect(view.result.current.draft.text).toBe('共享草稿');
    expect(JSON.parse(localStorage.getItem('chriptmas-workbench-draft:same-project')).text).toBe('个人草稿');
    expect(JSON.parse(localStorage.getItem('chriptmas-workbench-draft:shared:same-project')).text).toBe('共享草稿');
  });
});
