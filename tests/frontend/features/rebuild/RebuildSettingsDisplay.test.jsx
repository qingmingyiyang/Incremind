import { describe, expect, it, vi } from "vitest";
import { AUTO_MEMORY_PUBLICATION_SETTINGS_ENDPOINT, DEVELOPER_STUDIO_CONFIG_ENDPOINT, FOUR_LAYER_PROVIDER_STATUS_ENDPOINT, LOCAL_ASR_PROVIDER_SETTINGS_ENDPOINT, LOCAL_DOCUMENT_TEXT_EXTRACTOR_SETTINGS_ENDPOINT, LOCAL_OCR_PROVIDER_SETTINGS_ENDPOINT, MODEL_ROUTE_RUNTIME_ENDPOINT, MODEL_ROUTES_ENDPOINT, PROVIDERS_ENDPOINT, activateProvider, deleteProvider, activateModelRouteRuntime, createProvider, deleteProviderSecret, deactivateModelRouteRuntime, grantProviderEgressConsent, loadDeveloperStudioConfig, loadProviderDisconnectPreview, listProviders, loadAutoMemoryPublicationSettings, loadLocalAsrProviderSettings, loadLocalDocumentTextExtractorSettings, loadLocalOcrProviderSettings, loadModelRouteRuntime, listLocalAsrModels, downloadLocalAsrModel, revokeProviderEgressConsent, previewModelRouteRuntime, testProvider, saveDeveloperStudioConfig, saveAutoMemoryPublicationSettings, saveLocalAsrProviderSettings, saveLocalDocumentTextExtractorSettings, saveBuiltinWindowsOcrSettings, updateProvider, updateModelRoute, updateModelRouteBatch } from "@src/features/rebuild/rebuildSettingsApi";

describe("RebuildSettingsDisplay", () => {
it("serializes the built-in Windows OCR sentinel at the settings API boundary", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ status: "ready", enabled: true }),
    });

    await saveBuiltinWindowsOcrSettings({ enabled: true }, { fetchImpl });
    await loadLocalOcrProviderSettings({ fetchImpl });

    expect(LOCAL_OCR_PROVIDER_SETTINGS_ENDPOINT).toBe("/api/rebuild/settings/local-ocr-provider");
    expect(fetchImpl).toHaveBeenNthCalledWith(1, LOCAL_OCR_PROVIDER_SETTINGS_ENDPOINT, expect.objectContaining({
      method: "PUT",
      body: JSON.stringify({
        enabled: true,
        provider_name: "builtin-windows-ocr",
        command: ["builtin:windows-ocr"],
        confirm_enable: true,
      }),
    }));
    expect(fetchImpl).toHaveBeenNthCalledWith(2, LOCAL_OCR_PROVIDER_SETTINGS_ENDPOINT, expect.objectContaining({
      headers: expect.objectContaining({ Accept: "application/json" }),
    }));
  });
it("serializes the built-in document extractor sentinel at the settings API boundary", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ status: "ready", enabled: true }),
    });

    await saveLocalDocumentTextExtractorSettings({ enabled: true }, { fetchImpl });
    await loadLocalDocumentTextExtractorSettings({ fetchImpl });

    expect(LOCAL_DOCUMENT_TEXT_EXTRACTOR_SETTINGS_ENDPOINT).toBe("/api/rebuild/settings/local-document-text-extractor");
    expect(fetchImpl).toHaveBeenNthCalledWith(1, LOCAL_DOCUMENT_TEXT_EXTRACTOR_SETTINGS_ENDPOINT, expect.objectContaining({
      method: "PUT",
      body: JSON.stringify({
        enabled: true,
        provider_name: "builtin-document-text",
        command: ["builtin:document-text"],
        confirm_enable: true,
      }),
    }));
    expect(fetchImpl).toHaveBeenNthCalledWith(2, LOCAL_DOCUMENT_TEXT_EXTRACTOR_SETTINGS_ENDPOINT, expect.objectContaining({
      headers: expect.objectContaining({ Accept: "application/json" }),
    }));
  });
});

