import { cleanup } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { recognitionApi } from "@src/shared/api/recognitionApi";

afterEach(cleanup);

it("keeps the proposal API payload scoped and revision-checked", async () => {
  let review;
  const fetchImpl = async (url, init) => {
    if (url.includes("/restructure-proposals/proposal%2Fone")) review = JSON.parse(init.body);
    return { ok: true, status: 200, json: async () => ({}) };
  };
  await recognitionApi.reviewRestructureProposal({ projectId: "project-one", proposalId: "proposal/one", expectedRevision: 3, decision: "rejected", fetchImpl });
  expect(review).toEqual({ project_id: "project-one", expected_revision: 3, decision: "rejected" });
});
