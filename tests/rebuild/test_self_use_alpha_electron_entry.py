from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ELECTRON_ROOT = ROOT / "apps" / "desktop-electron"


def test_self_use_alpha_electron_package_exposes_launch_scripts() -> None:
    package = json.loads((ELECTRON_ROOT / "package.json").read_text(encoding="utf-8"))

    assert package["main"] == "src/main.cjs"
    assert package["scripts"]["doctor"] == "node src/doctor.cjs"
    assert package["scripts"]["dev"] == "electron ."
    assert package["scripts"]["smoke:file-picker"] == "electron src/file-picker-smoke.cjs"
    assert package["scripts"]["smoke:platform-adapter"] == "electron src/platform-adapter-smoke.cjs"
    assert package["scripts"]["start"] == "electron ."
    assert package["devDependencies"]["electron"].startswith("^")


def test_electron_main_loads_the_workspace_by_default_and_keeps_safe_shell() -> None:
    main = (ELECTRON_ROOT / "src" / "main.cjs").read_text(encoding="utf-8")
    desktop_system_ipc = (ELECTRON_ROOT / "src" / "desktop-system-ipc.cjs").read_text(encoding="utf-8")
    file_grant_ipc = (ELECTRON_ROOT / "src" / "file-grant-ipc-controller.cjs").read_text(encoding="utf-8")
    frontend_entry = (ELECTRON_ROOT / "src" / "frontend-entry.cjs").read_text(encoding="utf-8")
    renderer_security = (ELECTRON_ROOT / "src" / "renderer-security.cjs").read_text(encoding="utf-8")

    assert 'require("./frontend-entry.cjs")' in main
    assert "resolveEntry({" in main
    assert 'url.searchParams.get("view") || "home"' in frontend_entry
    assert "url.hash = new URLSearchParams({ view }).toString()" in frontend_entry
    assert 'petView ? DEFAULT_PET_VIEW : "home"' in frontend_entry
    assert 'require("./desktop-window-factory.cjs")' in main
    window_factory = (ELECTRON_ROOT / "src" / "desktop-window-factory.cjs").read_text(encoding="utf-8")
    assert "nodeIntegration: false" in window_factory
    assert "contextIsolation: true" in window_factory
    assert "sandbox: true" in window_factory
    assert 'require("./renderer-security.cjs")' in main
    assert "installRendererSessionPolicy" in main
    assert 'setWindowOpenHandler(() => ({ action: "deny" }))' in renderer_security
    assert 'webContents.on("will-navigate"' in renderer_security
    assert 'new FileGrantIpcController({' in main
    assert 'new DesktopSystemIpcController({' in main
    assert '"chriptmas:select-local-file"' in file_grant_ipc
    assert '"chriptmas:platform-info"' in desktop_system_ipc
    assert '"chriptmas:open-path"' in desktop_system_ipc
    assert '"chriptmas:show-notification"' in desktop_system_ipc
    assert '"chriptmas:register-shortcut"' in desktop_system_ipc
    assert 'this.app.getPath("userData")' in desktop_system_ipc
    assert "this.shell.openPath" in desktop_system_ipc
    assert "this.Notification.isSupported" in desktop_system_ipc
    assert "this.globalShortcut.register" in desktop_system_ipc
    assert "this.globalShortcut.unregisterAll" in desktop_system_ipc
    assert "this.dialog.showOpenDialog" in file_grant_ipc
    assert 'properties: ["openFile"]' in file_grant_ipc
    assert "localFileFilters(mediaKind)" in file_grant_ipc
    assert 'mediaKind === "document"' in file_grant_ipc
    assert 'name: "Documents"' in file_grant_ipc
    assert 'extensions: ["pdf", "doc", "docx"]' in file_grant_ipc
    file_picker_handler = file_grant_ipc.split(
        "async selectLocalFile",
        maxsplit=1,
    )[1].split(
        "async uploadLocalFile",
        maxsplit=1,
    )[0]
    assert "return result.filePaths[0]" in file_picker_handler
    assert "readFile" not in file_picker_handler
    assert "writeFile" not in file_picker_handler
    assert "spawn(" not in file_picker_handler
    assert "exec(" not in file_picker_handler


