import { expect, it } from 'vitest';
import { formatBytes } from '@src/shared/lib/formatBytes';

it.each([[0, '0 KB'], [1, '< 0.01 KB'], [512, '0.5 KB'], [1234, '1.21 KB'], [1048576, '1 MB'],
  [1610612736, '1.5 GB'], [null, '—'], [undefined, '—'], [-1, '—'], [Infinity, '—']])(
  'formats the measured byte value %s as %s', (input, expected) => expect(formatBytes(input)).toBe(expected));
