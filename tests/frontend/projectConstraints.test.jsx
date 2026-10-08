import { cleanup } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { recognitionApi } from "@src/shared/api/recognitionApi";

afterEach(cleanup);

it("sends scope, expected revision and explicit time bounds to the authority", async () => {
  let body;
  await recognitionApi.saveConstraint({ projectId: "one", constraintId: "constraint-1", expectedRevision: 3,
    content: "要求", enabled: true, validFrom: "2026-09-16T00:00:00Z", validUntil: null,
    fetchImpl: async (_url, init) => { body = JSON.parse(init.body); return { ok: true, status: 200, json: async () => ({}) }; },
  });
  expect(body).toEqual({ project_id: "one", expected_revision: 3, content: "要求", enabled: true, valid_from: "2026-09-16T00:00:00Z", valid_until: null });
});