describe("Rebuild settings API helpers", () => {
it("uses the registry batch and explicit runtime lifecycle endpoints", async () => {
    const fetchImpl = vi.fn(() => Promise.resolve({ ok: true, status: 200, json: async () => ({ status: "ok" }) }));
    const assignment = {
      route_key: "intake.classification",
      provider_id: "deepseek",
      model_name: "deepseek-v4-flash",
      adapter_kind: "openai-compatible",
      enabled: true,
      reason: "three tier basic model plan",
    };
    await updateModelRouteBatch({ expected_registry_revision: 2, assignments: [assignment] }, { fetchImpl });
    await updateModelRoute("intake.classification", { ...assignment, expected_registry_revision: 3 }, { fetchImpl });
    await loadModelRouteRuntime({ fetchImpl });
    await previewModelRouteRuntime(["intake.classification"], { fetchImpl });
    await activateModelRouteRuntime({ shadow_token: "shadow", route_keys: ["intake.classification"], expected_runtime_revision: 4 }, { fetchImpl });
    await deactivateModelRouteRuntime(5, { fetchImpl });

    expect(fetchImpl).toHaveBeenNthCalledWith(1, `${MODEL_ROUTES_ENDPOINT}/batch`, expect.objectContaining({ method: "PUT" }));
    expect(fetchImpl).toHaveBeenNthCalledWith(2, `${MODEL_ROUTES_ENDPOINT}/intake.classification`, expect.objectContaining({ method: "PUT" }));
    expect(fetchImpl).toHaveBeenNthCalledWith(3, MODEL_ROUTE_RUNTIME_ENDPOINT, expect.any(Object));
    expect(fetchImpl).toHaveBeenNthCalledWith(4, `${MODEL_ROUTE_RUNTIME_ENDPOINT}/preview`, expect.objectContaining({ method: "POST" }));
    expect(fetchImpl).toHaveBeenNthCalledWith(5, `${MODEL_ROUTE_RUNTIME_ENDPOINT}/activate`, expect.objectContaining({ body: expect.stringContaining('"confirm":true') }));
    expect(fetchImpl).toHaveBeenNthCalledWith(6, `${MODEL_ROUTE_RUNTIME_ENDPOINT}/deactivate`, expect.objectContaining({ body: expect.stringContaining('"confirm":true') }));
  });
it("surfaces a redacted backend reason for provider test failures", async () => {
    const fetchImpl = vi.fn(() => Promise.resolve({
      ok: false,
      status: 403,
      json: async () => ({ detail: "模型外发未授权：credential sk-example-secret-value rejected" }),
    }));

    await expect(testProvider("deepseek", {
      llm_provider: "deepseek",
      base_url: "https://api.deepseek.com",
      model: "deepseek-v4-flash",
    }, { fetchImpl })).rejects.toThrow("模型外发未授权：credential [已隐藏密钥] rejected");
  });
it("uses narrow OS Provider endpoints without returning key material", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ status: "ready" }),
      }),
    );

    await listProviders({ fetchImpl });
    await createProvider({ provider_id: "deepseek" }, { fetchImpl });
    await updateProvider("deepseek", { model: "deepseek-chat" }, { fetchImpl });
    await activateProvider("deepseek", { fetchImpl });
    await grantProviderEgressConsent("deepseek", "egress-deepseek-v1", { fetchImpl });
    await revokeProviderEgressConsent("deepseek", { fetchImpl });
    await deleteProviderSecret("deepseek", { fetchImpl });
    await loadAutoMemoryPublicationSettings({ fetchImpl });
    await loadLocalAsrProviderSettings({ fetchImpl });
    await listLocalAsrModels({ fetchImpl });
    await downloadLocalAsrModel("large-v3-turbo", { fetchImpl });
    await saveAutoMemoryPublicationSettings({
      enabled: true,
      allowed_layers: ["atom", "series_memory"],
    }, { fetchImpl });
    await saveLocalAsrProviderSettings({
      enabled: true,
      provider_name: "local-command-asr",
      command: ["python", "local_asr.py", "--model", "{model_name}", "{audio_path}"],
      model_profile: "large-v3-turbo",
      model_name: "large-v3-turbo",
    }, { fetchImpl });
    await loadDeveloperStudioConfig({ fetchImpl });
    await saveDeveloperStudioConfig({
      expected_revision: 3,
      model_profiles: [{ id: "mp-intake-main" }],
      task_model_map: { intakeMain: "mp-intake-main" },
      prompts: [{ id: "pt-input-understanding", content: "classify" }],
      skills: [],
      workflow_steps: [],
      snapshots: [],
    }, { fetchImpl });
    await loadProviderDisconnectPreview("deepseek", { fetchImpl });
    await listProviders({ fetchImpl });

    expect(fetchImpl).toHaveBeenNthCalledWith(
      1,
      PROVIDERS_ENDPOINT,
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      5,
      `${PROVIDERS_ENDPOINT}/deepseek/egress-consent`,
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ manifest_id: "egress-deepseek-v1", confirm: true }),
      }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      6,
      `${PROVIDERS_ENDPOINT}/deepseek/egress-consent`,
      expect.objectContaining({ method: "DELETE" }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      8,
      AUTO_MEMORY_PUBLICATION_SETTINGS_ENDPOINT,
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      9,
      LOCAL_ASR_PROVIDER_SETTINGS_ENDPOINT,
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      10,
      "/api/asr/faster-whisper/models",
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      11,
      "/api/asr/faster-whisper/models/large-v3-turbo/download",
      expect.objectContaining({ method: "POST" }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      12,
      AUTO_MEMORY_PUBLICATION_SETTINGS_ENDPOINT,
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          enabled: true,
          confirm_enable: true,
          allowed_layers: ["atom", "series_memory"],
        }),
      }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      13,
      LOCAL_ASR_PROVIDER_SETTINGS_ENDPOINT,
      expect.objectContaining({
        method: "PUT",
        body: expect.stringContaining("large-v3-turbo"),
      }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      14,
      DEVELOPER_STUDIO_CONFIG_ENDPOINT,
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
    expect(fetchImpl).toHaveBeenNthCalledWith(
      15,
      DEVELOPER_STUDIO_CONFIG_ENDPOINT,
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({
          expected_revision: 3,
          model_profiles: [{ id: "mp-intake-main" }],
          task_model_map: { intakeMain: "mp-intake-main" },
          prompts: [{ id: "pt-input-understanding", content: "classify" }],
          skills: [],
          workflow_steps: [],
          snapshots: [],
        }),
      }),
    );
    expect(fetchImpl.mock.calls[14][1].body).not.toContain("api_key");
    expect(fetchImpl.mock.calls[14][1].body).not.toContain("cookie");
    expect(fetchImpl).toHaveBeenCalledWith(
      `${PROVIDERS_ENDPOINT}/deepseek/disconnect-preview`,
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
    expect(fetchImpl).not.toHaveBeenCalledWith(
      expect.stringContaining("legacy-intake"),
      expect.anything(),
    );
  });
