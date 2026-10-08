const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const test = require("node:test");

const { OUTPUT_LIMIT, WindowsMediaSessionAdapter, parseOutput } = require("../src/companion/media-session-adapter.cjs");

function childFixture() {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.kill = () => {};
  return child;
}

test("non-Windows media adapter stays unavailable without spawning", async () => {
  let spawned = 0;
  const adapter = new WindowsMediaSessionAdapter({ platform: "linux", spawnProcess: () => { spawned += 1; } });
  assert.equal((await adapter.sample()).status, "unavailable");
  assert.equal(spawned, 0);
});

test("Windows adapter uses fixed PowerShell arguments and parses one bounded record", async () => {
  const child = childFixture();
  let invocation;
  const adapter = new WindowsMediaSessionAdapter({
    platform: "win32",
    scriptPath: "C:\\safe\\media.ps1",
    spawnProcess: (command, args, options) => { invocation = { command, args, options }; return child; },
  });
  const pending = adapter.sample();
  child.stdout.emit("data", Buffer.from('{"status":"ready","source":"player","title":"Song","artist":"Artist","playback_status":"playing"}'));
  child.emit("close", 0);
  assert.deepEqual(await pending, { status: "ready", source: "player", title: "Song", artist: "Artist", playback_status: "playing" });
  assert.equal(invocation.command, "powershell.exe");
  assert.deepEqual(invocation.args.slice(0, 5), ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File"]);
  assert.equal(invocation.options.shell, false);
  assert.equal(invocation.options.windowsHide, true);
});

test("parser rejects extra fields multiline controls and oversized text", () => {
  const valid = { status: "ready", source: "p", title: "t", artist: "a", playback_status: "playing" };
  assert.equal(parseOutput(Buffer.from(`${JSON.stringify(valid)}\n${JSON.stringify(valid)}`)).status, "error");
  assert.equal(parseOutput(Buffer.from(JSON.stringify({ ...valid, secret: "x" }))).status, "error");
  assert.equal(parseOutput(Buffer.from(JSON.stringify({ ...valid, title: "bad\u0000title" }))).status, "error");
  assert.equal(parseOutput(Buffer.from(JSON.stringify({ ...valid, title: "x".repeat(161) }))).status, "error");
  assert.deepEqual(parseOutput(Buffer.from('{"status":"empty","source":"","title":"","artist":"","playback_status":"closed"}')), { status: "empty", source: "", title: "", artist: "", playback_status: "closed" });
});

test("adapter shares an in-flight sample and bounds output", async () => {
  const child = childFixture();
  let spawned = 0, terminated = 0;
  const adapter = new WindowsMediaSessionAdapter({ platform: "win32", spawnProcess: () => { spawned += 1; return child; }, terminateTree: async () => { terminated += 1; } });
  const first = adapter.sample(), second = adapter.sample();
  assert.equal(first, second);
  child.stdout.emit("data", Buffer.alloc(OUTPUT_LIMIT + 1));
  assert.equal((await first).status, "unavailable");
  assert.equal(spawned, 1);
  assert.equal(terminated, 1);
});

test("timeout and explicit cancel terminate the owned child", async () => {
  const children = [childFixture(), childFixture()];
  const timers = [];
  let terminated = 0;
  const adapter = new WindowsMediaSessionAdapter({
    platform: "win32", spawnProcess: () => children.shift(), terminateTree: async () => { terminated += 1; },
    setTimer: (handler) => { timers.push(handler); return timers.length; }, clearTimer: () => {}, timeoutMs: 1000,
  });
  const timed = adapter.sample();
  timers[0]();
  assert.equal((await timed).status, "unavailable");
  const cancelled = adapter.sample();
  assert.equal(adapter.cancel(), true);
  children[0]?.emit("close", 1);
  timers[1]();
  assert.equal((await cancelled).status, "unavailable");
  assert.equal(terminated, 2);
});
