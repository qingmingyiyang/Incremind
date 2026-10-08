import { describe, expect, it } from "vitest";
import { recognitionApi } from "@src/shared/api/recognitionApi";

describe("recognition workbench contracts", () => {
it.each([[502, "连接未完成 · 重试"], [422, "模型输出未完成，未保存为成果或候选"]])("shows an actionable HTTP %s error without JSON syntax", async (status, message) => {
    await expect(recognitionApi.testSettings({ purpose: "generation", fetchImpl: async () => ({
      ok: false, status, text: async () => JSON.stringify({ detail: "model_output_incomplete" }),
    }) })).rejects.toThrow(message);
  });
it("retains a completed task only through an explicit project-scoped request", async () => {
    const calls = [];
    await recognitionApi.retainTaskExperience({ taskId: "task-7", projectId: "verification", fetchImpl: async (url, init) => {
      calls.push({ url, init });
      return { ok: true, status: 200, json: async () => ({ experience_id: "experience-7" }) };
    } });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/api/recognition/tasks/task-7/experience");
    expect(calls[0].init.method).toBe("POST");
    expect(JSON.parse(calls[0].init.body)).toEqual({ project_id: "verification" });
  });
it("uses the body-free source-policy read and CAS write contracts", async () => {
    const calls = [];
    const fetchImpl = async (url, init) => { calls.push({ url, init }); return { ok: true, status: 200, json: async () => ({ nodes: [] }) }; };
    await recognitionApi.loadSourcePolicy({ projectId: "project-a", sourceType: "experience", sourceId: "experience-1", revision: 2, fetchImpl });
    await recognitionApi.saveSourcePolicy({ projectId: "project-a", sourceType: "experience", sourceId: "experience-1", expectedSourceRevision: 2, expectedPolicyRevision: 4, allowedPurposes: ["generation", "embedding", "rerank"], fetchImpl });
    expect(calls[0].url).toBe("/api/recognition/source-policies/experience/experience-1?project_id=project-a&revision=2");
    expect(calls[1].url).toBe("/api/recognition/source-policies/experience/experience-1");
    expect(JSON.parse(calls[1].init.body)).toEqual({ project_id: "project-a", expected_source_revision: 2, expected_policy_revision: 4, allowed_purposes: ["generation", "embedding", "rerank"] });
  });
it("requests graph pages from the server and preserves the explicit focus", async () => {
    const calls = [];
    await recognitionApi.loadGraph({ projectId: "project-a", focus: "recognition-3", offset: 40, limit: 40, fetchImpl: async (url) => {
      calls.push(url);
      return { ok: true, status: 200, json: async () => ({ nodes: [], edges: [], total: 80, offset: 40, limit: 40, next_offset: null }) };
    } });
    expect(calls).toEqual(["/api/recognition/graph?project_id=project-a&offset=40&limit=40&focus=recognition-3"]);
  });
it("uses the scoped graph-view CAS contract without serializing memory content", async () => {
    const calls = [];
    await recognitionApi.saveGraphView({ projectId: "project-a", viewId: "view/a", expectedRevision: 3,
      nodeIds: ["recognition-1"], positions: { "recognition-1": { x: 80, y: 60 } }, collapsedIds: [], hiddenIds: [], selectedIds: ["recognition-1"], focusId: "recognition-1",
      fetchImpl: async (url, init) => { calls.push({ url, init }); return { ok: true, status: 200, json: async () => ({ view: {} }) }; },
    });
    expect(calls[0].url).toBe("/api/recognition/graph-views/view%2Fa");
    expect(JSON.parse(calls[0].init.body)).toEqual({ project_id: "project-a", expected_revision: 3, node_ids: ["recognition-1"], positions: { "recognition-1": { x: 80, y: 60 } }, collapsed_ids: [], hidden_ids: [], selected_ids: ["recognition-1"], focus_id: "recognition-1" });
  });
it("submits relation proposals for explicit human review", async () => {
    let payload;
    await recognitionApi.proposeRelation({ projectId: "project-a", fromId: "recognition-1", toId: "recognition-2", relation: "supports", evidence: "两条认识引用同一经历", fetchImpl: async (_url, init) => {
      payload = JSON.parse(init.body);
      return { ok: true, status: 200, json: async () => ({ id: "proposal-1" }) };
    } });
    expect(payload).toEqual({ project_id: "project-a", from_id: "recognition-1", to_id: "recognition-2", relation: "supports", evidence: "两条认识引用同一经历" });
  });
it("includes project and expected revision when approving a candidate", async () => {
    let payload;
    await recognitionApi.reviewCandidate({ candidateId: "candidate-1", projectId: "project-a", expectedRevision: 4, decision: "approve", fetchImpl: async (_url, init) => {
      payload = JSON.parse(init.body);
      return { ok: true, status: 200, json: async () => ({}) };
    } });
    expect(payload).toMatchObject({ project_id: "project-a", expected_revision: 4, decision: "approve" });
  });
it("keeps settings revision and explicit remote consent in every model update", async () => {
    let payload;
    await recognitionApi.saveSettings({
      purpose: "rerank", baseUrl: "https://example.invalid/v1", model: "reranker", apiKey: "",
      allowRemote: true, expectedRevision: 6,
      fetchImpl: async (_url, init) => {
        payload = JSON.parse(init.body);
        return { ok: true, status: 200, json: async () => ({}) };
      },
    });
    expect(payload).toMatchObject({ purpose: "rerank", allow_remote: true, expected_revision: 6, clear_api_key: false });
    expect(payload.api_key).toBe("");
  });
it("keeps every selected recognition revision when proposing a merge", async () => {
    let payload;
    await recognitionApi.mergeRecognitions({
      projectId: "project-a", expectedRevisions: { "recognition-1": 2, "recognition-2": 5 },
      content: "合并后的认识", conditions: "当前本地阶段",
      fetchImpl: async (_url, init) => {
        payload = JSON.parse(init.body);
        return { ok: true, status: 200, json: async () => ({}) };
      },
    });
    expect(payload).toMatchObject({ project_id: "project-a", expected_revisions: { "recognition-1": 2, "recognition-2": 5 }, conditions: "当前本地阶段" });
  });
});
