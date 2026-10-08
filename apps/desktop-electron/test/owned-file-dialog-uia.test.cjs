const assert = require("node:assert/strict");
const { spawnSync } = require("node:child_process");
const path = require("node:path");
const test = require("node:test");

const script = path.join(__dirname, "..", "scripts", "owned-file-dialog-uia.ps1");

function powershell(args) {
  return spawnSync("powershell.exe", ["-NoProfile", "-NonInteractive", ...args], {
    encoding: "utf8", windowsHide: true, timeout: 10_000,
    env: { ...process.env, OWNED_DIALOG_SCRIPT: script, OWNED_DIALOG_TEST_PID: String(process.pid) },
  });
}

function currentIdentity() {
  const query = "$p=Get-CimInstance Win32_Process -Filter ('ProcessId = '+$env:OWNED_DIALOG_TEST_PID);[pscustomobject]@{ProcessId=[int]$p.ProcessId;CreationDate=$p.CreationDate.ToUniversalTime().ToString('o');ExecutablePath=[string]$p.ExecutablePath}|ConvertTo-Json -Compress";
  const result = powershell(["-Command", query]);
  assert.equal(result.status, 0, result.stderr);
  const value = JSON.parse(result.stdout);
  assert.ok(Number.isInteger(value.ProcessId));
  assert.equal(typeof value.CreationDate, "string");
  assert.equal(typeof value.ExecutablePath, "string");
  return value;
}

test("owned file dialog helper parses without loading UI Automation", { skip: process.platform !== "win32" }, () => {
  const command = "$tokens=$null;$errors=$null;[void][System.Management.Automation.Language.Parser]::ParseFile($env:OWNED_DIALOG_SCRIPT,[ref]$tokens,[ref]$errors);if($errors.Count){exit 1}";
  const result = powershell(["-Command", command]);
  assert.equal(result.status, 0, result.stderr);
});

test("owned file dialog helper validates the current process identity without opening a dialog", { skip: process.platform !== "win32" }, () => {
  const identity = currentIdentity();
  const result = powershell(["-File", script,
    "-OwnerPid", String(identity.ProcessId),
    "-OwnerCreationDate", identity.CreationDate,
    "-OwnerExecutablePath", identity.ExecutablePath,
    "-ValidateOnly",
  ]);
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), { status: "identity_validated", owner_pid: identity.ProcessId });
});

test("owned file dialog helper fails closed for a mismatched owner creation identity", { skip: process.platform !== "win32" }, () => {
  const identity = currentIdentity();
  const result = powershell(["-File", script,
    "-OwnerPid", String(identity.ProcessId),
    "-OwnerCreationDate", "invalid-creation-identity",
    "-OwnerExecutablePath", identity.ExecutablePath,
    "-ValidateOnly",
  ]);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /owner_creation_identity_mismatch/);
  assert.equal(result.stdout.trim(), "");
});

test("owned file dialog helper fails closed for a mismatched owner executable identity", { skip: process.platform !== "win32" }, () => {
  const identity = currentIdentity();
  const result = powershell(["-File", script,
    "-OwnerPid", String(identity.ProcessId),
    "-OwnerCreationDate", identity.CreationDate,
    "-OwnerExecutablePath", path.join(path.dirname(identity.ExecutablePath), "unrelated.exe"),
    "-ValidateOnly",
  ]);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /owner_executable_identity_mismatch/);
  assert.equal(result.stdout.trim(), "");
});

test("owned file dialog helper rejects an action outside its fixed allowlist", { skip: process.platform !== "win32" }, () => {
  const identity = currentIdentity();
  const result = powershell(["-File", script,
    "-OwnerPid", String(identity.ProcessId),
    "-OwnerCreationDate", identity.CreationDate,
    "-OwnerExecutablePath", identity.ExecutablePath,
    "-Action", "untrusted-action",
    "-ValidateOnly",
  ]);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /ValidateSet/);
  assert.equal(result.stdout.trim(), "");
});
