import { describe, expect, it } from 'vitest';
import { readOutcomeChanges, readOutcomeUpdate } from '../../src/frontend/src/features/workbench/outcomeSources';

const project = 'project-outcome', document = 'document-previous';
const marker = { kind: 'outcome_update', id: 'outcome-update-prepared', revision: 1, project_id: project };
const context = kind => ({ kind, id: `${kind}-current`, revision: 2, project_id: project });
const prepared = contextIds => ({ project_id: project, scene: null, continue_from: document,
  text: '按当前资料更新合成成果', intent: 'do', context_ids: [...contextIds, { ...marker }] });
const changes = changed => ({ stale: changed.length > 0, changed, fresh: [], fresh_count: 0,
  verified: true, fresh_verified: true, unverifiable: [], ack_revision: 0, observation: { revisions: [] } });

describe('实际成果更新DTO类型', () => {
  // 变化引用取source_type，补充资料固定document，替代认识支路使用insight。
  it.each(['recognition', 'experience', 'document', 'original_item', 'original_source'])('接受变化引用的原始类型 %s 且原样保留DTO', kind => {
    const dto = prepared([context(kind)]), before = JSON.parse(JSON.stringify(dto));
    expect(readOutcomeUpdate(dto, project, document)).toBe(dto);
    expect(dto).toEqual(before);
  });
  it('接受新资料支路的document引用', () => {
    const dto = prepared([{ kind: 'document', id: 'document-fresh', revision: 1, project_id: project }]);
    expect(readOutcomeUpdate(dto, project, document)).toBe(dto);
  });
  it('接受替代认识支路的insight引用', () => {
    const dto = prepared([{ kind: 'insight', id: 'recognition-replacement', revision: 3, project_id: project }]);
    expect(readOutcomeUpdate(dto, project, document)).toBe(dto);
  });
  it('拒绝未登记的引用类型', () => {
    expect(() => readOutcomeUpdate(prepared([context('attachment')]), project, document)).toThrow('成果更新记录无效');
  });
  it.each(['summary', 'note', 'source'])('展示类型 %s 不充当原始更新引用类型', kind => {
    expect(() => readOutcomeUpdate(prepared([context(kind)]), project, document)).toThrow('成果更新记录无效');
  });
  it('保留唯一真实更新marker的数量约束', () => {
    for (const contextIds of [[context('document')], [context('document'), marker, marker]])
      expect(() => readOutcomeUpdate({ ...prepared([]), context_ids: contextIds }, project, document)).toThrow('成果更新记录无效');
  });
  it('保留marker项目、修订1和非空身份约束', () => {
    for (const value of [{ ...marker, project_id: 'other-project' }, { ...marker, revision: 2 }, { ...marker, id: '' }])
      expect(() => readOutcomeUpdate({ ...prepared([]), context_ids: [value] }, project, document)).toThrow('成果更新记录无效');
  });
  it('保留成果项目与旧稿的一致性', () => {
    const dto = prepared([context('document')]);
    expect(() => readOutcomeUpdate(dto, 'other-project', document)).toThrow('成果更新记录无效');
    expect(() => readOutcomeUpdate(dto, project, 'other-document')).toThrow('成果更新记录无效');
  });
  it('引用必须具有真实正整数修订和非空身份', () => {
    for (const invalid of [{ revision: 0 }, { revision: -1 }, { revision: 1.5 }, { id: '' }, { project_id: '' }])
      expect(() => readOutcomeUpdate(prepared([{ ...context('document'), ...invalid }]), project, document)).toThrow('成果更新记录无效');
  });
  it('保留当前项目之外实际画像引用的所属项目', () => {
    const dto = prepared([{ ...context('recognition'), project_id: 'me' }]);
    expect(readOutcomeUpdate(dto, project, document)).toBe(dto);
    expect(dto.context_ids[0].project_id).toBe('me');
  });
  it('展示投影支持experience对应的inspiration', () => {
    const dto = changes([{ kind: 'inspiration', source_type: 'experience', id: 'experience-current', project_id: project,
      title: '合成灵感', from_revision: 1, to_revision: 2, reasons: ['edited'] }]);
    expect(readOutcomeChanges(dto)).toBe(dto);
  });
  it('展示投影继续拒绝原始引用类型及未知类型', () => {
    for (const kind of ['recognition', 'attachment']) {
      const dto = changes([{ kind, id: 'current', project_id: project, title: '合成条目', reasons: ['edited'] }]);
      expect(() => readOutcomeChanges(dto)).toThrow('成果依据状态无效');
    }
  });
});
