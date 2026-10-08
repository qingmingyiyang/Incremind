import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DAILY_REMINDERS_ENDPOINT,
  INSPIRATION_OVERVIEW_ENDPOINT,
  LIBRARY_ACTIVITY_OVERVIEW_ENDPOINT,
  LIBRARY_OVERVIEW_ENDPOINT,
  LIBRARY_SEARCH_ENDPOINT,
  dailyRemindersUrl,
  inspirationOverviewUrl,
  libraryActivityOverviewUrl,
  libraryOverviewSafeErrorMessage,
  libraryOverviewUrl,
  librarySearchUrl,
  loadDailyReminders,
  loadInspirationOverview,
  loadLibraryActivityOverview,
  loadLibraryOverview,
  searchLibrary,
} from "@src/features/rebuild/libraryOverviewReadApi";

const originalElectronApi = globalThis.electronAPI;

afterEach(() => {
  if (originalElectronApi === undefined) {
    delete globalThis.electronAPI;
  } else {
    globalThis.electronAPI = originalElectronApi;
  }
});

describe("libraryOverviewReadApi", () => {
  it("owns the five read endpoints and exposes the complete read-domain contract", () => {
    expect([
      LIBRARY_OVERVIEW_ENDPOINT,
      LIBRARY_ACTIVITY_OVERVIEW_ENDPOINT,
      LIBRARY_SEARCH_ENDPOINT,
      INSPIRATION_OVERVIEW_ENDPOINT,
      DAILY_REMINDERS_ENDPOINT,
    ]).toEqual([
      "/api/rebuild/library/overview",
      "/api/rebuild/library/activity-overview",
      "/api/rebuild/library/search",
      "/api/rebuild/inspirations/overview",
      "/api/rebuild/reminders/today",
    ]);
    expect([
      libraryOverviewUrl,
      libraryActivityOverviewUrl,
      inspirationOverviewUrl,
      dailyRemindersUrl,
      librarySearchUrl,
      libraryOverviewSafeErrorMessage,
      loadLibraryOverview,
      loadLibraryActivityOverview,
      loadInspirationOverview,
      loadDailyReminders,
      searchLibrary,
    ].every((value) => typeof value === "function")).toBe(true);
  });

  it("builds scoped read URLs through the shared desktop transport", () => {
    globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:43123/" };
    expect(libraryOverviewUrl({ projectId: "project alpha" })).toBe(
      "http://127.0.0.1:43123/api/rebuild/library/overview?project_id=project%20alpha",
    );
    expect(libraryActivityOverviewUrl({ projectId: "project-alpha", year: 2026 })).toBe(
      "http://127.0.0.1:43123/api/rebuild/library/activity-overview?project_id=project-alpha&year=2026",
    );
    expect(inspirationOverviewUrl({ projectId: "project alpha" })).toBe(
      "http://127.0.0.1:43123/api/rebuild/inspirations/overview?project_id=project%20alpha",
    );
  });

  it("keeps overview error copy safe while retaining diagnostic status and reason", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false,
      status: 418,
      json: vi.fn().mockResolvedValue({
        detail: "secret lease unavailable",
        reason: "provider:example",
      }),
    });

    await expect(loadLibraryOverview({ fetchImpl })).rejects.toMatchObject({
      message: "模型连接暂时不可用，请检查连接设置后重试。",
      status: 418,
      code: "provider:example",
    });
  });
});
