# Chriptmas OS Electron Shell

This is the Electron shell for the Chriptmas OS MVP workspace. The default user route is the three-navigation workbench; Self-use Alpha remains an explicit readiness/diagnostic route.

The shell follows `vNext PRD.md`: Electron main owns the desktop lifecycle, instance identity, VaultRoot and PlatformAdapter; the React renderer is paired with the existing Python/FastAPI local sidecar for product APIs. The Phase 0 package stage includes the verified CPU sidecar runtime in `resources/sidecar`; Electron main starts it on an authenticated dynamic loopback port and stores user data in appData rather than the installed package.

## Development Open Path

Start the frontend dev server first:

```powershell
cd F:\Chriptmas_OS\src\frontend
npm run dev
```

Then open the Electron shell:

```powershell
cd F:\Chriptmas_OS\apps\desktop-electron
npm install
npm run dev
```

Expected entry:

```text
http://127.0.0.1:4173/?view=home
```

If needed, override the frontend URL:

```powershell
$env:CHRIPTMAS_REPLAY_FRONTEND_URL="http://127.0.0.1:4173/?view=home"
npm run dev
```

Use `?view=rebuild-self-use-alpha` only when opening the explicit readiness/diagnostic entry.

If needed, override the backend base URL exposed to the frontend:

```powershell
$env:CHRIPTMAS_REPLAY_BACKEND_URL="http://127.0.0.1:8001"
npm run dev
```

## Safety Boundary

The preload exposes only:

- `electronAPI.backendBaseUrl`
- `electronAPI.shell`
- `electronAPI.entry`
- `electronAPI.selectLocalFile`
- `electronAPI.getPlatformInfo`
- `electronAPI.openPath`
- `electronAPI.showNotification`
- `electronAPI.registerShortcut`
- `electronAPI.setWindowAppearance`

`selectLocalFile` only opens a native file picker after a user action and returns the selected path to the renderer. It does not read file bytes, authorize the path, run Provider commands, publish Memory, delete files, migrate data, parse batches, run OCR, run transcription, or extract frames.

The platform adapter bridge is intentionally narrow:

- `getPlatformInfo` returns app data directory, platform, shell, and path separator metadata.
- `openPath` asks the OS shell to open a user-provided path and rejects empty paths.
- `showNotification` displays a local desktop notification when supported.
- `registerShortcut` registers a single accelerator and focuses the existing window when triggered.
- `setWindowAppearance` accepts only `light`, `dark`, or `system` and synchronizes the native caption symbols with the renderer theme.

These adapter calls do not read file bytes, execute Provider commands, expose secrets, upload data, or publish Memory.

## 打包用的 Python 运行时

将便携 Python 放在仓库根的 `python-runtime/`，其中必须有 `python.exe`；也可用环境变量 `CHRIPTMAS_SIDECAR_PYTHON_RUNTIME` 指定运行时目录。不要放在 `runtime/`，那是用户数据根。开发态仍可用 `CHRIPTMAS_OS_PYTHON` 指定 Python；安装包内的路径仍为 `resources/sidecar/runtime`。

复制前检查运行时来源，暂存后检查整个 `.sidecar-stage`，打包验证检查整个 `resources`。数据闸门按文件路径拦截 `secrets.json`、`.env`、`.env.*`、`data-root.json`、SQLite/DB 文件及 WAL/SHM；运行时顶层的 `.rebuild-data`、`workspace`、`backups`、`logs`、`data` 和 `config/settings.toml` 也会被拦截。conda 的 `Library/` 可以保留。闸门不读文件内容；失败会删除 `.sidecar-stage`。必要的第三方同名文件必须逐路径登记放行原因，目前没有放行项。

应用代码只复制 Git 跟踪的 `src/backend`、`src/core`、`src/rebuild` 文件，未跟踪和被忽略的文件不进包，缺 Git 时构建失败。

## Packaged Electron smoke

Build the unpacked candidate, then run the real-process smoke on a Windows host permitted to start GUI child processes:

```powershell
npm run build:dir
npm run test:e2e:electron
```

Windows日常重建可直接双击仓库根目录的`Build-Chriptmas-OS.bat`。该入口严格串行执行前端构建、前端复制、clean sidecar staging、unpacked打包、逐文件前端一致性校验、build verifier和真实Electron E2E；任一步失败都会停止。命令行等价入口为`npm run build:windows`。只复核已有候选使用`npm run build:windows:verify`，生成NSIS安装包使用`npm run build:windows:installer`。

`npm run build:dir` is an unpacked validation build. Generate the installable NSIS candidate with:

```powershell
npm run build
```

The current frozen candidate is explicitly unsigned and unpublished. Build output does not grant permission to install, sign, or distribute it. Use [the release and validation entry](../../docs/release/README.md) for the candidate manifest, recovery boundary, pending installation tests, and authoritative status.

The smoke creates a unique Electron `--user-data-dir`, starts the real unpacked executable, verifies through the preload bridge that sidecar appData stays inside that temporary root, waits for the real workspace renderer plus authenticated sidecar health, verifies the workbench/library/settings navigation, then terminates the complete Electron process tree and checks that the sidecar PID is gone. It does not use a mock backend or user appData.

On failure it writes a redacted JSON diagnostic and, when a renderer is available, a screenshot under `%TEMP%\chriptmas-electron-e2e-artifacts` (or `CHRIPTMAS_E2E_ARTIFACT_DIR`). The P0 harness removes common external Provider credentials, sends external HTTP(S) traffic to an unreachable local proxy, rejects Provider-enhanced classification, then submits a fixed local-only text through the real workspace UI. It verifies the returned Source/Job and Library visibility, restarts the same temporary appData root, verifies persistence, and checks sidecar cleanup. It requires a GUI-capable Windows host to establish the initial renderer process.

## File Picker Smoke

`npm run smoke:file-picker` verifies the real BrowserWindow, preload bridge, IPC path, and renderer input fill with a controlled path. It does not open the native OS file picker.

Use `MANUAL_FILE_PICKER_SMOKE.md` for the human-run real OS dialog smoke. That checklist covers image, audio, video, document, and cancel behavior while keeping selection separate from authorization, Provider runs, and Memory publication.

## Platform Adapter Smoke

`npm run smoke:platform-adapter` verifies the real BrowserWindow and preload bridge for appDataDir, path separator metadata, notification bridge, shortcut bridge, and rejected empty openPath behavior. It does not open a real folder or register a production shortcut.
