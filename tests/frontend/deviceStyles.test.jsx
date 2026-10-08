import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { Row } from '../../src/frontend/src/shared/ui/Row';
import { Switch } from '../../src/frontend/src/shared/ui/Switch';

let styles = [];
function addStyle(path) {
  const style = document.createElement('style');
  style.textContent = readFileSync(resolve(process.cwd(), path), 'utf8');
  document.head.appendChild(style); styles.push(style);
}
afterEach(() => { cleanup(); styles.forEach(style => style.remove()); styles = []; });

it('actual device settings styles retain checked and unchecked Switch geometry and state', () => {
  addStyle('src/shared/ui/atoms.css');
  render(<div className="settings-page"><Row readOnly title="外发" trailing={<Switch label="外发" checked={true} onChange={() => {}}/>}/>
    <Row readOnly title="私密" trailing={<Switch label="私密" checked={false} onChange={() => {}}/>}/></div>);
  const controls = screen.getAllByRole('switch');
  const properties = ['padding-top', 'padding-bottom', 'padding-left', 'padding-right', 'border-radius', 'background-color', 'width', 'height'];
  const values = node => properties.map(property => getComputedStyle(node).getPropertyValue(property));
  const before = controls.map(values);
  addStyle('src/features/settings/Settings.css');
  expect(controls.map(values)).toEqual(before);
  expect(controls.map(node => node.getAttribute('aria-checked'))).toEqual(['true', 'false']);
});

it('device last-online metadata has the actual DESIGN size and ink token', () => {
  addStyle('src/shared/ui/atoms.css'); addStyle('src/features/settings/Settings.css');
  render(<div className="settings-page"><Row className="settings-device-row" readOnly title="手机" meta="刚刚"/></div>);
  const meta = screen.getByText('刚刚');
  expect(getComputedStyle(meta).fontSize).toBe('14px');
  expect(getComputedStyle(meta).color).toBe('var(--ink2)');
});
