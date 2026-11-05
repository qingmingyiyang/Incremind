import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import postcss from '../../src/frontend/node_modules/postcss/lib/postcss.mjs';

const fontRoot = resolve('public/fonts');
const facePath = resolve('src/font-faces.css');
const readFaces = () => {
  expect(existsSync(facePath)).toBe(true);
  const faces = [];
  postcss.parse(readFileSync(facePath, 'utf8')).walkAtRules('font-face', rule => {
    faces.push(Object.fromEntries(rule.nodes.map(node => [node.prop, node.value])));
  });
  return faces;
};

describe('bundled font assets', () => {
  it('imports generated local faces before all other style rules', () => {
    const sheet = postcss.parse(readFileSync(resolve('src/styles.css'), 'utf8'));
    expect(sheet.nodes.find(node => node.type !== 'comment').toString()).toBe('@import "./font-faces.css"');
  });

  it('exposes only the design weights and one Fira Code variable file', () => {
    const faces = readFaces();
    expect([...new Set(faces.map(face => `${face['font-family']}:${face['font-weight']}`))].sort()).toEqual([
      '"Fira Code":400 500', '"Noto Sans SC":400', '"Noto Sans SC":500', '"Noto Serif SC":700',
    ]);
    expect(faces.filter(face => face['font-family'] === '"Fira Code"')).toHaveLength(1);
  });

  it('uses WOFF2 with disjoint common and on-demand ranges', () => {
    for (const face of readFaces()) {
      expect(face.src).toMatch(/^url\("\/fonts\/[a-z0-9-]+\.woff2"\) format\("woff2"\)$/);
      expect(face['font-display']).toBe('swap');
      expect(face['unicode-range']).toMatch(/^U\+[\dA-F]+/);
      expect(existsSync(resolve(fontRoot, /\/fonts\/([^"/]+)/.exec(face.src)[1]))).toBe(true);
    }
    const faces = readFaces();
    for (const [family, weight] of [['Noto Sans SC', '400'], ['Noto Sans SC', '500'], ['Noto Serif SC', '700']]) {
      const selected = faces.filter(face => face['font-family'] === `"${family}"` && face['font-weight'] === weight);
      expect(selected.filter(face => face.src.includes('-common.woff2'))).toHaveLength(1);
      expect(selected.some(face => face.src.includes('-rare-'))).toBe(true);
      expect(selected.some(face => face['unicode-range'] === 'U+20BB7')).toBe(true);
    }
  });

  it('ships every generated asset exactly once and no retired TTF or license', () => {
    const manifest = JSON.parse(readFileSync(resolve(fontRoot, 'manifest.json'), 'utf8'));
    const files = readdirSync(fontRoot);
    expect(files.filter(file => /\.(ttf|otf)$/i.test(file))).toEqual([]);
    expect(files.filter(file => /fraunces/i.test(file))).toEqual([]);
    expect(files.filter(file => file.endsWith('.woff2')).sort()).toEqual(manifest.files.map(file => file.file).sort());
    expect(new Set(manifest.files.map(file => file.file)).size).toBe(manifest.files.length);
    for (const asset of manifest.files) expect(statSync(resolve(fontRoot, asset.file)).size).toBe(asset.bytes);
    for (const license of ['OFL-Fira-Code.txt', 'OFL-Noto-Sans-SC.txt', 'OFL-Noto-Serif-SC.txt', 'OFL-Noto-CJK.txt']) {
      expect(readFileSync(resolve(fontRoot, license), 'utf8')).toContain('SIL OPEN FONT LICENSE');
    }
  });

  it('records original coverage and explicit supplemental glyphs', () => {
    const manifest = JSON.parse(readFileSync(resolve(fontRoot, 'manifest.json'), 'utf8'));
    expect(manifest.fonts.map(font => [font.family, font.weight, font.original_codepoints])).toEqual([
      ['Noto Sans SC', '400', 30890], ['Noto Sans SC', '500', 30890], ['Noto Serif SC', '700', 30928], ['Fira Code', '400 500', 1551],
    ]);
    for (const font of manifest.fonts.slice(0, 3)) {
      expect(font.supplemental_codepoints).toEqual([0x20BB7]);
      expect(font.preserved_codepoints).toBe(font.original_codepoints);
    }
  });
});
