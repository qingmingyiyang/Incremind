import { productFetch as fetch } from '../../shared/api/deviceTransport';
// petMoodStore.js — 桌面宠物 mood 状态管理
// 默认 mood 用于非Electron预览；正式pet由Electron main通过IPC投影脱敏状态。
//
// mood 与 MemoryPersonaCard 的 POSE_BY_MOOD 对齐，
// 确保宠物窗和首页小熊卡片使用同一套 pose 素材。

// ── Mood 类型枚举（可扩展）──
// 与 MemoryPersonaCard.jsx 的 POSE_BY_MOOD key 对齐
export const PET_MOOD = Object.freeze({
  CALM: "calm",
  FOCUSED: "focused",
  CURIOUS: "curious",
  ANALYZING: "analyzing",
  IDLE: "idle",
  // 后续扩展预留：
  // RUNNING: "running",        // 正在处理记忆
  // CELEBRATING: "celebrating", // 有新记忆入库
  // TIRED: "tired",            // 长时间无新记忆
});

const MASCOT_ASSET_ROOT = `${import.meta.env.BASE_URL}mascots/`;

// ── Mood → pose 素材映射 ──
// 与 MemoryPersonaCard.jsx 的 BEAR_CARD_POSES 对齐
const PET_POSE_ASSETS = Object.freeze({
  [PET_MOOD.CALM]: `${MASCOT_ASSET_ROOT}bear_memory_card_04_right_relaxed_lean.webp`,
  [PET_MOOD.FOCUSED]: `${MASCOT_ASSET_ROOT}bear_memory_card_01_left_arms_crossed.webp`,
  [PET_MOOD.CURIOUS]: `${MASCOT_ASSET_ROOT}bear_memory_card_02_right_arms_crossed.webp`,
  [PET_MOOD.ANALYZING]: `${MASCOT_ASSET_ROOT}bear_memory_card_03_left_adjust_glasses.webp`,
  [PET_MOOD.IDLE]: `${MASCOT_ASSET_ROOT}bear_memory_card_04_right_relaxed_lean.webp`,
});

// ── Mood → 用户化标签 ──
const PET_MOOD_LABELS = Object.freeze({
  [PET_MOOD.CALM]: "安静偏亮",
  [PET_MOOD.FOCUSED]: "专注",
  [PET_MOOD.CURIOUS]: "好奇",
  [PET_MOOD.ANALYZING]: "正在识别",
  [PET_MOOD.IDLE]: "等待中",
});

// ── 默认 mood ──
const DEFAULT_MOOD = PET_MOOD.CALM;
export const PET_MOOD_ENDPOINT = "/api/rebuild/pet/mood";

function workerUrl(endpoint) {
  const electronBackend = globalThis.electronAPI?.backendBaseUrl;
  if (electronBackend) return `${electronBackend.replace(/\/$/, "")}${endpoint}`;

  return endpoint;
}

/** 返回尚未完成后端同步时使用的本地安全外观。 */
export function getPetMood(_options = {}) {
  const mood = DEFAULT_MOOD;
  return {
    mood,
    label: PET_MOOD_LABELS[mood] || "安静偏亮",
    poseSrc: PET_POSE_ASSETS[mood] || PET_POSE_ASSETS[DEFAULT_MOOD],
  };
}

/**
 * 从 mood 字符串获取 pose 素材路径。
 * 供组件在 mood 变化时切换图片。
 */
export function poseSrcForMood(mood) {
  return PET_POSE_ASSETS[mood] || PET_POSE_ASSETS[DEFAULT_MOOD];
}

/**
 * 从 mood 字符串获取用户化标签。
 */
export function labelForMood(mood) {
  return PET_MOOD_LABELS[mood] || PET_MOOD_LABELS[DEFAULT_MOOD];
}

export async function loadPetMood({ fetchImpl = fetch, signal, projectId = "" } = {}) {
  const query = new URLSearchParams();
  if (projectId) query.set("project_id", projectId);
  const endpoint = query.size ? `${PET_MOOD_ENDPOINT}?${query.toString()}` : PET_MOOD_ENDPOINT;
  const response = await fetchImpl(workerUrl(endpoint), {
    headers: { Accept: "application/json" },
    signal,
  });
  if (!response.ok) throw new Error(`Pet mood failed with ${response.status}`);
  return normalizePetMood(await response.json());
}

export function normalizePetMood(value) {
  const counterKeys = [
    "today_activity_count",
    "recent_7d_activity_count",
    "pending_memory_candidate_count",
    "published_memory_count",
    "today_memory_count",
    "recent_7d_memory_count",
  ];
  const allowedKeys = new Set(["mood", "execution", ...counterKeys]);
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("Pet mood payload is invalid");
  }
  if (Object.keys(value).some((key) => !allowedKeys.has(key)) || !PET_MOOD_VALUES.includes(value.mood)) {
    throw new Error("Pet mood payload is invalid");
  }
  for (const key of counterKeys) {
    if (!Number.isInteger(value[key]) || value[key] < 0) throw new Error("Pet mood payload is invalid");
  }
  const execution = normalizeExecutionSignal(value.execution);
  return {
    mood: value.mood,
    label: labelForMood(value.mood),
    todayCount: value.today_activity_count,
    recent7dCount: value.recent_7d_activity_count,
    execution,
    pendingMemoryCandidateCount: value.pending_memory_candidate_count,
    publishedMemoryCount: value.published_memory_count,
    todayMemoryCount: value.today_memory_count,
    recent7dMemoryCount: value.recent_7d_memory_count,
  };
}

function normalizeExecutionSignal(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Pet mood payload is invalid");
  const allowedSignals = new Set(["active:valid", "active:unavailable", "inactive:none", "unavailable:unavailable"]);
  if (Object.keys(value).length !== 2 || !allowedSignals.has(`${value.effect}:${value.lease}`)) {
    throw new Error("Pet mood payload is invalid");
  }
  return Object.freeze({ effect: value.effect, lease: value.lease });
}

const COMPANION_STATE_LABELS = Object.freeze({
  booting: "正在启动",
  ready: "可以使用",
  working: "正在处理",
  attention: "有新动态",
  offline: "暂时离线",
  speaking: "正在说话",
  sleeping: "正在休息",
  warning: "需要注意",
});

export function labelForCompanionState(state, mood) {
  return COMPANION_STATE_LABELS[state] || labelForMood(mood);
}

export const PET_MOOD_VALUES = Object.freeze(Object.values(PET_MOOD));

export function subscribeCompanionState(listener, api = globalThis.electronAPI) {
  if (typeof listener !== "function" || typeof api?.subscribeCompanionState !== "function") {
    return () => {};
  }
  return api.subscribeCompanionState((projection) => {
    const allowedKeys = new Set(["state", "mood", "revision", "animation_key"]);
    if (!projection || typeof projection !== "object" || Array.isArray(projection) || Object.keys(projection).some((key) => !allowedKeys.has(key))) return;
    if (!["booting", "ready", "working", "attention", "offline", "speaking", "sleeping", "warning"].includes(projection.state)) return;
    if (!PET_MOOD_VALUES.includes(projection.mood)) return;
    if (projection.animation_key !== undefined && !["idle", "talk", "listen", "sleep", "warn", "offline", "drag", "falling", "hang_left", "hang_right"].includes(projection.animation_key)) return;
    listener({
      state: projection.state,
      mood: projection.mood,
      revision: Number(projection.revision) || 0,
      ...(projection.animation_key ? { animation_key: projection.animation_key } : {}),
    });
  });
}
