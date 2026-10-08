import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import * as kit from '../../../../src/frontend/src/shared/ui/index.js';
import postcss from '../../../../src/frontend/node_modules/postcss/lib/postcss.mjs';

describe('shared UI kit contract', () => {
  it('places the mobile full-screen panel above the tray under decision A', () => {
    const memory = postcss.parse(readFileSync(resolve('src/shared/ui/memory.css'), 'utf8'));
    const shell = postcss.parse(readFileSync(resolve('src/shared/ui/shell.css'), 'utf8'));
    let panelZ, trayZ;
    memory.walkAtRules('media', media => {
      if (media.params === '(max-width:759px)') media.walkRules('.ui-focus-panel', rule => {
        rule.walkDecls('z-index', decl => { panelZ = Number(decl.value); });
      });
    });
    shell.walkRules('.ui-shell-tray', rule => {
      rule.walkDecls('z-index', decl => { trayZ = Number(decl.value); });
    });
    expect(panelZ).toBeGreaterThan(trayZ);
  });
  it('exports every component specified in DESIGN section 7', () => {
    for (const name of ['Shell', 'ProjectSwitcher', 'Tray', 'StatusDot', 'ProgressDots',
      'Composer', 'Receipt', 'InsightChip', 'LayerBadges', 'LadderTrace', 'FocusPanel',
      'SourceDraftView', 'LayerTabs', 'FilterBar', 'Row', 'Breadcrumb', 'Switch', 'Icon']) {
      expect(kit[name], name).toBeTypeOf('function');
    }
  });

  it('uses canonical tokens without component dark overrides or literal colors', () => {
    const folder = resolve('src/shared/ui');
    const files = readdirSync(folder).filter(name => name.endsWith('.css'));
    expect(files.length).toBeGreaterThan(0);
    for (const file of files) {
      const css = readFileSync(resolve(folder, file), 'utf8');
      expect(css, file).not.toMatch(/#[\da-f]{3,8}\b/i);
      expect(css, file).not.toMatch(/rgba?\s*\(|hsla?\s*\(/i);
      expect(css, file).not.toMatch(/\[data-theme\s*=/);
      expect(css, file).not.toMatch(/outline\s*:\s*(?:0|none)\b/);
    }
  });
});
