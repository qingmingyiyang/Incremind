import { productFetch } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
const DEFAULT_BASE_PATH = "/api/rebuild/companion";

function companionUrl(path) {
  const electronBackend = globalThis.electronAPI?.backendBaseUrl;
  return typeof electronBackend === "string" && /^http:\/\/127\.0\.0\.1:\d+\/?$/.test(electronBackend)
    ? `${electronBackend.replace(/\/$/, "")}${path}`
    : path;
}

export class CompanionApiError extends Error {
  constructor(message, { kind = "failed", status = 0, code = "unknown", retryable = false } = {}) {
    super(message);
    this.name = "CompanionApiError";
    this.kind = kind;
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

export function createCompanionApi({ request = productFetch, basePath = DEFAULT_BASE_PATH } = {}) {
  if (typeof request !== "function") throw new TypeError("Companion API requires a request function");
  if (basePath !== DEFAULT_BASE_PATH) throw new TypeError("Companion API base path is fixed");

  async function requestJson(path, { method = "GET", body, signal, v2 = false } = {}) {
    if (!path.startsWith("/") || path.includes("..") || path.includes("://")) {
      throw new TypeError("Companion API path is invalid");
    }
    let response;
    try {
      response = await request(companionUrl(`${v2 ? "/api/v2" : basePath}${path}`), {
        method,
        credentials: "same-origin",
        cache: "no-store",
        headers: body === undefined ? { Accept: "application/json" } : {
          Accept: "application/json",
          "Content-Type": "application/json",
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal,
      });
    } catch (error) {
      if (error?.name === "AbortError") {
        throw new CompanionApiError("请求已取消", {
          kind: "cancelled",
          code: "cancelled",
          retryable: false,
        });
      }
      throw new CompanionApiError("陪伴服务暂时不可用", {
        kind: "failed",
        code: "network_error",
        retryable: true,
      });
    }

    let payload;
    try { payload = await readResponseJson(response); }
    catch (error) {
      throw new CompanionApiError(error.message, { status: response.status, code: error.code, retryable: true });
    }
    if (!response.ok) throw errorFromResponse(response.status, payload);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new CompanionApiError("陪伴服务返回了无法识别的结果", {
        kind: "failed",
        status: response.status,
        code: "invalid_response",
      });
    }
    return payload;
  }

  return Object.freeze({
    getWeekStats: ({ projectId, signal } = {}) => requestJson(`/stats/week${projectId ? `?project_id=${encodeURIComponent(projectId)}` : ""}`, { signal, v2: true }),
    listTodos: ({ projectId, includeDone = true, signal } = {}) => {
      const query = new URLSearchParams({ include_done: String(includeDone) });
      if (projectId) query.set("project_id", projectId);
      return requestJson(`/todos?${query}`, { signal, v2: true });
    },
    doneTodo: ({ id, expectedRevision = null, signal }) => requestJson(`/todos/${encodeURIComponent(id)}/done`, { method: "POST", body: { expected_revision: expectedRevision }, signal, v2: true }),
    undoTodo: ({ id, expectedRevision = null, signal }) => requestJson(`/todos/${encodeURIComponent(id)}/undo`, { method: "POST", body: { expected_revision: expectedRevision }, signal, v2: true }),
    getStatus: ({ signal } = {}) => requestJson("/status", { signal }),
    getLifeState: ({ signal } = {}) => requestJson("/state", { signal }),
    dailyCheckIn: ({ signal } = {}) => requestJson("/state/daily-check-in", { method: "POST", body: {}, signal }),
      getCommerce: ({ signal } = {}) => requestJson("/commerce", { signal }),
      purchaseItem: ({ offerId, idempotencyKey, signal }) => requestJson("/commerce/purchase", { method: "POST", body: { offer_id: offerId, idempotency_key: idempotencyKey }, signal }),
      feedItem: ({ itemId, idempotencyKey, signal }) => requestJson("/commerce/feed", { method: "POST", body: { item_id: itemId, idempotency_key: idempotencyKey }, signal }),
      getAppearance: ({ signal } = {}) => requestJson("/appearance", { signal }),
      equipAppearance: ({ slot, selectionId, idempotencyKey, signal }) => requestJson("/appearance/equip", { method: "POST", body: { slot, selection_id: selectionId, idempotency_key: idempotencyKey }, signal }),
      markStorySeen: ({ chapterId, signal }) => requestJson(`/appearance/stories/${encodeURIComponent(chapterId)}/seen`, { method: "POST", body: {}, signal }),
    getFocus: ({ signal } = {}) => requestJson("/focus", { signal }),
    startFocus: ({ durationMinutes, supervisionEnabled, workProcesses, distractingProcesses, signal }) => requestJson("/focus/start", { method: "POST", body: { duration_minutes: durationMinutes, supervision_enabled: supervisionEnabled, work_processes: workProcesses, distracting_processes: distractingProcesses }, signal }),
    actOnFocus: ({ sessionId, action, expectedRevision, signal }) => requestJson(`/focus/${encodeURIComponent(sessionId)}/action`, { method: "POST", body: { action, expected_revision: expectedRevision }, signal }),
    observeFocus: ({ locked = false, sleeping = false, gameQuiet = false, signal } = {}) => requestJson("/focus/observe", { method: "POST", body: { locked, sleeping, game_quiet: gameQuiet }, signal }),
    getAmbient: ({ signal } = {}) => requestJson("/ambient", { signal }),
    getSensors: ({ signal } = {}) => requestJson("/sensors", { signal }),
    getWeather: ({ signal } = {}) => requestJson("/weather", { signal }),
    saveWeather: ({ enabled, locationName, latitude, longitude, noncommercialAcknowledged, expectedRevision, signal }) => requestJson("/weather/settings", {
      method: "PUT",
      body: { enabled, location_name: locationName, latitude, longitude, noncommercial_acknowledged: noncommercialAcknowledged, expected_revision: expectedRevision },
      signal,
    }),
    refreshWeather: ({ signal } = {}) => requestJson("/weather/refresh", { method: "POST", body: {}, signal }),
    getMediaSession: ({ signal } = {}) => requestJson("/media-session", { signal }),
    saveMediaSession: ({ enabled, modelCommentaryEnabled, expectedRevision, signal }) => requestJson("/media-session/settings", {
      method: "PUT",
      body: { enabled, model_commentary_enabled: modelCommentaryEnabled, expected_revision: expectedRevision },
      signal,
    }),
    saveSensors: ({ enabled, networkEnabled, healthOrigin, gameEnabled, gameProcesses, gameBehavior, expectedRevision, signal }) => requestJson("/sensors/settings", {
      method: "PUT",
      body: {
        enabled,
        network_enabled: networkEnabled,
        health_origin: healthOrigin || null,
        game_enabled: gameEnabled,
        game_processes: gameProcesses,
        game_behavior: gameBehavior,
        expected_revision: expectedRevision,
      },
      signal,
    }),
    saveAmbient: ({ enabled, intervalMinutes, idleEnabled, idleMinutes, expectedRevision, signal }) => requestJson("/ambient/settings", { method: "PUT", body: { enabled, interval_minutes: intervalMinutes, idle_enabled: idleEnabled, idle_minutes: idleMinutes, expected_revision: expectedRevision }, signal }),
    offerAmbient: ({ requireDue = false, quiet = false, game = false, sleeping = false, signal } = {}) => requestJson("/ambient/offer", { method: "POST", body: { require_due: requireDue, quiet, game, sleeping }, signal }),
    chooseAmbient: ({ eventId, optionId, expectedRevision, signal }) => requestJson(`/ambient/events/${encodeURIComponent(eventId)}/choose`, { method: "POST", body: { option_id: optionId, expected_revision: expectedRevision }, signal }),
    previewDiary: ({ timezoneOffsetMinutes, signal }) => requestJson(`/diary/preview?timezone_offset_minutes=${encodeURIComponent(timezoneOffsetMinutes)}`, { signal }),
    listDiaries: ({ localDate = null, signal } = {}) => requestJson(`/diary${localDate ? `?local_date=${encodeURIComponent(localDate)}` : ""}`, { signal }),
    generateDiary: ({ requestId, timezoneOffsetMinutes, previewFingerprint, confirmEgress, signal }) => requestJson("/diary/generate", { method: "POST", body: { request_id: requestId, timezone_offset_minutes: timezoneOffsetMinutes, preview_fingerprint: previewFingerprint, confirm_egress: confirmEgress }, signal }),
    editDiary: ({ diaryId, content, expectedRevision, signal }) => requestJson(`/diary/${encodeURIComponent(diaryId)}/edit`, { method: "POST", body: { content, expected_revision: expectedRevision }, signal }),
    deleteDiaryEvent: ({ eventId, signal }) => requestJson(`/diary/events/${encodeURIComponent(eventId)}`, { method: "DELETE", signal }),
    getSettings: ({ signal } = {}) => requestJson("/settings", { signal }),
    saveSettings: ({ expectedRevision, settings, signal }) => requestJson("/settings", {
      method: "PUT",
      body: { expected_revision: expectedRevision, settings },
      signal,
    }),
    listReminders: ({ signal } = {}) => requestJson("/reminders", { signal }),
    createReminder: ({ reminder, signal }) => requestJson("/reminders", { method: "POST", body: reminder, signal }),
    cancelReminder: ({ reminderId, expectedRevision, signal }) => requestJson(`/reminders/${encodeURIComponent(reminderId)}/cancel`, {
      method: "POST", body: { expected_revision: expectedRevision }, signal,
    }),
    listHistory: ({ cursor = null, limit = 50, projectId = null, signal } = {}) => {
      const query = new URLSearchParams({ limit: String(limit) });
      if (cursor) query.set("cursor", cursor);
      if (projectId) query.set("project_id", projectId);
      return requestJson(`/history?${query.toString()}`, { signal });
    },
    deleteMessage: ({ messageId, signal }) => requestJson(`/messages/${encodeURIComponent(messageId)}`, {
      method: "DELETE",
      signal,
    }),
    proposeMemoryCandidate: ({ messageId, signal }) => requestJson(`/messages/${encodeURIComponent(messageId)}/memory-candidate`, {
      method: "POST",
      body: {},
      signal,
    }),
    listDistillableEpisodes: ({ projectId, currentSessionId, signal }) => requestJson(
      `/memory-distillations/episodes?project_id=${encodeURIComponent(projectId)}&current_session_id=${encodeURIComponent(currentSessionId)}`, { signal },
    ),
    distillEpisode: ({ projectId, currentSessionId, episodeId, commandId, signal }) => requestJson("/memory-distillations", {
      method: "POST", body: { project_id: projectId, current_session_id: currentSessionId, episode_id: episodeId, command_id: commandId }, signal,
    }),
    getProfile: ({ signal } = {}) => requestJson("/profile", { signal }),
    saveProfile: ({ expectedRevision, profile, signal }) => requestJson("/profile", {
      method: "PUT",
      body: { expected_revision: expectedRevision, profile },
      signal,
    }),
    getCharacterPrompt: ({ signal } = {}) => requestJson("/character-prompt", { signal }),
    saveCharacterPrompt: ({ expectedBindingRevision, expectedActivationRevision, content, signal }) =>
      requestJson("/character-prompt", {
        method: "PUT",
        body: {
          expected_binding_revision: expectedBindingRevision,
          expected_activation_revision: expectedActivationRevision,
          content,
        },
        signal,
      }),
    restoreCharacterPrompt: ({ expectedBindingRevision, expectedActivationRevision, signal }) =>
      requestJson("/character-prompt/restore", {
        method: "POST",
        body: {
          expected_binding_revision: expectedBindingRevision,
          expected_activation_revision: expectedActivationRevision,
        },
        signal,
      }),
    getManual: ({ signal } = {}) => requestJson("/manual", { signal }),
    listNotes: ({ signal } = {}) => requestJson("/notes", { signal }),
    createNote: ({ content, signal }) => requestJson("/notes", {
      method: "POST",
      body: { content },
      signal,
    }),
    sendChat: ({
      requestId, sessionId = null, text, memoryId = null, projectId = null, signal,
    }) => requestJson("/chat", {
      method: "POST",
      body: {
        request_id: requestId,
        ...(sessionId ? { session_id: sessionId } : {}),
        ...(memoryId ? { memory_id: memoryId } : {}),
        ...(projectId ? { project_id: projectId } : {}),
        text,
      },
      signal,
    }),
    listChatMessages: ({ sessionId, limit = 50, signal }) => {
      const query = new URLSearchParams({ session_id: sessionId, limit: String(limit) });
      return requestJson(`/chat/messages?${query.toString()}`, { signal });
    },
  });
}

function errorFromResponse(status, payload) {
  const detail = payload?.error && typeof payload.error === "object" ? payload.error : {};
  const message = typeof detail.message === "string" && detail.message.trim()
    ? detail.message.trim().slice(0, 240)
    : "陪伴服务未完成请求";
  const code = typeof detail.code === "string" ? detail.code : `http_${status}`;
  if (status === 409) {
    return new CompanionApiError(message, { kind: "conflict", status, code, retryable: false });
  }
  if (status === 403 || code === "read_only") {
    return new CompanionApiError(message, { kind: "read_only", status, code, retryable: false });
  }
  if (status === 404) {
    return new CompanionApiError(message, { kind: "empty", status, code, retryable: false });
  }
  return new CompanionApiError(message, {
    kind: "failed",
    status,
    code,
    retryable: status === 408 || status === 429 || status >= 500,
  });
}
