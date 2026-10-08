const fs = require('node:fs');
const path = require('node:path');
const { invokeOwnedDialog } = require('./e2e-native-file-picker.cjs');

const panelSelector = '[aria-label="数据根目录迁移"]';
const samePath = (left, right) => path.resolve(left).toLowerCase() === path.resolve(right).toLowerCase();

function clickExpression(label) {
  return `(() => {
    const panel = document.querySelector(${JSON.stringify(panelSelector)});
    const button = [...(panel?.querySelectorAll('button') || [])].find(node => node.textContent.trim() === ${JSON.stringify(label)});
    if (!button || button.disabled) throw new Error('root migration action unavailable');
    button.click();
  })()`;
}

async function runRootMigrationGate(options) {
  const { temporaryRoot, executable, waitFor, createDocumentExtractionFixtures,
    enableBuiltinDocumentExtraction, submitWorkspaceFile, submitWorkspaceText,
    assertStoredOriginalAsset, openWorkspaceSession, closeWorkspaceSession,
    readWindowsProcessIdentity, onSessionChanged, evidenceRoot } = options;
  let session = options.session;
  const initial = await session.page.evaluate('window.electronAPI.inspectRootMigration()');
  const oldRoot = path.join(temporaryRoot, 'vault');
  if (!initial.configured || !['vaultRoot', 'modelRoot', 'mediaRoot'].every(key => samePath(initial.configured[key], oldRoot))) {
    throw new Error('root migration initial roots escaped the controlled temporary Vault');
  }
  await session.page.evaluate(`(() => {
    localStorage.setItem('chriptmas-os-onboarding-v2-complete', 'true');
    document.querySelector('.first-run-onboarding-close')?.click();
  })()`);
  await enableBuiltinDocumentExtraction(session.page);
  const fixture = createDocumentExtractionFixtures(temporaryRoot)[0];
  const intake = await submitWorkspaceFile(session.page, fixture);
  const asset = assertStoredOriginalAsset(temporaryRoot, intake, fixture.bytes);
  const tasks = () => session.page.evaluate(`fetch(window.electronAPI.backendBaseUrl + '/api/rebuild/tasks?project_id=default').then(async response => {
    if (!response.ok) throw new Error('root migration task query failed');
    return response.json();
  })`);
  const originalTask = await waitFor(async () => {
    const rows = (await tasks()).items || [];
    if (rows.length !== 1 || rows[0].status !== 'delivered') throw new Error('root migration seed task not delivered');
    return rows[0];
  }, 'root migration seed delivery', 90000);

  const destinationParent = path.join(temporaryRoot, 'migration-destination');
  fs.mkdirSync(destinationParent);
  await session.page.evaluate(`(() => { window.location.hash = '#view=rebuild-settings&task=platform'; })()`);
  await waitFor(async () => {
    const ready = await session.page.evaluate(`Boolean(document.querySelector(${JSON.stringify(panelSelector)})?.querySelector('input[aria-label="数据根目录"]'))`);
    if (!ready) throw new Error('root migration settings panel unavailable');
    return true;
  }, 'root migration settings panel');
  await session.page.evaluate('window.electronAPI.openMainWindow()');
  await waitFor(async () => {
    if (!await session.page.evaluate('document.hasFocus()')) throw new Error('root migration main window is not focused');
    return true;
  }, 'root migration interaction focus', 10000);
  const folderSelection = await invokeOwnedDialog({ page: session.page, child: session.child,
    executable, fixtureRoot: temporaryRoot, filePath: destinationParent,
    action: 'open-directory', triggerExpression: clickExpression('选择上级文件夹') });
  const target = await waitFor(async () => {
    const value = await session.page.evaluate(`document.querySelector(${JSON.stringify(panelSelector)})?.querySelector('input[aria-label="数据根目录"]')?.value || ''`);
    if (!value) throw new Error('root migration target not returned from native picker');
    return value;
  }, 'root migration native target');
  if (!samePath(path.dirname(target), destinationParent) || fs.existsSync(target)) {
    throw new Error('root migration target is not a fresh controlled child directory');
  }

  const health = async () => session.page.evaluate(`fetch(window.electronAPI.backendBaseUrl + '/api/health').then(async response => {
    if (!response.ok) throw new Error('root migration sidecar not healthy');
    return response.json();
  })`);
  const refreshSidecar = async () => {
    const value = await waitFor(health, 'root migration restarted sidecar', 90000);
    session.sidecarPid = value.desktop_session.child_pid;
    session.sidecarIdentity = readWindowsProcessIdentity(session.sidecarPid);
    return value;
  };
  const initialPid = session.sidecarPid;
  await session.page.evaluate(clickExpression('检查迁移计划'));
  await waitFor(async () => {
    const result = await session.page.evaluate(`(() => {
      const panel = document.querySelector(${JSON.stringify(panelSelector)});
      return { error: panel?.querySelector('[role="alert"]')?.textContent || '', planned: panel?.textContent.includes('预检通过') };
    })()`);
    if (result.error) throw new Error('root migration preflight UI error: ' + result.error);
    if (!result.planned) throw new Error('root migration plan not ready');
    return true;
  }, 'root migration UI preflight', 90000);
  await refreshSidecar();
  const preflightPid = session.sidecarPid;
  if (preflightPid === initialPid) throw new Error('root migration preflight did not restart sidecar');
  const confirmation = await invokeOwnedDialog({ page: session.page, child: session.child,
    executable, fixtureRoot: temporaryRoot, filePath: destinationParent,
    action: 'confirm-root-migration', triggerExpression: clickExpression('确认并迁移') });
  await waitFor(async () => {
    const result = await session.page.evaluate(`(() => {
      const panel = document.querySelector(${JSON.stringify(panelSelector)});
      return { error: panel?.querySelector('[role="alert"]')?.textContent || '', complete: panel?.textContent.includes('新根目录配置已由重启后的桌面运行时读回') };
    })()`);
    if (result.error) throw new Error('root migration execute UI error: ' + result.error);
    if (!result.complete) throw new Error('root migration UI completion not visible');
    return true;
  }, 'root migration UI completion', 120000);
  const migratedHealth = await refreshSidecar();
  const migratedPid = session.sidecarPid;
  if (migratedPid === preflightPid) throw new Error('root migration execute did not restart sidecar');
  const assertConfigured = async () => {
    const diagnostic = await session.page.evaluate('window.electronAPI.inspectRootMigration()');
    if (!['vaultRoot', 'modelRoot', 'mediaRoot'].every(key => samePath(diagnostic.configured?.[key] || '', target))) {
      throw new Error('root migration configured roots do not match the native-selected target');
    }
  };
  await assertConfigured();
  const relativeAsset = path.join('library', ...asset.vault_ref.split('/'));
  if (!fs.readFileSync(path.join(target, relativeAsset)).equals(fixture.bytes)
    || !fs.readFileSync(path.join(oldRoot, relativeAsset)).equals(fixture.bytes)) {
    throw new Error('root migration copied asset or retained original changed');
  }
  if (evidenceRoot) {
    fs.mkdirSync(evidenceRoot, { recursive: true });
    await session.page.evaluate(`document.querySelector(${JSON.stringify(panelSelector)}).scrollIntoView({block:'center'})`);
    await new Promise(resolve => setTimeout(resolve, 200));
    const screenshot = await session.page.send('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(evidenceRoot, 'root-migration-completed.png'), Buffer.from(screenshot.data, 'base64'));
  }
  const afterWrite = await submitWorkspaceText(session.page, '迁移后受控写入。此条资料只应保存在新根目录。');
  const relativeSource = path.join('.rebuild-data', 'objects', 'default', 'sources', `${afterWrite.source_id}.json`);
  if (!fs.existsSync(path.join(target, relativeSource)) || fs.existsSync(path.join(oldRoot, relativeSource))) {
    throw new Error('root migration subsequent write did not exclusively use the new root');
  }
  await closeWorkspaceSession(session);
  session = await openWorkspaceSession(temporaryRoot);
  onSessionChanged(session);
  await assertConfigured();
  const restartedRows = (await tasks()).items || [];
  if (!restartedRows.some(row => row.task_ref === originalTask.task_ref && row.status === 'delivered')) {
    throw new Error('root migration seed task disappeared after full application restart');
  }
  if (!fs.existsSync(path.join(target, relativeSource)) || fs.existsSync(path.join(oldRoot, relativeSource))) {
    throw new Error('root migration write location changed after full restart');
  }
  return { status: 'passed', gate: 'packaged_root_migration', native_folder_selection: folderSelection.mode,
    native_confirmation: confirmation.mode, preflight_sidecar_restarted: true, migration_sidecar_restarted: true,
    runtime_root_revision: migratedHealth.runtime_roots?.revision || null,
    all_configured_roots_match: true, original_asset_bytes_equal: true, old_copy_retained: true,
    subsequent_write_only_in_new_root: true, full_restart_root_and_task_persist: true,
    isolated_app_data: true, formal_vault_used: false };
}

module.exports = { runRootMigrationGate };
