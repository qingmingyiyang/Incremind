import { describe, expect, it, vi } from "vitest";

import { CompanionApiError, createCompanionApi } from "@src/features/companion/companionApi";

function response(status, payload) {
  return { ok: status >= 200 && status < 300, status, json: vi.fn().mockResolvedValue(payload) };
}

describe("companionApi", () => {
  it("uses v2 week and todo endpoints with nullable CAS revisions", async () => {
    const request = vi.fn().mockResolvedValue(response(200, { items: [] }));
    const api = createCompanionApi({ request });
    await api.getWeekStats({ projectId: "p" });
    await api.listTodos({ projectId: "p", includeDone: true });
    await api.doneTodo({ id: "td1", expectedRevision: null });
    await api.undoTodo({ id: "td1", expectedRevision: 3 });
    expect(request.mock.calls[0][0]).toBe("/api/v2/stats/week?project_id=p");
    expect(request.mock.calls[1][0]).toBe("/api/v2/todos?include_done=true&project_id=p");
    expect(request.mock.calls[2]).toEqual(["/api/v2/todos/td1/done", expect.objectContaining({ method: "POST", body: JSON.stringify({ expected_revision: null }) })]);
    expect(request.mock.calls[3]).toEqual(["/api/v2/todos/td1/undo", expect.objectContaining({ method: "POST", body: JSON.stringify({ expected_revision: 3 }) })]);
  });
  it("uses exact bounded distillation GET and proposal-only POST payloads", async () => {
    const request = vi.fn().mockResolvedValue(response(200, { items: [] }));
    const api = createCompanionApi({ request });
    await api.listDistillableEpisodes({ projectId: "project-a", currentSessionId: "session-current" });
    await api.distillEpisode({ projectId: "project-a", currentSessionId: "session-current", episodeId: "episode-past", commandId: "distill-episode-past-1" });
    expect(request.mock.calls[0][0]).toBe("/api/rebuild/companion/memory-distillations/episodes?project_id=project-a&current_session_id=session-current");
    expect(request.mock.calls[1]).toEqual(["/api/rebuild/companion/memory-distillations", expect.objectContaining({ method: "POST", body: JSON.stringify({ project_id: "project-a", current_session_id: "session-current", episode_id: "episode-past", command_id: "distill-episode-past-1" }) })]);
  });
  it("uses a fixed same-origin endpoint and normalized settings payload", async () => {
    const request = vi.fn().mockResolvedValue(response(200, { revision: 3 }));
    const api = createCompanionApi({ request });

    await expect(api.saveSettings({ expectedRevision: 2, settings: { locale: "zh-CN" } }))
      .resolves.toEqual({ revision: 3 });
    expect(request).toHaveBeenCalledWith("/api/rebuild/companion/settings", expect.objectContaining({
      method: "PUT",
      credentials: "same-origin",
      cache: "no-store",
      body: JSON.stringify({ expected_revision: 2, settings: { locale: "zh-CN" } }),
    }));
    expect(() => createCompanionApi({ request, basePath: "https://example.com" })).toThrow(/fixed/);
  });

  it("uses fixed state projection and server-owned daily check-in contracts", async () => {
    const request = vi.fn().mockResolvedValue(response(200, { state: {} }));
    const api = createCompanionApi({ request });
    await api.getLifeState();
    await api.dailyCheckIn();
    expect(request.mock.calls[0][0]).toBe("/api/rebuild/companion/state");
    expect(request.mock.calls[1]).toEqual([
      "/api/rebuild/companion/state/daily-check-in",
      expect.objectContaining({ method: "POST", body: "{}" }),
    ]);
  });

  it.each([
    [409, "conflict", "conflict"],
    [403, "read_only", "read_only"],
    [404, "missing", "empty"],
    [503, "server_unavailable", "failed"],
  ])("normalizes HTTP %i as %s", async (status, code, kind) => {
    const api = createCompanionApi({
      request: vi.fn().mockResolvedValue(response(status, { error: { code, message: "失败" } })),
    });
    await expect(api.getStatus()).rejects.toMatchObject({
      name: "CompanionApiError",
      kind,
      status,
      code,
    });
  });

  it("distinguishes cancellation from retryable transport failure", async () => {
    const cancelled = createCompanionApi({
      request: vi.fn().mockRejectedValue(Object.assign(new Error("stop"), { name: "AbortError" })),
    });
    await expect(cancelled.getStatus()).rejects.toMatchObject({ kind: "cancelled", retryable: false });

    const failed = createCompanionApi({ request: vi.fn().mockRejectedValue(new Error("offline")) });
    try {
      await failed.getStatus();
      throw new Error("expected rejection");
    } catch (error) {
      expect(error).toBeInstanceOf(CompanionApiError);
      expect(error).toMatchObject({ kind: "failed", code: "network_error", retryable: true });
    }
  });

  it("builds an opaque history cursor without accepting a custom path", async () => {
    const request = vi.fn().mockResolvedValue(response(200, { items: [] }));
    const api = createCompanionApi({ request });
    await api.listHistory({ cursor: "opaque+/=", limit: 25 });
    expect(request.mock.calls[0][0]).toBe("/api/rebuild/companion/history?limit=25&cursor=opaque%2B%2F%3D");
  });

  it("hard deletes only one encoded message id through the fixed endpoint", async () => {
    const request = vi.fn().mockResolvedValue(response(200, { receipt: { status: "completed" } }));
    const api = createCompanionApi({ request });
    await api.deleteMessage({ messageId: "message:one" });
    expect(request.mock.calls[0]).toEqual([
      "/api/rebuild/companion/messages/message%3Aone",
      expect.objectContaining({ method: "DELETE", credentials: "same-origin" }),
    ]);
  });

  it("uses revisioned profile and prompt mutation contracts", async () => {
    const request = vi.fn().mockResolvedValue(response(200, { revision: 1 }));
    const api = createCompanionApi({ request });
    await api.saveProfile({
      expectedRevision: 2,
      profile: { nickname: "帝权", birthday: null, oc_address: "御主", relationship: "搭档", custom_notes: "" },
    });
    expect(request.mock.calls[0]).toEqual([
      "/api/rebuild/companion/profile",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          expected_revision: 2,
          profile: { nickname: "帝权", birthday: null, oc_address: "御主", relationship: "搭档", custom_notes: "" },
        }),
      }),
    ]);

    await api.saveCharacterPrompt({
      expectedBindingRevision: 3,
      expectedActivationRevision: 7,
      content: "角色设定",
    });
    expect(request.mock.calls[1][0]).toBe("/api/rebuild/companion/character-prompt");
    expect(JSON.parse(request.mock.calls[1][1].body)).toEqual({
      expected_binding_revision: 3,
      expected_activation_revision: 7,
      content: "角色设定",
    });

    await api.restoreCharacterPrompt({ expectedBindingRevision: 4, expectedActivationRevision: 8 });
    expect(request.mock.calls[2][0]).toBe("/api/rebuild/companion/character-prompt/restore");
    expect(request.mock.calls[2][1].method).toBe("POST");
  });

  it("uses fixed local manual and note endpoints", async () => {
    const request = vi.fn().mockResolvedValue(response(200, { items: [] }));
    const api = createCompanionApi({ request });
    await api.getManual();
    await api.listNotes();
    await api.createNote({ content: "本地灵感" });
    expect(request.mock.calls.map(([url]) => url)).toEqual([
      "/api/rebuild/companion/manual",
      "/api/rebuild/companion/notes",
      "/api/rebuild/companion/notes",
    ]);
    expect(request.mock.calls[2][1]).toEqual(expect.objectContaining({
      method: "POST",
      body: JSON.stringify({ content: "本地灵感" }),
    }));
  });

  it("uses exact bounded chat request and history contracts", async () => {
    const request = vi.fn().mockResolvedValue(response(200, { items: [] }));
    const api = createCompanionApi({ request });
    const signal = new AbortController().signal;

    await api.sendChat({ requestId: "request:one", text: "你好", signal });
    await api.sendChat({ requestId: "request:two", sessionId: "session:stable", text: "继续" });
    await api.sendChat({
      requestId: "request:three", text: "讨论记忆", memoryId: "atom-build", projectId: "project-alpha",
    });
    await api.sendChat({ requestId: "request:four", text: "回顾项目", projectId: "project-alpha" });
    await api.listChatMessages({ sessionId: "session:stable", limit: 24, signal });

    expect(request.mock.calls[0]).toEqual([
      "/api/rebuild/companion/chat",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ request_id: "request:one", text: "你好" }),
        signal,
      }),
    ]);
    expect(JSON.parse(request.mock.calls[1][1].body)).toEqual({
      request_id: "request:two",
      session_id: "session:stable",
      text: "继续",
    });
    expect(JSON.parse(request.mock.calls[2][1].body)).toEqual({
      request_id: "request:three", memory_id: "atom-build", project_id: "project-alpha", text: "讨论记忆",
    });
    expect(JSON.parse(request.mock.calls[3][1].body)).toEqual({
      request_id: "request:four", project_id: "project-alpha", text: "回顾项目",
    });
    expect(request.mock.calls[4][0]).toBe(
      "/api/rebuild/companion/chat/messages?session_id=session%3Astable&limit=24",
    );
  });

  it("uses consent-bound diary preview, generation, edit and source deletion contracts", async () => {
    const request = vi.fn().mockResolvedValue(response(200, { items: [] }));
    const api = createCompanionApi({ request });
    await api.previewDiary({ timezoneOffsetMinutes: 480 });
    await api.generateDiary({ requestId: "diary:req:one", timezoneOffsetMinutes: 480, previewFingerprint: "a".repeat(64), confirmEgress: true });
    await api.editDiary({ diaryId: "diary:20260722:1", content: "修改后的日记", expectedRevision: 1 });
    await api.deleteDiaryEvent({ eventId: "diary:focus:abc" });
    expect(request.mock.calls[0][0]).toBe("/api/rebuild/companion/diary/preview?timezone_offset_minutes=480");
    expect(JSON.parse(request.mock.calls[1][1].body)).toEqual({ request_id: "diary:req:one", timezone_offset_minutes: 480, preview_fingerprint: "a".repeat(64), confirm_egress: true });
    expect(request.mock.calls[2]).toEqual(["/api/rebuild/companion/diary/diary%3A20260722%3A1/edit", expect.objectContaining({ method: "POST" })]);
    expect(request.mock.calls[3]).toEqual(["/api/rebuild/companion/diary/events/diary%3Afocus%3Aabc", expect.objectContaining({ method: "DELETE" })]);
  });

  it("uses revisioned bounded system sensor settings", async () => {
    const request = vi.fn().mockResolvedValue(response(200, { revision: 4, config: {} }));
    const api = createCompanionApi({ request });
    await api.getSensors();
    await api.saveSensors({ enabled: true, networkEnabled: true, healthOrigin: "https://health.example", gameEnabled: true, gameProcesses: ["steam.exe"], gameBehavior: "quiet", expectedRevision: 3 });
    expect(request.mock.calls[0][0]).toBe("/api/rebuild/companion/sensors");
    expect(JSON.parse(request.mock.calls[1][1].body)).toEqual({ enabled: true, network_enabled: true, health_origin: "https://health.example", game_enabled: true, game_processes: ["steam.exe"], game_behavior: "quiet", expected_revision: 3 });
  });

  it("uses revisioned media sensing and separate model commentary consent", async () => {
    const request = vi.fn().mockResolvedValue(response(200, { revision: 2, config: {} }));
    const api = createCompanionApi({ request });
    await api.getMediaSession();
    await api.saveMediaSession({ enabled: true, modelCommentaryEnabled: false, expectedRevision: 1 });
    expect(request.mock.calls[0][0]).toBe("/api/rebuild/companion/media-session");
    expect(request.mock.calls[1]).toEqual([
      "/api/rebuild/companion/media-session/settings",
      expect.objectContaining({ method: "PUT", body: JSON.stringify({ enabled: true, model_commentary_enabled: false, expected_revision: 1 }) }),
    ]);
  });

  it("uses the authenticated Electron loopback origin and ignores unsafe origins", async () => {
    const original = globalThis.electronAPI;
    const request = vi.fn().mockResolvedValue(response(200, { items: [] }));
    try {
      globalThis.electronAPI = { backendBaseUrl: "http://127.0.0.1:43123/" };
      await createCompanionApi({ request }).listNotes();
      expect(request.mock.calls[0][0]).toBe("http://127.0.0.1:43123/api/rebuild/companion/notes");
      globalThis.electronAPI = { backendBaseUrl: "https://evil.example" };
      await createCompanionApi({ request }).listNotes();
      expect(request.mock.calls[1][0]).toBe("/api/rebuild/companion/notes");
    } finally {
      globalThis.electronAPI = original;
    }
  });
});
