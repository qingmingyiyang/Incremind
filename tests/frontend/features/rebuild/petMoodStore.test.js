import { describe, expect, it, vi } from "vitest";

import {
  loadPetMood,
  normalizePetMood,
  subscribeCompanionState,
} from "@src/features/rebuild/petMoodStore";

describe("memory-backed pet mood", () => {
  it("loads the existing read-only memory activity projection", async () => {
    const fetchImpl = vi.fn(async () => ({
      ok: true,
      json: async () => ({
        mood: "curious",
        today_activity_count: 2,
        recent_7d_activity_count: 5,
        execution: { effect: "inactive", lease: "none" },
        pending_memory_candidate_count: 1,
        published_memory_count: 9,
        today_memory_count: 1,
        recent_7d_memory_count: 3,
      }),
    }));
    await expect(loadPetMood({ fetchImpl, projectId: "project-alpha" })).resolves.toEqual({
      mood: "curious",
      label: "好奇",
      todayCount: 2,
      recent7dCount: 5,
      execution: { effect: "inactive", lease: "none" },
      pendingMemoryCandidateCount: 1,
      publishedMemoryCount: 9,
      todayMemoryCount: 1,
      recent7dMemoryCount: 3,
    });
    expect(fetchImpl).toHaveBeenCalledWith("/api/rebuild/pet/mood?project_id=project-alpha", expect.objectContaining({
      headers: { Accept: "application/json" },
    }));
  });

  it("rejects payload expansion and invalid counters instead of exposing unbounded fields", () => {
    const valid = {
      mood: "calm", execution: { effect: "inactive", lease: "none" }, today_activity_count: 0, recent_7d_activity_count: 0,
      pending_memory_candidate_count: 0, published_memory_count: 0, today_memory_count: 0, recent_7d_memory_count: 0,
    };
    expect(() => normalizePetMood({ ...valid, content: "secret" })).toThrow("invalid");
    expect(() => normalizePetMood({ ...valid, today_activity_count: -1 })).toThrow("invalid");
    expect(() => normalizePetMood({ ...valid, running_job_count: 1 })).toThrow("invalid");
    expect(() => normalizePetMood({ ...valid, execution: { effect: "active", lease: "expired" } })).toThrow("invalid");
    expect(() => normalizePetMood({ ...valid, execution: { effect: "inactive", lease: "unavailable" } })).toThrow("invalid");
    expect(normalizePetMood({ ...valid, execution: { effect: "unavailable", lease: "unavailable" } }).execution).toEqual({ effect: "unavailable", lease: "unavailable" });
  });
});

describe("subscribeCompanionState", () => {
  it("keeps only the bounded main-owned appearance projection", () => {
    let bridgeListener;
    const listener = vi.fn();
    const unsubscribe = vi.fn();
    const api = { subscribeCompanionState: vi.fn((next) => { bridgeListener = next; return unsubscribe; }) };
    expect(subscribeCompanionState(listener, api)).toBe(unsubscribe);

    bridgeListener({ state: "speaking", mood: "curious", animation_key: "talk", revision: 7 });
    bridgeListener({ state: "ready", mood: "calm", animation_key: "../../run", revision: 8 });
    bridgeListener({ state: "ready", mood: "calm", revision: 9, text: "secret" });

    expect(listener).toHaveBeenCalledTimes(1);
    expect(listener).toHaveBeenCalledWith({ state: "speaking", mood: "curious", animation_key: "talk", revision: 7 });
  });
});
