import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { Shell } from '@src/shared/ui/Shell';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
it('shows the initial offline state and restores a solid point on reconnect', async () => {
  vi.spyOn(navigator, 'onLine', 'get').mockReturnValue(false);
  render(<Shell project="alpha"/>);
  expect(screen.getByRole('img', { name: '离线' })).toHaveClass('ui-status-offline');
  await act(async () => window.dispatchEvent(new Event('online')));
  expect(screen.getByRole('img', { name: '在线' })).toHaveClass('ui-status-online');
  await act(async () => window.dispatchEvent(new Event('offline')));
  expect(screen.getByRole('img', { name: '离线' })).toHaveClass('ui-status-offline');
  expect(screen.getByRole('navigation', { name: '主导航' }).querySelectorAll('button')).toHaveLength(3);
});
it('removes both connectivity listeners on shell unmount', () => {
  const add = vi.spyOn(window, 'addEventListener'), remove = vi.spyOn(window, 'removeEventListener');
  const app = render(<Shell project="alpha"/>); app.unmount();
  for (const event of ['online', 'offline']) {
    const listener = add.mock.calls.find(([kind]) => kind === event)?.[1];
    expect(listener).toBeTypeOf('function');
    expect(remove).toHaveBeenCalledWith(event, listener);
  }
});
