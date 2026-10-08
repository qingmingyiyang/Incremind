import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import VersionHistory from "../../src/frontend/src/features/library/VersionHistory";

afterEach(cleanup);

it("distinguishes imported source history from the local baseline", async () => {
  const api = { loadRecognitionVersions: vi.fn().mockResolvedValue({
    versions: [{ id: "target~v1", version: 1, action: "import", snapshot: { content: "目标正文" } }],
    imported_versions: [{ origin_bundle_id: "bundle", source_scope: { project_id: "原项目" },
      source_record: { id: "source~v3", payload: { recognition_id: "source", version: 3,
        recognition_revision: 5, snapshot: { content: "原项目正文", conditions: ["原条件"] } } } }],
  }) };
  render(<VersionHistory projectId="target" recognitionId="target" api={api} />);
  expect(await screen.findByText(/来源项目 原项目 · 原版本 3/)).toBeInTheDocument();
  expect(screen.getByText(/版本 1 · 导入基线/)).toBeInTheDocument();
  expect(screen.getByText("原项目正文")).toBeInTheDocument();
});

it("discards delayed history from the previous project", async () => {
  let resolveOld;
  const api = { loadRecognitionVersions: vi.fn().mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; }))
    .mockResolvedValue({ versions: [] }) };
  const { rerender } = render(<VersionHistory projectId="old" recognitionId="r" api={api} />);
  rerender(<VersionHistory projectId="new" recognitionId="r" api={api} />);
  resolveOld({ versions: [{ id: "old", version: 1, snapshot: { content: "旧项目私有内容" } }] });
  await waitFor(() => expect(api.loadRecognitionVersions).toHaveBeenCalledTimes(2));
  expect(screen.queryByText("旧项目私有内容")).not.toBeInTheDocument();
});
