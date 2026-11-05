import assert from 'node:assert/strict';
import { fork } from 'node:child_process';
import { once } from 'node:events';
import { readFile } from 'node:fs/promises';
import { resolve, isAbsolute, relative } from 'node:path';
import { clearSharedCommand, readSharedCommands, saveSharedCommand } from '../../src/frontend/src/features/shared-projects/sharedProjectCommands.js';

if (process.argv[2] === 'worker') {
  assert.equal(localStorage.constructor.name, 'Storage');
  localStorage.getItem('native-storage-ready');
  process.on('message', message => {
    try {
      let value;
      if (message.op === 'save') value = saveSharedCommand(message.command);
      else if (message.op === 'settle') { clearSharedCommand(message.command); value = true; }
      else if (message.op === 'read') value = readSharedCommands(message.key);
      else if (message.op === 'fill') {
        let low = 0, high = 16 * 1024 * 1024;
        while (low + 1 < high) {
          const size = Math.floor((low + high) / 2);
          try { localStorage.setItem('quota-pressure', 'x'.repeat(size)); low = size; }
          catch (error) { assert.equal(error.name, 'QuotaExceededError'); high = size; }
        }
        value = low;
      } else if (message.op === 'unfill') localStorage.removeItem('quota-pressure');
      else throw new Error('unsupported storage check');
      process.send({ sequence: message.sequence, value });
    } catch (error) { process.send({ sequence: message.sequence, error: error.message }); }
  });
  process.send({ ready: true, pid: process.pid });
} else {
  const input = resolve(process.argv[2]), assigned = resolve(process.env.CHRIPTMAS_APP_ROOT), inside = relative(assigned, input);
  assert.ok(inside && !inside.startsWith('..') && !isAbsolute(inside));
  const fixture = JSON.parse(await readFile(input, 'utf8'));
  assert.equal(fixture.results.length, 2);
  for (const value of fixture.results) assert.equal(value.status, 'failed');
  const workers = [], waiting = new Map(); let sequence = 0;
  async function start() {
    const child = fork(import.meta.filename, ['worker'], { execArgv: [`--localstorage-file=${input}.storage`, '--no-warnings'], stdio: ['ignore', 'ignore', 'inherit', 'ipc'] });
    workers.push(child);
    child.on('message', message => {
      if (!message.sequence) return;
      const pending = waiting.get(message.sequence); waiting.delete(message.sequence); clearTimeout(pending.timer); pending.resolve(message);
    });
    assert.equal((await once(child, 'message'))[0].ready, true); return child;
  }
  function call(child, message) {
    const id = ++sequence;
    return new Promise((resolveCall, reject) => {
      const timer = setTimeout(() => reject(new Error('native storage check timed out')), 15000);
      waiting.set(id, { resolve: resolveCall, timer }); child.send({ ...message, sequence: id });
    });
  }
  async function ok(child, message) { const result = await call(child, message); assert.equal(result.error, undefined); return result.value; }
  const key = 'synthetic-user-a:purpose-tests:http://127.0.0.1:18121:search:default';
  const commands = fixture.bodies.map(({ expected, ...body }) => ({ version: 1, storageKey: key, id: body.id,
    path: body.purpose, body, expected, phase: 'prepared', createdAt: new Date().toISOString() }));
  try {
    const a = await start(), b = await start(), [first, second] = commands;
    await Promise.all([ok(a, { op: 'save', command: first }), ok(b, { op: 'save', command: second })]);
    assert.deepEqual((await ok(a, { op: 'read', key })).map(value => value.id).sort(), commands.map(value => value.id).sort());
    await Promise.all([ok(a, { op: 'settle', command: first }), ok(b, { op: 'save', command: { ...second, phase: 'unknown' } })]);
    assert.ok((await call(a, { op: 'save', command: { ...first, phase: 'unknown' } })).error);
    assert.ok((await call(b, { op: 'save', command: { ...second, expected: { changed: true } } })).error);
    const refreshed = await start();
    assert.deepEqual((await ok(refreshed, { op: 'read', key })).map(value => value.id), [second.id]);
    for (const other of ['synthetic-user-b:purpose-tests:http://127.0.0.1:18121:search:default', 'synthetic-user-a:purpose-tests:https://another.invalid:search:default', `${key}:asr`]) {
      await ok(b, { op: 'save', command: { ...first, storageKey: other } });
      assert.deepEqual((await ok(refreshed, { op: 'read', key: other })).map(value => value.id), [first.id]);
    }
    assert.ok(await ok(a, { op: 'fill' }));
    assert.ok((await call(b, { op: 'save', command: { ...first, id: 'test-quota-refusal', body: { ...first.body, id: 'test-quota-refusal' } } })).error);
    await ok(a, { op: 'unfill' });
    assert.deepEqual((await ok(refreshed, { op: 'read', key })).map(value => value.id), [second.id]);
    process.stdout.write(JSON.stringify({ storage: 'native Node WebStorage', pids: workers.map(child => child.pid), cases: 8, actual_http_results: fixture.results.map(value => ({ id: value.id, status: value.status })) }));
  } finally {
    for (const value of waiting.values()) clearTimeout(value.timer);
    for (const child of workers) if (child.exitCode === null) child.kill();
    await Promise.all(workers.filter(child => child.exitCode === null).map(child => once(child, 'exit')));
  }
}
