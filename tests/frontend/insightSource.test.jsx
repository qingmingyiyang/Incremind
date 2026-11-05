import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { insightSourceReference } from '../../src/frontend/src/shared/lib/insightSourceReference';
import { InsightSource } from '../../src/frontend/src/shared/ui/InsightSource';

const source = { via: 'steer', text: '以后请保留原话并注明日期和范围', turn: 'turn-original', project_id: 'project-original' };

describe('插话认识来源', () => {
  it('保持缺失来源和其他来源类型的原展示', () => {
    expect(insightSourceReference(undefined)).toBeNull();
    for (const via of ['document', 'consolidation', 'client', 'codex', 'claude-code']) {
      const value = { ...source, via };
      expect(insightSourceReference(value)).toBeNull();
      expect(value).toEqual({ ...source, via });
    }
  });
  it('按十二个Unicode字符显示原话且不修改来源', () => {
    const value = { ...source, text: '😀甲乙丙丁戊己庚辛壬癸子丑' };
    expect(insightSourceReference(value).text).toBe('😀甲乙丙丁戊己庚辛壬癸子');
    expect(Array.from(insightSourceReference(value).text)).toHaveLength(12);
    expect(value.text).toBe('😀甲乙丙丁戊己庚辛壬癸子丑');
  });
  it('使用来源的原轮项目而不采用候选归属', () => {
    const candidate = { project_id: 'me', source };
    expect(insightSourceReference(candidate.source).reference).toEqual({ project_id: 'project-original', turn_id: 'turn-original' });
  });
  it('来源缺少任一可信跳转字段时仅保留文字', () => {
    for (const value of [{ ...source, project_id: undefined }, { ...source, turn: undefined }, { ...source, text: undefined },
      { ...source, project_id: '../other' }, { ...source, turn: 'turn-original?other' }, { ...source, project_id: 'p'.repeat(129) }]) {
      expect(insightSourceReference(value).reference).toBeNull();
    }
  });
  it('实际共享Row渲染可信来源的跳转按钮', () => {
    const html = renderToStaticMarkup(<InsightSource source={source} onReference={() => {}}/>);
    expect(html).toContain('<button');
    expect(html).toContain('插话');
    expect(html).toContain(Array.from(source.text).slice(0, 12).join(''));
    expect(html).not.toContain(source.text);
  });
  it('没有来源项目或导航接点时实际渲染静态文字', () => {
    for (const props of [{ source }, { source: { ...source, project_id: undefined }, onReference: () => {} }]) {
      const html = renderToStaticMarkup(<InsightSource {...props}/>);
      expect(html).toContain('插话');
      expect(html).not.toContain('<button');
    }
  });
  it('共享来源组件只交付原轮引用', () => {
    const references = [];
    const element = InsightSource({ source, onReference: reference => references.push(reference) });
    element.props.onOpen();
    expect(references).toEqual([{ project_id: 'project-original', turn_id: 'turn-original' }]);
  });
  it('实际渲染兼容其他来源且不生成额外链接', () => {
    expect(renderToStaticMarkup(<InsightSource source={{ via: 'client', text: '外部来源', client: 'codex' }}/>)).toBe('');
  });
});
