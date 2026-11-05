import { expect, it } from 'vitest';
import { localGenerationStatus } from '@src/features/settings/localGenerationStatus';

it('shows actual missing file and dependency conditions before local selection', () => {
  const state = localGenerationStatus({ status: 'missing_assets', eligible: false,
    missing_files: ['config.json'], missing_dependencies: ['torch'] });
  expect(state.label).toBe('缺少文件');
  expect(state.detail).toContain('config.json'); expect(state.detail).toContain('torch');
  expect(state.selectable).toBe(false);
});

it('distinguishes qualified assets from actual loaded weights', () => {
  expect(localGenerationStatus({ status: 'available', eligible: true }).label).toBe('待加载');
  expect(localGenerationStatus({ status: 'loaded', eligible: true }).label).toBe('已加载');
  expect(localGenerationStatus({ status: 'available', eligible: true }).detail).toContain('首次使用时加载');
});

it('allows an explicit retry after real runtime failure and blocks changed loaded assets', () => {
  const failed = localGenerationStatus({ status: 'load_failed', eligible: true, reason_code: 'local_model_load_failed' });
  expect(failed.retry).toBe(true); expect(failed.selectable).toBe(true);
  const changed = localGenerationStatus({ status: 'restart_required', eligible: true,
    reason_code: 'local_model_assets_changed_restart_required' });
  expect(changed.selectable).toBe(false); expect(changed.detail).toContain('重启应用');
});
