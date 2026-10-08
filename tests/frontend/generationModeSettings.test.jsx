import { cleanup } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { recognitionApi } from "@src/shared/api/recognitionApi";

afterEach(cleanup);

it("sends only mode choices and expected revision to the dedicated endpoint", async () => {
  let request;
  await recognitionApi.saveGenerationMode({ mode: "local", localEnabled: true, localBaseUrl: "http://127.0.0.1:8001/local-model/v1", expectedRevision: 6,
    fetchImpl: async (url, init) => { request = { url, init }; return { ok: true, status: 200, json: async () => ({ mode: "local" }) }; },
  });
  expect(request.url).toBe("/api/recognition/settings/generation-mode");
  expect(request.init.method).toBe("PUT");
  expect(JSON.parse(request.init.body)).toEqual({ mode: "local", local_enabled: true, local_base_url: "http://127.0.0.1:8001/local-model/v1", expected_revision: 6 });
});
