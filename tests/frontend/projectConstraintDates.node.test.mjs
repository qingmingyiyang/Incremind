import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import test from 'node:test';
import { constraintDateComparison, constraintDateError, constraintDateFields, constraintDatePayload, constraintTimeSummary, localConstraintTime, rebaseConstraintDraft } from '../../src/frontend/src/features/settings/constraintDates.js';

const draft = (base = {}, changes = {}) => ({ base, ...constraintDateFields(base), ...changes });

test('empty dates retain unlimited bounds', () => {
  assert.deepEqual(constraintDatePayload(draft()), { validFrom: null, validUntil: null });
});

test('an unchanged timestamp retains its original microseconds and offset', () => {
  const base = { valid_from: '2026-11-03T12:15:30.123456+08:00', valid_until: '2026-11-04T12:15:30+08:00' };
  assert.deepEqual(constraintDatePayload(draft(base)), { validFrom: base.valid_from, validUntil: base.valid_until });
});

test('editing local time serializes an actual timezone-qualified instant', () => {
  const local = '2026-11-03T15:30';
  assert.equal(constraintDatePayload(draft({}, { valid_from: local })).validFrom, new Date(local).toISOString());
});

test('a user can clear either bound without changing the other', () => {
  const base = { valid_from: '2026-11-03T12:00:00Z', valid_until: '2026-11-04T12:00:00Z' };
  assert.deepEqual(constraintDatePayload(draft(base, { valid_from: '' })), { validFrom: null, validUntil: base.valid_until });
  assert.deepEqual(constraintDatePayload(draft(base, { valid_until: '' })), { validFrom: base.valid_from, validUntil: null });
});

test('equal or reversed bounds are rejected before sending', () => {
  const base = { valid_from: '2026-11-03T12:00:00Z', valid_until: '2026-11-03T12:00:00Z' };
  assert.match(constraintDateError(draft(base)), /结束须晚于开始/);
  assert.throws(() => constraintDatePayload(draft({}, { valid_from: '2026-11-04T12:00', valid_until: '2026-11-03T12:00' })), /结束须晚于开始/);
});

test('invalid calendar values remain explicit failures', () => {
  assert.match(constraintDateError(draft({}, { valid_from: '2026-02-30T12:00' })), /有效时间无效/);
  assert.throws(() => localConstraintTime('invalid'), /有效时间无效/);
});

test('a timezone change cannot reinterpret an already-open date draft', () => {
  assert.match(constraintDateError({ ...draft({}, { valid_from: '2026-11-03T12:00' }), time_zone: 'changed-zone' }), /本地时区变化/);
});

test('conflict comparison includes both authoritative bounds', () => {
  const row = { content: '约束', valid_from: '2026-11-03T12:00:00Z', valid_until: null };
  assert.deepEqual(constraintDateComparison(row), { content: '约束', valid_from: row.valid_from, valid_until: '不限' });
  assert.deepEqual(constraintDateComparison({ content: row.content, ...draft(row) }, true), constraintDateComparison(row));
});

test('keeping a local conflict preserves local times against the new revision', () => {
  const base = { revision: 1, valid_from: '2026-11-03T12:00:00Z' };
  const local = draft(base);
  const server = { revision: 2, valid_from: '2026-11-04T12:00:00Z' };
  const rebased = rebaseConstraintDraft(local, server);
  assert.equal(constraintDatePayload(rebased).validFrom, new Date(base.valid_from).toISOString());
});

test('new edits made during a save survive updating the base revision', () => {
  const submitted = draft({ revision: 1, valid_from: '2026-11-03T12:00:00Z' });
  const edited = { ...submitted, valid_until: '2026-11-10T09:00' };
  const saved = { ...submitted.base, revision: 2 };
  assert.equal(constraintDatePayload(rebaseConstraintDraft(edited, saved)).validUntil, new Date('2026-11-10T09:00').toISOString());
});

test('adopting a server conflict replaces content and both bounds together', () => {
  const local = { content: '本机', ...draft({}, { valid_from: '2026-11-03T12:00' }) };
  const server = { content: '服务端', revision: 2, valid_until: '2026-11-10T12:00:00Z' };
  const selected = rebaseConstraintDraft(local, server, true);
  assert.equal(selected.content, server.content);
  assert.deepEqual(constraintDatePayload(selected), { validFrom: null, validUntil: server.valid_until });
});

test('time status uses the original closed-open validity boundary', () => {
  const row = { enabled: true, valid_from: '2026-11-03T12:00:00Z', valid_until: '2026-11-04T12:00:00Z' };
  assert.equal(constraintTimeSummary(row, new Date(row.valid_from)), '生效');
  assert.equal(constraintTimeSummary(row, new Date('2026-11-03T11:59:59Z')), '尚未开始');
  assert.equal(constraintTimeSummary(row, new Date(row.valid_until)), '已到期');
  assert.equal(constraintTimeSummary({ ...row, enabled: false }), '已停用');
});

test('real browser-equivalent time zones preserve local display and reject a DST gap', () => {
  const moduleUrl = new URL('../../src/frontend/src/features/settings/constraintDates.js', import.meta.url).href;
  const code = `import {constraintDatePayload, constraintDateFields} from ${JSON.stringify(moduleUrl)}; const base={valid_from:'2026-11-01T06:30:00.123456Z'}; let gap=false; try {constraintDatePayload({base:{},valid_from:'2026-03-08T02:30',valid_until:''})} catch {gap=true} console.log(JSON.stringify({fields:constraintDateFields(base), kept:constraintDatePayload({base,...constraintDateFields(base)}),gap}));`;
  const read = zone => JSON.parse(execFileSync(process.execPath, ['--input-type=module', '-e', code], { env: { ...process.env, TZ: zone }, encoding: 'utf8' }));
  const ny = read('America/New_York');
  assert.equal(ny.fields.valid_from, '2026-11-01T01:30:00.123');
  assert.equal(ny.kept.validFrom, '2026-11-01T06:30:00.123456Z');
  assert.equal(ny.gap, true);
  const shanghai = read('Asia/Shanghai');
  assert.equal(shanghai.fields.valid_from, '2026-11-01T14:30:00.123');
  assert.equal(shanghai.gap, false);
});