it("loads four-layer Provider status from the OS settings API", async () => {
    const fetchImpl = vi.fn(() =>
      Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ status: "ready", key_material_returned: false }),
      }),
    );

    const result = await import("@src/features/rebuild/rebuildSettingsApi").then((api) =>
      api.loadFourLayerProviderStatus({ fetchImpl }),
    );

    expect(result.key_material_returned).toBe(false);
    expect(fetchImpl).toHaveBeenCalledWith(
      FOUR_LAYER_PROVIDER_STATUS_ENDPOINT,
      expect.objectContaining({ headers: { Accept: "application/json" } }),
    );
  });
it("deletes a Provider through the narrow registry endpoint", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({ ok: true, status: 204, json: async () => null });
    await deleteProvider("unused-provider", { fetchImpl });
    expect(fetchImpl).toHaveBeenCalledWith(
      `${PROVIDERS_ENDPOINT}/unused-provider`,
      expect.objectContaining({ method: "DELETE" }),
    );
  });
it("preserves Developer Studio conflict status and payload for the UI", async () => {
    const fetchImpl = vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({
        status: "rejected",
        error: "developer studio config revision conflict",
      }),
    });

    await expect(saveDeveloperStudioConfig({ expected_revision: 3 }, { fetchImpl })).rejects.toMatchObject({
      message: "Developer Studio 配置暂时无法同步",
      status: 409,
      payload: {
        status: "rejected",
        error: "developer studio config revision conflict",
      },
    });
  });
});
