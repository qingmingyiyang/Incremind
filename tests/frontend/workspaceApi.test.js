import { afterEach, describe, expect, it, vi } from "vitest";
import { workspaceApi } from "@src/shared/api/workspaceApi";

afterEach(() => vi.unstubAllGlobals());

describe("workspace API", () => {
it("scopes list and search to a project", async () => {
    const fetch = vi.fn(async () => ({ ok: true, text: async () => '{"items":[]}' }));
    vi.stubGlobal("fetch", fetch);
    await workspaceApi.list("演示 项目");
    await workspaceApi.search("演示 项目", "材料 1");
    expect(fetch.mock.calls[0][0]).toContain("project_id=%E6%BC%94%E7%A4%BA+%E9%A1%B9%E7%9B%AE");
    expect(fetch.mock.calls[1][0]).toContain("q=%E6%9D%90%E6%96%99+1");
  });
it("sends uploads as multipart and reports server detail", async () => {
    const fetch = vi.fn(async () => ({ ok: false, status: 422, text: async () => '{"detail":"不支持此文件"}' }));
    vi.stubGlobal("fetch", fetch);
    await expect(workspaceApi.file("demo", new File(["a"], "a.txt"))).rejects.toThrow("不支持此文件 (422)");
    expect(fetch.mock.calls[0][1].body.get("project_id")).toBe("demo");
    expect(fetch.mock.calls[0][1].headers).toBeUndefined();
  });
it("scopes every item action to a non-default project", async () => {
    const fetch = vi.fn(async () => ({ ok: true, text: async () => '{}' }));
    vi.stubGlobal("fetch", fetch);
    const projectId = "private project";
    await workspaceApi.process(projectId, "item-1", true);
    await workspaceApi.saveDraft(projectId, "item-1", { title: "T", summary: "S" }, 3);
    await workspaceApi.confirm(projectId, "item-1", 4);
    const controller = new AbortController();
    await workspaceApi.source(projectId, "item-1", { signal: controller.signal });
    await workspaceApi.recognition(projectId, "item-1");
    await workspaceApi.retry(projectId, "item-1");
    for (const [url, options] of fetch.mock.calls) {
      if (url.includes("/source?")) expect(url).toContain("project_id=private+project");
      else expect(JSON.parse(options.body).project_id).toBe(projectId);
    }
    expect(JSON.parse(fetch.mock.calls[0][1].body).remote_processing_consent).toBe(true);
    expect(JSON.parse(fetch.mock.calls[1][1].body).expected_revision).toBe(3);
    expect(JSON.parse(fetch.mock.calls[2][1].body).expected_revision).toBe(4);
    expect(fetch.mock.calls[3][1].signal).toBe(controller.signal);
  });
it("preserves structured revision conflicts and refuses missing revisions", async () => {
    const current = { id: "item-1", revision: 5, draft: { title: "服务器标题" } };
    const fetch = vi.fn(async () => ({ ok: false, status: 409, text: async () => JSON.stringify({ detail: { code: "draft_revision_conflict", current } }) }));
    vi.stubGlobal("fetch", fetch);
    expect(() => workspaceApi.saveDraft("demo", "item-1", { title: "本地标题" }, 0)).toThrow("草稿版本不可用");
    expect(fetch).not.toHaveBeenCalled();
    await expect(workspaceApi.saveDraft("demo", "item-1", { title: "本地标题" }, 4)).rejects.toMatchObject({ status: 409, code: "draft_revision_conflict", current });
  });
it("previews a question and only sends remote consent with its preview id", async () => {
    const fetch = vi.fn(async () => ({ ok: true, text: async () => "{}" }));
    vi.stubGlobal("fetch", fetch);
    await workspaceApi.askPreview("private project", "问题");
    await workspaceApi.ask("private project", "问题", "preview-1", true);
    expect(fetch.mock.calls[0][0]).toContain("/api/workspace/v1/ask/preview");
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ project_id: "private project", question: "问题" });
    expect(fetch.mock.calls[1][0]).toContain("/api/workspace/v1/ask");
    expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({
      project_id: "private project", question: "问题", preview_id: "preview-1", remote_processing_consent: true,
    });
  });
});

it("sends legacy revision, nullable document basis, and exact confirmation Markdown", async () => {
  const fetch = vi.fn(async () => ({ ok: true, text: async () => "{}" }));
  vi.stubGlobal("fetch", fetch);
  await workspaceApi.saveLegacyReviewDraft("private", "source /", " draft ", 2, null);
  await workspaceApi.confirmLegacyReview("private", "source /", 3, { id: "doc", revision: 7 }, " exact\r\n稿 ");
  expect(fetch.mock.calls[0][0]).toContain("source%20%2F/draft");
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ project_id: "private", markdown: " draft ", expected_revision: 2, expected_document_basis: null });
  expect(JSON.parse(fetch.mock.calls[1][1].body)).toEqual({ project_id: "private", expected_revision: 3, expected_document_basis: { id: "doc", revision: 7 }, expected_markdown: " exact\r\n稿 " });
});

it.each([undefined, null, 0, 1.5, "2", Number.MAX_SAFE_INTEGER + 1])("rejects invalid legacy revision %s before fetching", (revision) => {
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  expect(() => workspaceApi.saveLegacyReviewDraft("p", "s", "draft", revision, null)).toThrow("草稿版本不可用");
  expect(() => workspaceApi.confirmLegacyReview("p", "s", revision, null, "draft")).toThrow("草稿版本不可用");
  expect(fetch).not.toHaveBeenCalled();
});

it.each([undefined, {}, { id: "", revision: 1 }, { id: 3, revision: 1 }, { id: "d", revision: 0 }])("rejects invalid legacy document basis %j", (basis) => {
  const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
  expect(() => workspaceApi.saveLegacyReviewDraft("p", "s", "draft", 1, basis)).toThrow();
  expect(() => workspaceApi.confirmLegacyReview("p", "s", 1, basis, "draft")).toThrow();
  expect(fetch).not.toHaveBeenCalled();
});

it("requires confirmation Markdown and preserves legacy structured conflicts", async () => {
  const current = { source_id: "s", revision: 4, document_basis: null, draft_markdown: "new" };
  const fetch = vi.fn(async () => ({ ok: false, status: 409, text: async () => JSON.stringify({ detail: { code: "draft_revision_conflict", current } }) }));
  vi.stubGlobal("fetch", fetch);
  expect(() => workspaceApi.confirmLegacyReview("p", "s", 1, null)).toThrow("确认正文不可用");
  expect(fetch).not.toHaveBeenCalled();
  await expect(workspaceApi.confirmLegacyReview("p", "s", 1, null, "old")).rejects.toMatchObject({ status: 409, code: "draft_revision_conflict", current });
});
