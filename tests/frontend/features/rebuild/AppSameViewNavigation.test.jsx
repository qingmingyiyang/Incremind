import React from 'react';
import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
vi.mock('@src/features/library/Library', () => ({ default: ({ projectId }) => {
  const [initial] = React.useState(() => `${projectId}:${new URLSearchParams(location.hash.slice(1)).get('item_id') || 'none'}`);
  return <div data-testid="library-route-target">{initial}</div>;
} }));
vi.mock('@src/features/settings/SettingsPage', () => ({ default: ({ projectId, initialSection }) => {
  const [initial] = React.useState(() => `${projectId}:${initialSection}`);
  return <div data-testid="settings-route-target">{initial}</div>;
} }));
vi.mock('@src/features/companion/CompanionPage', () => ({ CompanionPage: ({ projectId, initialMode }) => <div>{projectId}:{initialMode}</div> }));
vi.mock('@src/features/workbench/Workbench', () => ({ default: ({ projectId }) => <div>workbench:{projectId}</div> }));
import { App } from '@src/App';
let mainNavigationListener;
beforeEach(() => {
  window.history.replaceState({}, '', '/#view=rebuild-library-overview&project_id=project-alpha&item_id=atom-first');
  localStorage.clear();
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ items: [] }) })));
  vi.stubGlobal('electronAPI', { subscribeMainNavigation: vi.fn(listener => { mainNavigationListener = listener; return vi.fn(); }) });
});
afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState({}, '', '/'); });
it('keeps the active project when native pet navigation opens Companion chat', async () => {
  render(<App />);
  await screen.findByTestId('library-route-target');
  act(() => mainNavigationListener({ panel: 'chat' }));
  expect(window.location.hash).toBe('#view=companion&mode=chat&project_id=project-alpha');
  expect(window.location.hash).not.toContain('atom-first');
});
it('routes retired speech model selection to the current settings shell', async () => {
  window.history.replaceState({}, '', '/#view=rebuild-model-selection&project_id=project-alpha');
  render(<App />);
  expect(await screen.findByTestId('settings-route-target')).toHaveTextContent('project-alpha:model');
  expect(screen.getByRole('button', { name: '设置' })).toHaveAttribute('aria-current', 'page');
});
it('accepts only the native weekly memory review intent and keeps the active project', async () => {
  render(<App />);
  await screen.findByTestId('library-route-target');
  act(() => mainNavigationListener({ panel: 'chat', intent: 'weekly_memory_review' }));
  expect(window.location.hash).toBe('#view=companion&mode=review&intent=weekly_memory_review&project_id=project-alpha');
  expect(window.location.hash).not.toContain('atom-first');
});
it('opens pending memories in the selected project from the native pet', async () => {
  render(<App />);
  await screen.findByTestId('library-route-target');
  act(() => mainNavigationListener({ view: 'rebuild-library-overview', filter: 'pending_memory' }));
  expect(window.location.hash).toBe('#view=library&filter=pending_memory&project_id=project-alpha');
  expect(window.location.hash).not.toContain('atom-first');
});
it('drops an unrecognized native Companion intent', async () => {
  render(<App />);
  await screen.findByTestId('library-route-target');
  act(() => mainNavigationListener({ panel: 'chat', intent: 'memory_topic_discussion' }));
  expect(window.location.hash).toBe('#view=companion&mode=chat&project_id=project-alpha');
});
it('remounts the Library route when same-view hash parameters change', async () => {
  render(<App />);
  expect(await screen.findByTestId('library-route-target')).toHaveTextContent('project-alpha:atom-first');
  window.history.replaceState({}, '', '/#view=rebuild-library-overview&project_id=project-alpha&item_id=candidate-second');
  fireEvent(window, new HashChangeEvent('hashchange'));
  expect(await screen.findByTestId('library-route-target')).toHaveTextContent('project-alpha:candidate-second');
});
it.each(['rebuild-video-workflow', 'rebuild-project-brain'])('remounts the current Library when the project changes through %s', async view => {
  window.history.replaceState({}, '', `/#view=${view}&project_id=project-alpha`);
  render(<App />);
  expect(await screen.findByTestId('library-route-target')).toHaveTextContent('project-alpha:none');
  window.history.replaceState({}, '', `/#view=${view}&project_id=project-beta`);
  fireEvent(window, new HashChangeEvent('hashchange'));
  expect(await screen.findByTestId('library-route-target')).toHaveTextContent('project-beta:none');
});
it('keeps the selected project when a retired project skill page falls back to Workbench', async () => {
  window.history.replaceState({}, '', '/#view=rebuild-project-skill-overview&project_id=project-alpha');
  render(<App />);
  expect(await screen.findByText('workbench:project-alpha')).toBeInTheDocument();
});
it('remounts Settings when the requested section changes in the same route', async () => {
  window.history.replaceState({}, '', '/#view=rebuild-settings&project_id=project-alpha&section=data');
  render(<App />);
  expect(await screen.findByTestId('settings-route-target')).toHaveTextContent('project-alpha:data');
  window.history.replaceState({}, '', '/#view=rebuild-settings&project_id=project-alpha&section=model');
  fireEvent(window, new HashChangeEvent('hashchange'));
  expect(await screen.findByTestId('settings-route-target')).toHaveTextContent('project-alpha:model');
});
