import { productFetch } from '../../shared/api/deviceTransport';
const PACK_URL = "./mascots/companion-pack.json";

export const FALLBACK_COMPANION_PACK = deepFreeze({
  version: 3,
  pack_id: "bear-companion-safe-fallback",
  sprite: {
    src: "./mascots/bear_companion_sprite-v1.png",
    width: 1536,
    height: 1872,
    frame_width: 384,
    frame_height: 468,
    columns: 4,
    rows: 4,
  },
  hit_region: { kind: "alpha", threshold: 18 },
  overlays: {},
  states: {
    ready: { row: 0, frames: [0], fps: 1, loop: true, fallback: null, authentic: true },
    booting: { fallback: "ready" }, working: { fallback: "ready" }, attention: { fallback: "ready" },
    offline: { fallback: "ready" }, speaking: { fallback: "ready" }, sleeping: { fallback: "ready" },
    warning: { fallback: "ready" }, drag: { fallback: "ready" }, falling: { fallback: "ready" },
    hang_left: { fallback: "ready" }, hang_right: { fallback: "ready" },
    idle: { fallback: "ready" }, talk: { fallback: "ready" }, listen: { fallback: "ready" },
    sleep: { fallback: "ready" }, warn: { fallback: "ready" },
  },
});

export async function loadCompanionPack({ request = productFetch } = {}) {
  if (typeof request !== "function") return { pack: FALLBACK_COMPANION_PACK, fallbackUsed: true };
  try {
    const response = await request(PACK_URL, { cache: "no-store", credentials: "same-origin" });
    if (!response?.ok) throw new Error("pack unavailable");
    return { pack: validateCompanionPack(await response.json()), fallbackUsed: false };
  } catch {
    return { pack: FALLBACK_COMPANION_PACK, fallbackUsed: true };
  }
}

export function validateCompanionPack(value) {
  if (!plain(value) || ![2, 3].includes(value.version)) throw new TypeError("companion pack schema is invalid");
  const expectedKeys = value.version === 3 ? "hit_region,overlays,pack_id,sprite,states,version" : "hit_region,pack_id,sprite,states,version";
  if (keys(value) !== expectedKeys) throw new TypeError("companion pack schema is invalid");
  if (typeof value.pack_id !== "string" || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(value.pack_id)) throw new TypeError("companion pack id is invalid");
  const sprite = value.sprite;
  if (!plain(sprite) || keys(sprite) !== "columns,frame_height,frame_width,height,rows,src,width") throw new TypeError("companion sprite schema is invalid");
  if (typeof sprite.src !== "string" || !/^\.\/mascots\/[a-z0-9][a-z0-9_-]*\.png$/.test(sprite.src)) throw new TypeError("companion sprite path is invalid");
  for (const field of ["width", "height", "frame_width", "frame_height", "columns", "rows"]) positive(sprite[field], `sprite ${field}`);
  if (sprite.width !== sprite.frame_width * sprite.columns || sprite.height !== sprite.frame_height * sprite.rows) throw new TypeError("companion sprite dimensions are inconsistent");
  if (!plain(value.hit_region) || keys(value.hit_region) !== "kind,threshold" || value.hit_region.kind !== "alpha" || !Number.isInteger(value.hit_region.threshold) || value.hit_region.threshold < 0 || value.hit_region.threshold > 255) throw new TypeError("companion hit region is invalid");
  if (value.version === 3) validateOverlays(value.overlays, sprite);
  if (!plain(value.states) || !Object.hasOwn(value.states, "ready") || Object.keys(value.states).length > 32) throw new TypeError("companion states are invalid");
  for (const [stateId, state] of Object.entries(value.states)) validateState(stateId, state, sprite, value.states);
  for (const stateId of Object.keys(value.states)) resolveCompanionState(value, stateId);
  const normalized = structuredClone(value);
  if (value.version === 2) normalized.overlays = {};
  return deepFreeze(normalized);
}

