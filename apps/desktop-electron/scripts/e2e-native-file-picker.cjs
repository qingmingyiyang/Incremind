const { spawn, spawnSync } = require('node:child_process');
const path = require('node:path');

async function invokeOwnedDialog({ page, child, executable, fixtureRoot, filePath, action = 'open', triggerExpression }) {
  const pid = child.pid;
  if (!Number.isInteger(pid) || child.exitCode !== null) throw new Error('native picker owner is unavailable');
  const identityRead = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command',
    `$entry=Get-CimInstance Win32_Process -Filter 'ProcessId = ${pid}';$window=Get-Process -Id ${pid};[pscustomobject]@{created=$entry.CreationDate.ToUniversalTime().ToString('o');executable=$entry.ExecutablePath;handle=$window.MainWindowHandle.ToInt64()}|ConvertTo-Json -Compress`],
  { encoding: 'utf8', windowsHide: true, timeout: 15000 });
  if (identityRead.status !== 0) throw new Error('native picker owner identity query failed');
  const identity = JSON.parse(identityRead.stdout.trim());
  if (!identity.handle || path.resolve(identity.executable).toLowerCase() !== path.resolve(executable).toLowerCase()) {
    throw new Error('native picker owner identity mismatch');
  }
  const helper = spawn('powershell.exe', ['-NoProfile', '-NonInteractive', '-File',
    path.join(__dirname, 'owned-file-dialog-uia.ps1'),
    '-OwnerPid', String(pid), '-OwnerCreationDate', identity.created,
    '-OwnerExecutablePath', executable, '-OwnerWindowHandle', String(identity.handle),
    '-FixtureRoot', fixtureRoot, '-FixturePath', filePath, '-Action', action, '-TimeoutSeconds', '20'],
  { windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'] });
  const invocation = new Promise((resolve, reject) => {
    let output = '';
    let error = '';
    helper.stdout.on('data', data => { output += data; });
    helper.stderr.on('data', data => { error += data; });
    helper.once('error', reject);
    helper.once('exit', code => {
      if (code !== 0) return reject(new Error(`owned native picker failed (${code}): ${error.trim()}`));
      try { resolve(JSON.parse(output.trim())); } catch { reject(new Error('owned native picker result invalid')); }
    });
  });
  const click = page.send('Runtime.evaluate', {
    expression: triggerExpression,
    userGesture: true,
    returnByValue: true,
  });
  const results = await Promise.allSettled([invocation, click]);
  for (const result of results) if (result.status === 'rejected') throw result.reason;
  if (results[1].value.exceptionDetails) throw new Error('native file input click failed');
  return results[0].value;
}

function selectOwnedFixture(options) {
  return invokeOwnedDialog({ ...options,
    triggerExpression: `document.querySelector('section[aria-label="工作台输入"] input[type="file"]').click()`,
  });
}

module.exports = { selectOwnedFixture, invokeOwnedDialog };