def test_electron_preload_exposes_workspace_metadata_and_narrow_platform_bridge() -> None:
    preload = (ELECTRON_ROOT / "src" / "preload.cjs").read_text(encoding="utf-8")

    assert "contextBridge.exposeInMainWorld" in preload
    assert "backendBaseUrl" in preload
    assert 'entry: "workspace"' in preload
    assert "selectLocalFile" in preload
    assert "getPlatformInfo" in preload
    assert "openPath" in preload
    assert "showNotification" in preload
    assert "registerShortcut" in preload
    assert "chriptmas:select-local-file" in preload
    assert "chriptmas:platform-info" in preload
    assert "chriptmas:open-path" in preload
    assert "chriptmas:show-notification" in preload
    assert "chriptmas:register-shortcut" in preload
    forbidden_channels = (
        "provider_execution",
        "memory_publication",
        "migration_execution",
    )
    for token in forbidden_channels:
        assert token not in preload
    assert preload.count("require(") == 1
    assert 'require("electron")' in preload
    assert 'require("node:fs")' not in preload
    assert 'require("node:child_process")' not in preload


def test_electron_doctor_reports_workspace_route_and_binary_checks() -> None:
    doctor = (ELECTRON_ROOT / "src" / "doctor.cjs").read_text(encoding="utf-8")

    assert "checkDesktopWorkspaceEntry" in doctor
    assert "view=home" in doctor
    assert "electron_binary_available" in doctor
    assert "preload_platform_bridge" in doctor
    assert "platform_adapter_main" in doctor
    assert "platform_adapter_preload" in doctor
    assert "main_security_and_route" in doctor
    assert "provider_execution" not in doctor
    assert "memory_publication" not in doctor
    assert "migration_execution" not in doctor


def test_self_use_alpha_electron_file_picker_smoke_uses_real_window_bridge() -> None:
    smoke = (ELECTRON_ROOT / "src" / "file-picker-smoke.cjs").read_text(encoding="utf-8")

    assert "new BrowserWindow" in smoke
    assert "show: true" in smoke
    assert "contextIsolation: true" in smoke
    assert "nodeIntegration: false" in smoke
    assert "sandbox: true" in smoke
    assert 'ipcMain.handle("chriptmas:select-local-file"' in smoke
    assert 'api.entry === "workspace"' in smoke
    assert "api.selectLocalFile({ mediaKind: \"document\" })" in smoke
    assert "file-path" in smoke
    assert "input.value.endsWith(\"#document\")" in smoke
    assert "writeFile" not in smoke
    assert "spawn(" not in smoke
    assert "exec(" not in smoke


def test_self_use_alpha_electron_platform_adapter_smoke_uses_real_window_bridge() -> None:
    smoke = (ELECTRON_ROOT / "src" / "platform-adapter-smoke.cjs").read_text(encoding="utf-8")

    assert "new BrowserWindow" in smoke
    assert "show: true" in smoke
    assert "contextIsolation: true" in smoke
    assert "nodeIntegration: false" in smoke
    assert "sandbox: true" in smoke
    assert 'ipcMain.handle("chriptmas:platform-info"' in smoke
    assert 'api.entry === "workspace"' in smoke
    assert 'ipcMain.handle("chriptmas:open-path"' in smoke
    assert 'ipcMain.handle("chriptmas:show-notification"' in smoke
    assert 'ipcMain.handle("chriptmas:register-shortcut"' in smoke
    assert "api.getPlatformInfo()" in smoke
    assert 'api.openPath("")' in smoke
    assert "api.showNotification" in smoke
    assert "api.registerShortcut" in smoke
    assert 'openRejected.status === "rejected"' in smoke
    assert "writeFile" not in smoke
    assert "spawn(" not in smoke
    assert "exec(" not in smoke


def test_real_os_file_picker_manual_smoke_records_native_dialog_boundary() -> None:
    manual_smoke = (ELECTRON_ROOT / "MANUAL_FILE_PICKER_SMOKE.md").read_text(encoding="utf-8")
    readme = (ELECTRON_ROOT / "README.md").read_text(encoding="utf-8")

    assert "npm run smoke:file-picker" in manual_smoke
    assert "does not open the native OS dialog" in manual_smoke
    assert "native OS file picker" in manual_smoke
    assert "image Source authorization" in manual_smoke
    assert "audio Source authorization" in manual_smoke
    assert "video Source authorization" in manual_smoke
    assert "document Source authorization" in manual_smoke
    assert "canceling the dialog" in manual_smoke
    assert "PDF / DOC / DOCX filter" in manual_smoke
    assert "No file bytes are read" in manual_smoke
    assert "No file path is authorized" in manual_smoke
    assert "No OCR" in manual_smoke
    assert "Provider command" in manual_smoke
    assert "Memory publication" in manual_smoke
    assert "No completed human-run record is claimed" in manual_smoke
    assert "MANUAL_FILE_PICKER_SMOKE.md" in readme