function validateOverlays(overlays, sprite) {
  if (!plain(overlays) || Object.keys(overlays).length > 8) throw new TypeError("companion overlays are invalid");
  for (const [overlayId, overlay] of Object.entries(overlays)) {
    if (!/^[a-z][a-z0-9_]{0,31}$/.test(overlayId) || !plain(overlay) || keys(overlay) !== "height,natural_height,natural_width,src,width,x,y") throw new TypeError("companion overlay schema is invalid");
    if (typeof overlay.src !== "string" || !/^\.\/mascots\/(?:[a-z0-9][a-z0-9_-]*\.png|items\/[a-z0-9][a-z0-9-]*\.svg)$/.test(overlay.src)) throw new TypeError("companion overlay path is invalid");
    positive(overlay.width, "overlay width");
    positive(overlay.height, "overlay height");
    positive(overlay.natural_width, "overlay natural width");
    positive(overlay.natural_height, "overlay natural height");
    if (!Number.isInteger(overlay.x) || !Number.isInteger(overlay.y) || overlay.x < 0 || overlay.y < 0 || overlay.x + overlay.width > sprite.frame_width || overlay.y + overlay.height > sprite.frame_height) throw new TypeError("companion overlay bounds are invalid");
  }
}

export function resolveCompanionState(pack, requestedState) {
  const requested = typeof requestedState === "string" ? requestedState : "ready";
  let stateId = Object.hasOwn(pack.states, requested) ? requested : "ready";
  const visited = new Set();
  while (true) {
    if (visited.has(stateId)) throw new TypeError("companion state fallback cycle");
    visited.add(stateId);
    const state = pack.states[stateId];
    if (Array.isArray(state.frames)) return { stateId, state, fallbackUsed: stateId !== requested || state.authentic !== true };
    stateId = state.fallback;
  }
}

function validateState(stateId, state, sprite, states) {
  if (!/^[a-z][a-z0-9_]{0,31}$/.test(stateId) || !plain(state)) throw new TypeError("companion state is invalid");
  if (keys(state) === "fallback") {
    if (typeof state.fallback !== "string" || !Object.hasOwn(states, state.fallback)) throw new TypeError("companion state fallback is invalid");
    return;
  }
  if (keys(state) !== "authentic,fallback,fps,frames,loop,row") throw new TypeError("companion frame state schema is invalid");
  if (!Number.isInteger(state.row) || state.row < 0 || state.row >= sprite.rows) throw new TypeError("companion state row is invalid");
  if (!Array.isArray(state.frames) || !state.frames.length || state.frames.length > sprite.columns || new Set(state.frames).size !== state.frames.length || state.frames.some((frame) => !Number.isInteger(frame) || frame < 0 || frame >= sprite.columns)) throw new TypeError("companion state frames are invalid");
  if (typeof state.fps !== "number" || !Number.isFinite(state.fps) || state.fps < 0.5 || state.fps > 30) throw new TypeError("companion state fps is invalid");
  if (typeof state.loop !== "boolean" || typeof state.authentic !== "boolean") throw new TypeError("companion state flags are invalid");
  if (state.fallback !== null && (typeof state.fallback !== "string" || !Object.hasOwn(states, state.fallback))) throw new TypeError("companion state fallback is invalid");
}

function positive(value, label) {
  if (!Number.isInteger(value) || value < 1 || value > 8192) throw new TypeError(`${label} is invalid`);
}

function plain(value) { return Boolean(value) && typeof value === "object" && !Array.isArray(value) && Object.getPrototypeOf(value) === Object.prototype; }
function keys(value) { return Object.keys(value).sort().join(); }
function deepFreeze(value) { Object.freeze(value); for (const nested of Object.values(value)) if (nested && typeof nested === "object" && !Object.isFrozen(nested)) deepFreeze(nested); return value; }

export { PACK_URL };
