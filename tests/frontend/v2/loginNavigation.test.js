import { afterEach, expect, it } from 'vitest';
import { resumeAuthenticatedPage } from '../../../src/frontend/src/features/settings/loginNavigation';

afterEach(() => { history.replaceState(null, '', '/'); });

it('preserves explicit capture and error destinations and notifies the mounted surface', () => {
  const observed = [], changed = () => observed.push(location.hash);
  window.addEventListener('hashchange', changed);
  try {
    for (const hash of ['#capture=one-share', '#capture-error=credentials', '#view=settings&section=devices']) {
      history.replaceState(null, '', '/login?source=share' + hash);
      resumeAuthenticatedPage();
      expect(location.pathname).toBe('/');
      expect(location.search).toBe('?source=share');
      expect(location.hash).toBe(hash);
      expect(observed.at(-1)).toBe(hash);
    }
    expect(observed).toHaveLength(3);
  } finally { window.removeEventListener('hashchange', changed); }
});

it('opens the workbench and synchronizes the surface when login has no destination', () => {
  history.replaceState(null, '', '/login');
  const observed = [], changed = () => observed.push(location.hash);
  window.addEventListener('hashchange', changed);
  try {
    resumeAuthenticatedPage();
    expect(location.hash).toBe('#view=workbench&project_id=default');
    expect(observed).toEqual(['#view=workbench&project_id=default']);
  } finally { window.removeEventListener('hashchange', changed); }
});
