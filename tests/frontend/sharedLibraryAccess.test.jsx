import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { sharedLibraryScope, sharedLibraryList, sharedLibraryDetail, sharedLibraryApi } from '../../src/frontend/src/features/library/sharedLibraryApi';
import { Library } from '../../src/frontend/src/features/library/Library';

const authority = { account_revision: 1, member_revision: 2, project_revision: 3 };
const candidate = { id: 'candidate-one', project_id: 'same-project', namespace: 'actual-source-namespace',
  kind: 'candidate', state: 'pending', revision: 1, text: '待确认内容', conditions: [], recall_preference_revision: 0 };
const detail = { insight: candidate, insight_project_id: 'same-project', namespace: candidate.namespace,
  can_write: true, can_edit_insight: true, authority };

describe('共享资料读取资格', () => {
  it('只消费服务返回的namespace，保留同编号项目的实际物理来源', () => {
    expect(sharedLibraryScope(detail, candidate.namespace)).toBe(detail);
    expect(() => sharedLibraryScope(detail, 'another-actual-namespace')).toThrow();
    expect(() => sharedLibraryScope({ ...detail, namespace: undefined })).toThrow();
    const list = { items: [candidate], namespace: candidate.namespace, can_write: true, authority };
    expect(sharedLibraryList(list, 'insight', 'same-project')).toBe(list);
    expect(() => sharedLibraryList({ ...list, items: [{ ...candidate, namespace: 'personal-namespace' }] }, 'insight', 'same-project')).toThrow();
    expect(() => sharedLibraryList(list, 'insight', 'another-project')).toThrow();
    expect(sharedLibraryDetail(detail, 'same-project')).toBe(detail);
    expect(() => sharedLibraryDetail(detail, 'another-project')).toThrow();
  });
  it('提交前要求当前详情的人审能力和精确修订，不发出不合格请求', () => {
    expect(() => sharedLibraryApi.review('same-project', candidate, 'confirm', { ...detail, can_write: false })).toThrow();
    expect(() => sharedLibraryApi.review('same-project', { ...candidate, revision: 2 }, 'confirm', detail)).toThrow();
    expect(() => sharedLibraryApi.review('same-project', candidate, 'edit', detail)).toThrow();
    expect(() => sharedLibraryApi.review('same-project', candidate, 'drop', { ...detail, authority: {} })).toThrow();
  });
  it('失效共享范围保持在共享资料入口，不挂个人资料工具', () => {
    const html = renderToStaticMarkup(<Library projectId="same-project" projectScopeKind="shared"
      projectScope={{ id: 'same-project', scope_kind: 'shared', membership: 'unavailable', can_read_sources: false, role: null }}/>);
    expect(html).toContain('当前无法读取这个共享项目');
    expect(html).toContain('资料层级');
    expect(html).not.toContain('收件箱');
    expect(html).not.toContain('提炼灵感');
    expect(html).not.toContain('资料库更多');
    expect(html).not.toContain('现在整理');
  });
});
