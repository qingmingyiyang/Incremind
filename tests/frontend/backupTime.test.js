import { describe, expect, it } from 'vitest';
import { compareDateValues, formatLocalShortDate } from '../../src/frontend/src/shared/lib/time';

describe('backup time ordering', () => {
  it('orders actual instants across UTC offsets', () => {
    const earlier = '2026-11-03T00:19:00+08:00';
    const later = '2026-11-03T08:10:00+00:00';
    expect([later, earlier].sort(compareDateValues)).toEqual([earlier, later]);
    expect(compareDateValues('2026-11-03T08:10:00+08:00', '2026-11-03T00:19:00+00:00')).toBeLessThan(0);
    expect(compareDateValues('2026-11-03T08:10:00+00:00', '2026-11-03T16:10:00+08:00')).toBe(0);
  });

  it('keeps unknown dates distinct from real dates before the Unix epoch', () => {
    const snapshots = [undefined, '1960-01-01T00:00:00Z', '', 'invalid'].map(created_at => ({ created_at }));
    expect(snapshots.sort((left, right) => compareDateValues(left.created_at, right.created_at)).map(row => row.created_at))
      .toEqual([undefined, '', 'invalid', '1960-01-01T00:00:00Z']);
    expect(compareDateValues(undefined, '1960-01-01T00:00:00Z')).toBeLessThan(0);
    expect(formatLocalShortDate(undefined)).toBe('—');
    expect(formatLocalShortDate('invalid')).toBe('—');
  });
});
