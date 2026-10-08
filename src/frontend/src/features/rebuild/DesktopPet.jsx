import { useCallback, useEffect, useRef, useState } from "react";
import { getPetMood, PET_MOOD, subscribeCompanionState } from "./petMoodStore";
import { FALLBACK_COMPANION_PACK, loadCompanionPack, resolveCompanionState } from "./companionPack";
import { startPetVoicePlayback } from "./petVoicePlayback";
import "./desktopPet.css";

// Keep this relative to the renderer document. An absolute web path resolves
// to the drive root when the packaged app is loaded through file://.
const SPRITE_SRC = FALLBACK_COMPANION_PACK.sprite.src;
const FRAME_WIDTH = FALLBACK_COMPANION_PACK.sprite.frame_width;
const FRAME_HEIGHT = FALLBACK_COMPANION_PACK.sprite.frame_height;
const ALPHA_HIT_THRESHOLD = FALLBACK_COMPANION_PACK.hit_region.threshold;
const PET_STATUS_LABELS = Object.freeze({
  idle: "可以使用",
  busy: "正在处理",
  "needs-confirmation": "需要确认",
  unavailable: "暂不可用",
});

function invokeOrFallback(injected, apiKey, ...args) {
  if (typeof injected === "function") return injected(...args);
  return globalThis.electronAPI?.[apiKey]?.(...args);
}

function subscribeCompanionVoice(listener) {
  return globalThis.electronAPI?.subscribeCompanionVoice?.(listener) || (() => {});
}

function subscribeCompanionAppearance(listener) {
  return globalThis.electronAPI?.subscribeCompanionAppearance?.(listener) || (() => {});
}

