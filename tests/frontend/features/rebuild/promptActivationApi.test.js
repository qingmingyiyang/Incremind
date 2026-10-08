import { describe, expect, it, vi } from "vitest";

import {
  PROMPT_ACTIVATION_ENDPOINT,
  activatePromptUnit,
  loadPromptActivation,
  previewPromptActivation,
  rollbackPromptUnit,
} from "@src/features/rebuild/rebuildSettingsApi";


function response(payload = {}) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
  };
}


describe("prompt activation API", () => {
  it("sends explicit revision, preview, confirmation and reason contracts", async () => {
    const fetchImpl = vi.fn().mockResolvedValue(response({ status: "ready" }));

    await loadPromptActivation({ fetchImpl });
    await previewPromptActivation({
      unit_id: "intake.classification",
      expected_config_revision: 4,
      expected_activation_revision: 2,
    }, { fetchImpl });
    await activatePromptUnit({
      unit_id: "intake.classification",
      expected_config_revision: 4,
      expected_activation_revision: 2,
      preview_token: "preview-token",
      confirm: true,
      reason: "validated",
    }, { fetchImpl });
    await rollbackPromptUnit({
      unit_id: "intake.classification",
      expected_config_revision: 5,
      expected_activation_revision: 3,
      confirm: true,
      reason: "restore previous",
    }, { fetchImpl });

    expect(fetchImpl).toHaveBeenNthCalledWith(
      1,
      PROMPT_ACTIVATION_ENDPOINT,
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      2,
      `${PROMPT_ACTIVATION_ENDPOINT}/preview`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          unit_id: "intake.classification",
          expected_config_revision: 4,
          expected_activation_revision: 2,
        }),
      }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      3,
      `${PROMPT_ACTIVATION_ENDPOINT}/activate`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          unit_id: "intake.classification",
          expected_config_revision: 4,
          expected_activation_revision: 2,
          preview_token: "preview-token",
          confirm: true,
          reason: "validated",
        }),
      }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      4,
      `${PROMPT_ACTIVATION_ENDPOINT}/rollback`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          unit_id: "intake.classification",
          expected_config_revision: 5,
          expected_activation_revision: 3,
          confirm: true,
          reason: "restore previous",
        }),
      }),
    );
  });

  it("preserves actionable conflict details", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({ detail: "prompt activation revision conflict", actionable: true }),
    });

    await expect(previewPromptActivation({
      unit_id: "intake.classification",
      expected_config_revision: 4,
      expected_activation_revision: 1,
    }, { fetchImpl })).rejects.toMatchObject({
      status: 409,
      payload: { detail: "prompt activation revision conflict", actionable: true },
    });
  });
});
