import { readFileSync } from 'node:fs';
import { resolve as resolvePath } from 'node:path';
import { describe, expect, it } from 'vitest';
import postcss from '../../src/frontend/node_modules/postcss/lib/postcss.mjs';
import tailwind from '../../src/frontend/tailwind.config.js';

const css = readFileSync(resolvePath('src/styles.css'), 'utf8');
const sheet = postcss.parse(css);
const light = ['#F7F4EE', '#FFFDF9', '#EFEAE1', '#1C1A17', '#4E4A44', '#6F6A62', '#E6E0D5', '#D2CABC', '#C0282F', '#FFFFFF', '#F4E3AE', 'rgba(28,26,23,.14)'];
const dark = ['#1A1815', '#221F1B', '#2A2621', '#ECE6DB', '#C4BDB1', '#A39C90', '#332F29', '#48423A', '#E66B6F', '#1A1815', '#5A4919', 'rgba(0,0,0,.45)'];
const names = ['paper', 'panel', 'sunk', 'ink', 'ink2', 'muted', 'line', 'line2', 'red', 'on-red', 'mark', 'shadow'];
const declarations = (rule) => Object.fromEntries(rule.nodes.filter(node => node.type === 'decl').map(node => [node.prop, node.value]));

// Evaluate actual custom-property declarations in source order, including media
// and selector specificity. Browser checks additionally verify rendered values.
function tokens(theme, systemDark = false) {
  const result = {};
  const candidates = [];
  sheet.walkRules(rule => {
    if (!rule.selector.includes(':root')) return;
    const media = rule.parent.type === 'atrule' && rule.parent.name === 'media';
    if (media && (!systemDark || !rule.parent.params.includes('prefers-color-scheme: dark'))) return;
    const matches = rule.selector.split(',').some(selector => {
      const s = selector.trim();
      if (s === ':root') return true;
      if (s === ':root:not([data-theme])') return !theme;
      return s === `:root[data-theme="${theme}"]`;
    });
    if (matches) candidates.push({ rule, specificity: rule.selector.includes('[data-theme') ? 1 : 0 });
  });
  candidates.sort((a, b) => a.specificity - b.specificity).forEach(({ rule }) => Object.assign(result, declarations(rule)));
  return result;
}
function resolve(value, values, chain = []) {
  return value.replace(/var\((--[\w-]+)\)/g, (_, key) => {
    expect(chain).not.toContain(key);
    expect(values[key], key).toBeDefined();
    return resolve(values[key], values, [...chain, key]);
  });
}

describe('warm paper design token contract', () => {
  it.each([['light', false, light], ['dark', false, dark], [undefined, false, light], [undefined, true, dark], ['system', true, dark], ['light', true, light]])('uses exact palette for %s with systemDark=%s', (theme, systemDark, expected) => {
    const values = tokens(theme, systemDark);
    expect(names.map(name => values[`--${name}`])).toEqual(expected);
    expect(values['color-scheme']).toBe(expected === dark ? 'dark' : 'light');
  });
  it('maps legacy colors and font names to the canonical palette without broken references', () => {
    for (const theme of ['light', 'dark']) {
      const values = tokens(theme);
      for (const [key, value] of Object.entries(values)) if (key.startsWith('--')) resolve(value, values, [key]);
      for (const [key, target] of Object.entries({ '--cr-canvas': 'paper', '--cr-paper': 'panel', '--cr-ink': 'ink', '--cr-text': 'ink2', '--cr-muted': 'muted', '--cr-red': 'red', '--cr-text-on-accent': 'on-red', '--cr-line-strong': 'line2' })) {
        expect(resolve(values[key], values)).toBe(values[`--${target}`]);
      }
      expect(values['--serif']).toContain('Noto Serif SC');
      expect(values['--sans']).toContain('Noto Sans SC');
      expect(values['--mono']).toContain('Fira Code');
      expect(values['--r-control']).toBe('4px');
      expect(values['--r-panel']).toBe('12px');
      for (const state of ['neutral', 'success', 'warning', 'error', 'conflict']) {
        expect(resolve(values[`--cr-state-${state}-bg`], values)).toBe(values['--sunk']);
        expect(resolve(values[`--cr-state-${state}-fg`], values)).not.toBe(values['--sunk']);
      }
    }
    expect(css).not.toMatch(/fraunces/i);
  });
  it('defines literal colors only in canonical palette tokens', () => {
    sheet.walkDecls(declaration => {
      if (/#[\da-f]{3}|rgba?\(/i.test(declaration.value)) expect(names.map(name => `--${name}`)).toContain(declaration.prop);
    });
  });
  it('keeps Tailwind colors and fonts as references with only prescribed radii', () => {
    for (const value of Object.values(tailwind.theme.extend.colors)) expect(value).toMatch(/^var\(--[\w-]+\)$/);
    for (const value of Object.values(tailwind.theme.extend.fontFamily)) expect(value).toMatch(/^var\(--(?:serif|sans|mono)\)$/);
    for (const value of Object.values(tailwind.theme.extend.borderRadius)) expect(value).toMatch(/^(var\(--r-(control|panel)\)|999px)$/);
  });
  it('restores both global and existing control keyboard focus rings', () => {
    for (const selector of [':focus-visible', '.cr-ui-control:focus-visible']) {
      let rule;
      sheet.walkRules(selector, matched => { rule = matched; });
      expect(rule, selector).toBeDefined();
      expect(declarations(rule).outline).toBe('2px solid var(--red)');
      expect(declarations(rule)['outline-offset']).toBe('2px');
      if (selector === ':focus-visible') {
        expect(rule.nodes.find(node => node.prop === 'outline').important).toBe(true);
        expect(rule.nodes.find(node => node.prop === 'outline-offset').important).toBe(true);
      }
    }
    expect(css).not.toMatch(/outline:\s*0\s*;/);
  });
});
