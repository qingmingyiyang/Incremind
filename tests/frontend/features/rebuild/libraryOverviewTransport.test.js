import { afterEach, describe, expect, it, vi } from "vitest";

import {
  libraryBackendUrl,
  responseJsonOrEmpty,
} from "@src/features/rebuild/libraryOverviewTransport";

const originalElectronApi = globalThis.electronAPI;


afterEach(() => {
  if (originalElectronApi === undefined) {
    delete globalThis.electronAPI;
  } else {
    globalThis.electronAPI = originalElectronApi;
  }

});

describe("libraryOverviewTransport", () => {
  it("uses the active desktop backend while preserving the browser endpoint", () => {
    delete globalThis.electronAPI;

    expect(libraryBackendUrl("/api/rebuild/library/overview")).toBe("/api/rebuild/library/overview");

    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:8001" };
    expect(libraryBackendUrl("/api/rebuild/library/overview")).toBe(
      "http://127.0.0.1:8001/api/rebuild/library/overview",
    );

    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:43123/" };
    expect(libraryBackendUrl("/api/rebuild/library/overview")).toBe(
      "http://127.0.0.1:43123/api/rebuild/library/overview",
    );
  });

  it("preserves the facade JSON fallback for empty or malformed error bodies", async () => {
    await expect(responseJsonOrEmpty({ json: vi.fn().mockResolvedValue({ detail: "ready" }) })).resolves.toEqual({
      detail: "ready",
    });
    await expect(responseJsonOrEmpty({ json: vi.fn().mockRejectedValue(new Error("invalid json")) })).resolves.toEqual({});
  });
});