export function DesktopPet({
  openMainWindow,
  openPetContextMenu,
  setMousePassthrough,
  movePetWindow,
  finishPetWindowMove,
  beginPetGesture,
  updatePetGesture,
  endPetGesture,
  commitPetClick,
  getMood = getPetMood,
  subscribeState = subscribeCompanionState,
  loadPack = loadCompanionPack,
  startVoicePlayback = startPetVoicePlayback,
  subscribeVoice = subscribeCompanionVoice,
  subscribeAppearance = subscribeCompanionAppearance,
} = {}) {
  const [projection, setProjection] = useState(() => ({ state: "booting", mood: getMood().mood, revision: 0 }));
  const [frame, setFrame] = useState(0);
  const [isHovered, setIsHovered] = useState(false);
  const [spriteStatus, setSpriteStatus] = useState("loading");
  const [reducedMotion, setReducedMotion] = useState(false);
  const [mouthOpen, setMouthOpen] = useState(false);
  const [appearanceOverlayFailed, setAppearanceOverlayFailed] = useState(false);
  const [appearance, setAppearance] = useState({ outfit_id: "default", background_id: "default", growth_stage: "new", idle_variant: "default", revision: 1 });
  const [packResult, setPackResult] = useState({ pack: FALLBACK_COMPANION_PACK, fallbackUsed: true });
  const canvasRef = useRef(null);
  const spriteRef = useRef(null);
  const passthroughRef = useRef(null);
  const dragRef = useRef(null);
  const gestureSequenceRef = useRef(0);
  const voiceSessionRef = useRef(null);
  const voiceRequestRef = useRef(null);

  useEffect(() => {
    const next = getMood();
    setProjection({ state: "booting", mood: next.mood, revision: 0 });
    return subscribeState(setProjection);
  }, [getMood, subscribeState]);

  useEffect(() => {
    const media = globalThis.matchMedia?.("(prefers-reduced-motion: reduce)");
    if (!media) return undefined;
    const update = () => setReducedMotion(media.matches);
    update();
    media.addEventListener?.("change", update);
    return () => media.removeEventListener?.("change", update);
  }, []);

  useEffect(() => {
    let active = true;
    Promise.resolve(loadPack()).then((result) => {
      if (active && result?.pack) setPackResult(result);
    }).catch(() => {});
    return () => { active = false; };
  }, [loadPack]);

  useEffect(() => {
    let mounted = true;
    const unsubscribe = subscribeVoice((event) => {
      const previousRequestId = voiceRequestRef.current;
      if (previousRequestId) globalThis.electronAPI?.reportCompanionVoicePlayback?.(previousRequestId, "cancelled");
      voiceSessionRef.current?.stop();
      voiceSessionRef.current = null;
      setMouthOpen(false);
      if (event?.cancelled === true) { voiceRequestRef.current = null; return; }
      const requestId = event?.request_id;
      voiceRequestRef.current = requestId;
      Promise.resolve(startVoicePlayback(event.audio, {
        minimumHoldMs: reducedMotion ? 180 : 70,
        onMouth: (open) => { if (mounted && voiceRequestRef.current === requestId) setMouthOpen(open); },
        onEnded: () => { if (mounted && voiceRequestRef.current === requestId) { globalThis.electronAPI?.reportCompanionVoicePlayback?.(requestId, "ended"); voiceRequestRef.current = null; setMouthOpen(false); } },
      })).then((session) => {
        if (!mounted || voiceRequestRef.current !== requestId) session?.stop?.();
        else { voiceSessionRef.current = session; globalThis.electronAPI?.reportCompanionVoicePlayback?.(requestId, "playing"); }
      }).catch(() => { if (mounted && voiceRequestRef.current === requestId) { globalThis.electronAPI?.reportCompanionVoicePlayback?.(requestId, "failed"); voiceRequestRef.current = null; setMouthOpen(false); } });
    });
    return () => { mounted = false; unsubscribe?.(); if (voiceRequestRef.current) globalThis.electronAPI?.reportCompanionVoicePlayback?.(voiceRequestRef.current, "cancelled"); voiceSessionRef.current?.stop(); voiceSessionRef.current = null; voiceRequestRef.current = null; };
  }, [reducedMotion, startVoicePlayback, subscribeVoice]);

  useEffect(() => subscribeAppearance((next) => { setAppearance(next); setAppearanceOverlayFailed(false); }), [subscribeAppearance]);

  const requestedVisual = projection.animation_key || projection.state;
  const resolvedVisual = resolveCompanionState(packResult.pack, requestedVisual);
  const frameSpec = resolvedVisual.state;

  useEffect(() => {
    setFrame(0);
    if (reducedMotion) return undefined;
    let nextFrame = 0;
    const interval = globalThis.setInterval(() => {
      if (!frameSpec.loop && nextFrame >= frameSpec.frames.length - 1) {
        globalThis.clearInterval(interval);
        return;
      }
      nextFrame = (nextFrame + 1) % frameSpec.frames.length;
      setFrame(nextFrame);
    }, Math.round(1000 / frameSpec.fps));
    return () => globalThis.clearInterval(interval);
  }, [frameSpec, projection.revision, reducedMotion]);

  const drawFrame = useCallback(() => {
    const canvas = canvasRef.current;
    const sprite = spriteRef.current;
    if (!canvas || !sprite?.complete || !sprite.naturalWidth) return;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (!context) return;
    const sourceFrame = frameSpec.frames[Math.min(frame, frameSpec.frames.length - 1)] ?? frameSpec.frames[0];
    context.clearRect(0, 0, FRAME_WIDTH, FRAME_HEIGHT);
    context.drawImage(
      sprite,
      sourceFrame * packResult.pack.sprite.frame_width,
      frameSpec.row * packResult.pack.sprite.frame_height,
      FRAME_WIDTH,
      FRAME_HEIGHT,
      0,
      0,
      FRAME_WIDTH,
      FRAME_HEIGHT,
    );
    if (mouthOpen && typeof context.ellipse === "function") {
      context.save?.();
      context.beginPath?.();
      context.ellipse(FRAME_WIDTH / 2, 151, 10, 7, 0, 0, Math.PI * 2);
      context.fillStyle = "#3b2118";
      context.fill?.();
      context.restore?.();
    }
  }, [frame, frameSpec, mouthOpen, packResult.pack]);

  useEffect(drawFrame, [drawFrame]);

  function updatePassthrough(shouldPassThrough) {
    if (passthroughRef.current === shouldPassThrough) return;
    passthroughRef.current = shouldPassThrough;
    invokeOrFallback(setMousePassthrough, "setPetMousePassthrough", shouldPassThrough);
  }

  function inspectVisiblePixel(event) {
    const canvas = canvasRef.current;
    if (!canvas) return false;
    const bounds = canvas.getBoundingClientRect();
    const x = Math.floor(((event.clientX - bounds.left) / bounds.width) * FRAME_WIDTH);
    const y = Math.floor(((event.clientY - bounds.top) / bounds.height) * FRAME_HEIGHT);
    let isVisibleBear = false;
    if (x >= 0 && x < FRAME_WIDTH && y >= 0 && y < FRAME_HEIGHT) {
      const context = canvas.getContext("2d", { willReadFrequently: true });
      const alpha = context?.getImageData(x, y, 1, 1).data[3] ?? 0;
      isVisibleBear = alpha > packResult.pack.hit_region.threshold;
    }
    setIsHovered(isVisibleBear);
    updatePassthrough(!isVisibleBear);
    return isVisibleBear;
  }

  function normalizedPoint(event) {
    const bounds = canvasRef.current?.getBoundingClientRect();
    if (!bounds?.width || !bounds?.height) return { x: 0.5, y: 0.5 };
    return {
      x: Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width)),
      y: Math.min(1, Math.max(0, (event.clientY - bounds.top) / bounds.height)),
    };
  }

  function handleClick(event) {
    if (dragRef.current?.moved) {
      dragRef.current = null;
      return;
    }
    const gesture = dragRef.current;
    dragRef.current = null;
    if (!gesture || !event) return;
    const point = normalizedPoint(event);
    invokeOrFallback(commitPetClick, "commitPetClick", {
      gesture_id: gesture.gestureId,
      x: point.x,
      y: point.y,
      click_count: Math.min(2, Math.max(1, event.detail || 1)),
    });
  }

  function handleDragStart(event) {
    if (event.button !== 0 || !inspectVisiblePixel(event)) return;
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.setPointerCapture?.(event.pointerId);
    gestureSequenceRef.current += 1;
    const gestureId = `gesture-${Date.now().toString(36)}-${gestureSequenceRef.current.toString(36)}`;
    const point = normalizedPoint(event);
    dragRef.current = { pointerId: event.pointerId, gestureId, x: event.screenX, y: event.screenY, distance: 0, moved: false };
    invokeOrFallback(beginPetGesture, "beginPetGesture", { gesture_id: gestureId, pointer_kind: ["mouse", "pen", "touch"].includes(event.pointerType) ? event.pointerType : "mouse", x: point.x, y: point.y });
    updatePassthrough(false);
  }

  function handleDragMove(event) {
    const previous = dragRef.current;
    if (!previous || previous.pointerId !== event.pointerId) {
      inspectVisiblePixel(event);
      return;
    }
    const dx = event.screenX - previous.x;
    const dy = event.screenY - previous.y;
    if (!dx && !dy) return;
    const distance = previous.distance + Math.hypot(dx, dy);
    dragRef.current = { ...previous, x: event.screenX, y: event.screenY, distance, moved: distance > 8 };
    invokeOrFallback(movePetWindow, "movePetWindow", { dx, dy });
    invokeOrFallback(updatePetGesture, "updatePetGesture", { gesture_id: previous.gestureId, dx, dy });
  }

  function handleDragEnd(event) {
    if (dragRef.current?.pointerId !== event.pointerId) return;
    event.preventDefault();
    event.stopPropagation();
    event.currentTarget.releasePointerCapture?.(event.pointerId);
    invokeOrFallback(endPetGesture, "endPetGesture", { gesture_id: dragRef.current.gestureId, cancelled: event.type === "pointercancel" });
    invokeOrFallback(finishPetWindowMove, "finishPetWindowMove");
  }

  function handleSpriteLoad() {
    const sprite = spriteRef.current;
    if (sprite?.naturalWidth !== packResult.pack.sprite.width || sprite?.naturalHeight !== packResult.pack.sprite.height) {
      setSpriteStatus("error");
      updatePassthrough(true);
      return;
    }
    setSpriteStatus("ready");
    drawFrame();
  }

  function handleSpriteError() {
    setSpriteStatus("error");
    updatePassthrough(true);
  }

  function handleContextMenu(event) {
    event.preventDefault();
    invokeOrFallback(openPetContextMenu, "openPetContextMenu");
  }

  function handleKeyDown(event) {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      invokeOrFallback(openMainWindow, "openMainWindow");
    }
    if (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10")) {
      event.preventDefault();
      invokeOrFallback(openPetContextMenu, "openPetContextMenu");
    }
  }

  const petStatus = statusForProjection(projection.state);
  const moodLabel = PET_STATUS_LABELS[petStatus];
  const outfitOverlay = appearance.outfit_id === "default" ? null : packResult.pack.overlays?.[`outfit_${appearance.outfit_id.replaceAll("-", "_")}`];

  return (
    <div
      className={`desktop-pet-shell pet-status-${petStatus} background-${appearance.background_id} growth-${appearance.growth_stage} idle-${appearance.idle_variant}${isHovered ? " is-hovered" : ""}`}
      onClick={handleClick}
      onContextMenu={handleContextMenu}
      onKeyDown={handleKeyDown}
      onMouseMove={inspectVisiblePixel}
      onPointerDown={handleDragStart}
      onPointerMove={handleDragMove}
      onPointerUp={handleDragEnd}
      onPointerCancel={handleDragEnd}
      role="button"
      tabIndex={0}
      aria-label={`桌面宠物：${moodLabel}。按 Enter 或空格打开主界面，右键打开宠物菜单`}
      title={`${moodLabel}。按 Enter 或空格打开主界面`}
      data-frame={frame}
      data-state={petStatus}
      data-sprite-status={spriteStatus}
      data-pack={packResult.pack.pack_id}
      data-pack-fallback={packResult.fallbackUsed || resolvedVisual.fallbackUsed ? "true" : "false"}
      data-mouth={mouthOpen ? "open" : "closed"}
      data-outfit={appearance.outfit_id}
      data-background={appearance.background_id}
      data-growth={appearance.growth_stage}
      data-idle-variant={appearance.idle_variant}
    >
      <img
        ref={spriteRef}
        className="desktop-pet-sprite-source"
        src={packResult.pack.sprite.src}
        alt=""
        aria-hidden="true"
        draggable={false}
        onLoad={handleSpriteLoad}
        onError={handleSpriteError}
      />
      <canvas
        ref={canvasRef}
        className="desktop-pet-bear"
        width={FRAME_WIDTH}
        height={FRAME_HEIGHT}
        aria-hidden="true"
      />
      {outfitOverlay && !appearanceOverlayFailed ? <img className="desktop-pet-outfit-overlay" src={outfitOverlay.src} alt="" aria-hidden="true" draggable={false} style={{ left: `${(outfitOverlay.x / packResult.pack.sprite.frame_width) * 100}%`, top: `${(outfitOverlay.y / packResult.pack.sprite.frame_height) * 100}%`, width: `${(outfitOverlay.width / packResult.pack.sprite.frame_width) * 100}%`, height: `${(outfitOverlay.height / packResult.pack.sprite.frame_height) * 100}%` }} onLoad={(event) => { if (event.currentTarget.naturalWidth !== outfitOverlay.natural_width || event.currentTarget.naturalHeight !== outfitOverlay.natural_height) setAppearanceOverlayFailed(true); }} onError={() => setAppearanceOverlayFailed(true)} /> : null}
      {isHovered ? <div className="desktop-pet-bubble" aria-hidden="true">{moodLabel}</div> : null}
    </div>
  );
}

function statusForProjection(state) {
  if (state === "attention" || state === "warning") return "needs-confirmation";
  if (state === "offline") return "unavailable";
  if (state === "booting" || state === "working" || state === "speaking") return "busy";
  return "idle";
}

export { ALPHA_HIT_THRESHOLD, FRAME_HEIGHT, FRAME_WIDTH, PET_MOOD, SPRITE_SRC };
