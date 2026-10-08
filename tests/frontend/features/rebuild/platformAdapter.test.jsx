import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// 必须在 import platformAdapter 之前 hoisted 生效。



import {
  getAutoUpdateStatus,
  getPlatformInfo,
  getShellStatus,
  isElectron,
  notify,
  openFolder,
  registerShortcut,
  selectLocalFile,
  _resetBrowserNotificationPermission,
} from "@src/features/rebuild/platformAdapter";

// platformAdapter 测试：覆盖 Electron / browser 两路分支
// - Electron 环境走 electronAPI
// - Browser 环境 fallback 到 Web API 或返回 unsupported

describe("platformAdapter", () => {
  let originalElectronAPI;
  let originalNotification;


  beforeEach(() => {
    originalElectronAPI = globalThis.electronAPI;
    originalNotification = globalThis.Notification;

    _resetBrowserNotificationPermission();

  });

  afterEach(() => {
    globalThis.electronAPI = originalElectronAPI;
    if (originalNotification === undefined) {
      delete globalThis.Notification;
    } else {
      globalThis.Notification = originalNotification;
    }

    _resetBrowserNotificationPermission();
  });

  // ── 环境检测 ──

  describe("getShellStatus / isElectron", () => {
    it("returns 'electron' when electronAPI.shell is set", () => {
      globalThis.electronAPI = { shell: "electron" };
      expect(getShellStatus()).toBe("electron");
      expect(isElectron()).toBe(true);

    });





    it("returns 'browser' when electronAPI is absent", () => {
      globalThis.electronAPI = undefined;
      expect(getShellStatus()).toBe("browser");
      expect(isElectron()).toBe(false);

    });
  });

  // ── openFolder ──

  describe("openFolder", () => {
    it("returns 'opened' when electronAPI.openPath succeeds", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        openPath: vi.fn().mockResolvedValue({ status: "opened" }),
      };
      const result = await openFolder("C:/Users/test/exports");
      expect(result.status).toBe("opened");
      expect(globalThis.electronAPI.openPath).toHaveBeenCalledWith("C:/Users/test/exports");
    });

    it("returns 'unsupported' in browser environment", async () => {
      globalThis.electronAPI = undefined;

      const result = await openFolder("C:/Users/test/exports");
      expect(result.status).toBe("unsupported");
    });

    it("returns 'rejected' when path is empty", async () => {
      globalThis.electronAPI = { shell: "electron", openPath: vi.fn() };
      const result = await openFolder("");
      expect(result.status).toBe("rejected");
      expect(globalThis.electronAPI.openPath).not.toHaveBeenCalled();
    });

    it("passes through non-opened status from electronAPI.openPath", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        openPath: vi.fn().mockResolvedValue({ status: "failed", reason: "path not found" }),
      };
      const result = await openFolder("C:/missing");
      expect(result.status).toBe("failed");
      expect(result.reason).toBe("path not found");
    });

    it("returns 'rejected' when openPath throws", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        openPath: vi.fn().mockRejectedValue(new Error("IPC failed")),
      };
      const result = await openFolder("C:/test");
      expect(result.status).toBe("rejected");
      expect(result.reason).toBe("IPC failed");
    });










    it("uses electronAPI.openPath is available", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        openPath: vi.fn().mockResolvedValue({ status: "opened" }),
      };
      await openFolder("C:/test");

    });

    it("uses browser fallback", async () => {
      globalThis.electronAPI = undefined;

      await openFolder("C:/test");

    });
  });

  // ── notify ──

  describe("notify", () => {
    it("uses electronAPI.showNotification when available", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        showNotification: vi.fn().mockResolvedValue({ status: "shown" }),
      };
      const result = await notify({ title: "已保存", body: "正在整理" });
      expect(result.status).toBe("shown");
      expect(globalThis.electronAPI.showNotification).toHaveBeenCalledWith({
        title: "已保存",
        body: "正在整理",
      });
    });

    it("falls back to browser Notification API when granted", async () => {
      globalThis.electronAPI = undefined;
      const mockNotification = vi.fn();
      globalThis.Notification = mockNotification;
      mockNotification.requestPermission = vi.fn().mockResolvedValue("granted");
      const result = await notify({ title: "Test", body: "Body" });
      expect(result.status).toBe("shown");
      expect(mockNotification.requestPermission).toHaveBeenCalled();
      expect(mockNotification).toHaveBeenCalledWith("Test", { body: "Body" });
    });

    it("returns 'denied' when browser notification permission not granted", async () => {
      globalThis.electronAPI = undefined;
      const mockNotification = vi.fn();
      globalThis.Notification = mockNotification;
      mockNotification.requestPermission = vi.fn().mockResolvedValue("denied");
      const result = await notify({ title: "Test" });
      expect(result.status).toBe("denied");
    });

    it("returns 'unsupported' when Notification API is not available", async () => {
      globalThis.electronAPI = undefined;
      delete globalThis.Notification;
      const result = await notify({ title: "Test" });
      expect(result.status).toBe("unsupported");
    });

    it("uses default title when title is missing", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        showNotification: vi.fn().mockResolvedValue({ status: "shown" }),
      };
      await notify({});
      expect(globalThis.electronAPI.showNotification).toHaveBeenCalledWith({
        title: "Chriptmas OS",
        body: "",
      });
    });








    it("uses electronAPI.showNotification is available", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        showNotification: vi.fn().mockResolvedValue({ status: "shown" }),
      };
      await notify({ title: "Test", body: "Body" });

    });

    it("uses browser fallback", async () => {
      globalThis.electronAPI = undefined;

      const mockNotification = vi.fn();
      globalThis.Notification = mockNotification;
      mockNotification.requestPermission = vi.fn().mockResolvedValue("granted");
      await notify({ title: "Test", body: "Body" });

    });
  });

  // ── selectLocalFile ──

  describe("selectLocalFile", () => {
    it("returns 'selected' with path when electronAPI.selectLocalFile returns string", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        selectLocalFile: vi.fn().mockResolvedValue("C:/docs/file.pdf"),
      };
      const result = await selectLocalFile({ mediaKind: "document" });
      expect(result.status).toBe("selected");
      expect(result.path).toBe("C:/docs/file.pdf");
      expect(globalThis.electronAPI.selectLocalFile).toHaveBeenCalledWith({ mediaKind: "document" });
    });

    it("returns 'cancelled' when user cancels (null)", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        selectLocalFile: vi.fn().mockResolvedValue(null),
      };
      const result = await selectLocalFile({ mediaKind: "image" });
      expect(result.status).toBe("cancelled");
    });

    it("returns 'unsupported' in browser environment", async () => {
      globalThis.electronAPI = undefined;
      const result = await selectLocalFile();
      expect(result.status).toBe("unsupported");
    });

    it("handles array return from legacy selectLocalFile", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        selectLocalFile: vi.fn().mockResolvedValue(["C:/path1", "C:/path2"]),
      };
      const result = await selectLocalFile();
      expect(result.status).toBe("selected");
      expect(result.path).toBe("C:/path1");
    });

    it("returns 'cancelled' when selectLocalFile throws", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        selectLocalFile: vi.fn().mockRejectedValue(new Error("dialog error")),
      };
      const result = await selectLocalFile();
      expect(result.status).toBe("cancelled");
    });
  });

  // ── getPlatformInfo ──

  describe("getPlatformInfo", () => {
    it("uses electronAPI.getPlatformInfo when available", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        getPlatformInfo: vi.fn().mockResolvedValue({
          appDataDir: "C:/AppData",
          pathSeparator: "\\",
          platform: "win32",
        }),
      };
      const info = await getPlatformInfo();
      expect(info.appDataDir).toBe("C:/AppData");
      expect(info.pathSeparator).toBe("\\");
      expect(info.platform).toBe("win32");
    });

    it("returns fallback info in browser environment", async () => {
      globalThis.electronAPI = undefined;
      const info = await getPlatformInfo();
      expect(info.shell).toBe("browser");
      expect(info.pathSeparator).toBe("/");
    });
  });

  // ── registerShortcut ──

  describe("registerShortcut", () => {
    it("returns 'registered' when electronAPI.registerShortcut succeeds", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        registerShortcut: vi.fn().mockResolvedValue({ status: "registered" }),
      };
      const result = await registerShortcut("Ctrl+Shift+Alt+R");
      expect(result.status).toBe("registered");
      expect(globalThis.electronAPI.registerShortcut).toHaveBeenCalledWith("Ctrl+Shift+Alt+R");
    });

    it("returns 'unsupported' in browser environment", async () => {
      globalThis.electronAPI = undefined;
      const result = await registerShortcut("Ctrl+Shift+Alt+R");
      expect(result.status).toBe("unsupported");
    });

    it("returns 'rejected' when accelerator is empty", async () => {
      globalThis.electronAPI = { shell: "electron", registerShortcut: vi.fn() };
      const result = await registerShortcut("");
      expect(result.status).toBe("rejected");
      expect(globalThis.electronAPI.registerShortcut).not.toHaveBeenCalled();
    });

    it("passes through non-registered status from electronAPI.registerShortcut", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        registerShortcut: vi.fn().mockResolvedValue({ status: "failed", reason: "conflict" }),
      };
      const result = await registerShortcut("Ctrl+X");
      expect(result.status).toBe("failed");
      expect(result.reason).toBe("conflict");
    });

    it("returns 'failed' when registerShortcut throws", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        registerShortcut: vi.fn().mockRejectedValue(new Error("IPC failed")),
      };
      const result = await registerShortcut("Ctrl+Shift+P");
      expect(result.status).toBe("failed");
      expect(result.reason).toBe("IPC failed");
    });










    it("uses electronAPI.registerShortcut is available", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        registerShortcut: vi.fn().mockResolvedValue({ status: "registered" }),
      };
      await registerShortcut("Ctrl+Shift+Alt+R");

    });

    it("returns 'unsupported' in browser environment ", async () => {
      globalThis.electronAPI = undefined;

      const result = await registerShortcut("Ctrl+Shift+Alt+R");
      expect(result.status).toBe("unsupported");

    });
  });

  // ── getAutoUpdateStatus ──

  describe("getAutoUpdateStatus", () => {
    it("returns unavailable placeholder when electronAPI.getAutoUpdateStatus available", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        getAutoUpdateStatus: vi.fn().mockResolvedValue({
          status: "unavailable",
          reason: "auto-update is not configured",
          enabled: false,
        }),
      };
      const result = await getAutoUpdateStatus();
      expect(result.status).toBe("unavailable");
      expect(result.enabled).toBe(false);
      expect(result.reason).toBe("auto-update is not configured");
      expect(globalThis.electronAPI.getAutoUpdateStatus).toHaveBeenCalled();
    });

    it("returns unavailable placeholder in browser environment", async () => {
      globalThis.electronAPI = undefined;
      const result = await getAutoUpdateStatus();
      expect(result.status).toBe("unavailable");
      expect(result.enabled).toBe(false);
    });

    it("returns unavailable when electronAPI.getAutoUpdateStatus throws", async () => {
      globalThis.electronAPI = {
        shell: "electron",
        getAutoUpdateStatus: vi.fn().mockRejectedValue(new Error("IPC failed")),
      };
      const result = await getAutoUpdateStatus();
      expect(result.status).toBe("unavailable");
      expect(result.enabled).toBe(false);
      expect(result.reason).toBe("IPC failed");
    });
  });
});
