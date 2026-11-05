import assert from 'node:assert/strict';
import { fork } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
import { resolve, relative, isAbsolute } from 'node:path';
import { once } from 'node:events';
import {
  canResendBackupFile, createBackupCommand, isBackupCommandSettled, readBackupCommands, saveBackupCommand, settleBackupCommand,
} from '../../src/frontend/src/features/settings/backupCommands.js';

if (process.argv[2] === 'worker') {
  assert.equal(localStorage.constructor.name, 'Storage');
  localStorage.getItem('native-storage-ready');
  process.on('message', message => {
    try {
      let value;
      if (message.op === 'save') value = saveBackupCommand(message.command, message.phase);
      else if (message.op === 'settle') value = settleBackupCommand(message.command) ?? true;
      else if (message.op === 'settled') value = isBackupCommandSettled(message.command);
      else if (message.op === 'read') value = readBackupCommands(message.key, message.kind, message.legacy);
      else if (message.op === 'legacy') localStorage.setItem(message.key, JSON.stringify(message.body));
      else if (message.op === 'file-proof') {
        const first = new File(['old'], 'backup.zip', { lastModified: 100 });
        const second = new File(['new'], 'backup.zip', { lastModified: 100 });
        assert.equal(canResendBackupFile(message.command, first, { id: message.command.id, file: first }), true);
        assert.equal(canResendBackupFile(message.command, second, { id: message.command.id, file: first }), false);
        assert.equal(canResendBackupFile(message.command, first, null), false);
        value = true;
      } else if (message.op === 'fill') {
        let low = 0, high = 16 * 1024 * 1024;
        while (low + 1 < high) {
          const size = Math.floor((low + high) / 2);
          try { localStorage.setItem('quota-pressure', 'x'.repeat(size)); low = size; }
          catch (failure) { assert.equal(failure.name, 'QuotaExceededError'); high = size; }
        }
        value = low;
      } else if (message.op === 'unfill') localStorage.removeItem('quota-pressure');
      else throw new Error('unsupported test operation');
      process.send({ sequence: message.sequence, value });
    } catch (failure) { process.send({ sequence: message.sequence, error: failure.message }); }
  });
  process.send({ ready: true, pid: process.pid });
} else {
  const directory = resolve(process.argv[2] || '');
  const allowed = resolve('work/g12-backup-management');
  const scope = relative(allowed, directory);
  assert.ok(scope && !scope.startsWith('..') && !isAbsolute(scope));
  await mkdir(directory, { recursive: false });
  const storage = resolve(directory, 'local-storage');
  const workers = [], waits = new Map();
  let sequence = 0;
  async function start() {
    const child = fork(import.meta.filename, ['worker'], {
      execArgv: [`--localstorage-file=${storage}`, '--no-warnings'], stdio: ['ignore', 'ignore', 'inherit', 'ipc'],
    });
    workers.push(child);
    child.on('message', message => {
      if (message.sequence) {
        const pending = waits.get(message.sequence);
        waits.delete(message.sequence); clearTimeout(pending.timer); pending.resolve(message);
      }
    });
    const [ready] = await once(child, 'message');
    assert.equal(ready.ready, true);
    return child;
  }
  function call(child, message) {
    const id = ++sequence;
    return new Promise((resolveCall, reject) => {
      const timer = setTimeout(() => reject(new Error('real storage worker timed out')), 30000);
      waits.set(id, { resolve: resolveCall, timer }); child.send({ ...message, sequence: id });
    });
  }
  async function ok(child, message) {
    const result = await call(child, message);
    assert.equal(result.error, undefined); return result.value;
  }
  const userA = 'chriptmas-user:actor:person-a:backup-import-operations:http://127.0.0.1:18112';
  const userB = 'chriptmas-user:actor:person-b:backup-import-operations:http://127.0.0.1:18112';
  const otherDomain = 'chriptmas-user:actor:person-a:backup-schedule-operations:http://127.0.0.1:18112';
  const otherOrigin = 'chriptmas-user:actor:person-a:backup-import-operations:https://another.invalid';
  const command = (key, id) => createBackupCommand('import', key, id, { file: { name: `${id}.zip`, size: 3 } });
  const first = command(userA, 'storage-import-first'), second = command(userA, 'storage-import-second');
  try {
    const a = await start(), b = await start();
    await Promise.all([ok(a, { op: 'save', command: first }), ok(b, { op: 'save', command: second })]);
    for (const worker of [a, b]) assert.deepEqual((await ok(worker, { op: 'read', key: userA, kind: 'import' })).map(value => value.id).sort(),
      [first.id, second.id].sort());
    await Promise.all([ok(a, { op: 'settle', command: first }), ok(b, { op: 'save', command: second, phase: 'unknown' })]);
    assert.equal(await ok(b, { op: 'settled', command: first }), true);
    assert.equal(await ok(a, { op: 'settled', command: second }), false);
    assert.ok((await call(a, { op: 'save', command: first, phase: 'unknown' })).error);
    assert.ok((await call(b, { op: 'save', command: { ...second, body: { file: { name: 'different.zip', size: 3 } } } })).error);
    const refreshed = await start();
    assert.deepEqual((await ok(refreshed, { op: 'read', key: userA, kind: 'import' })).map(value => value.id), [second.id]);
    assert.equal(await ok(refreshed, { op: 'file-proof', command: second }), true);
    for (const key of [userB, otherOrigin]) await ok(b, { op: 'save', command: command(key, first.id) });
    const schedule = createBackupCommand('schedule', otherDomain, 'schedule-storage-first', {
      command_id: 'schedule-storage-first', expected_revision: 2, frequency: 'monthly', time: '09:00', timezone: 'UTC', weekday: 0, month_day: 31,
    });
    await ok(a, { op: 'save', command: schedule });
    assert.deepEqual((await ok(refreshed, { op: 'read', key: otherDomain, kind: 'schedule' }))[0].body, schedule.body);
    for (const key of [userB, otherOrigin]) assert.deepEqual((await ok(refreshed, { op: 'read', key, kind: 'import' })).map(value => value.id), [first.id]);
    const legacyKey = `${userA}:legacy`, legacy = { command_id: 'legacy-storage-import' };
    await ok(a, { op: 'legacy', key: legacyKey, body: legacy });
    const migrated = await ok(refreshed, { op: 'read', key: userA, kind: 'import', legacy: legacyKey });
    const old = migrated.find(value => value.id === legacy.command_id);
    assert.ok(old && old.body.file === null);
    await ok(a, { op: 'settle', command: old });
    assert.deepEqual((await ok(refreshed, { op: 'read', key: userA, kind: 'import', legacy: legacyKey })).map(value => value.id), [second.id]);
    assert.ok(await ok(a, { op: 'fill' }));
    assert.ok((await call(b, { op: 'save', command: command(userA, 'quota-storage-refusal') })).error);
    await ok(a, { op: 'unfill' });
    assert.deepEqual((await ok(refreshed, { op: 'read', key: userA, kind: 'import' })).map(value => value.id), [second.id]);
    console.log(JSON.stringify({ passed: 9, storage: 'native Node WebStorage', processes: workers.map(child => child.pid),
      cases: ['concurrent IDs', 'own-ID settlement', 'late state', 'immutable body', 'refresh', 'same File object', 'scope domains', 'legacy recovery', 'real quota refusal'] }));
  } finally {
    for (const pending of waits.values()) clearTimeout(pending.timer);
    for (const child of workers) if (child.exitCode === null) child.kill();
    await Promise.all(workers.filter(child => child.exitCode === null).map(child => once(child, 'exit')));
  }
}
