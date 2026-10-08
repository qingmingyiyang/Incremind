import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DesktopPet, PET_MOOD, SPRITE_SRC } from "@src/features/rebuild/DesktopPet";
import { validateCompanionPack } from "@src/features/rebuild/companionPack";

describe("DesktopPet", () => {
  let alpha = 255;
  let context;

  beforeEach(() => {
    alpha = 255;
    context = {
      clearRect: vi.fn(),
      drawImage: vi.fn(),
      getImageData: vi.fn(() => ({ data: [0, 0, 0, alpha] })),
    };
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(context);
    vi.spyOn(HTMLCanvasElement.prototype, "getBoundingClientRect").mockReturnValue({
      left: 0, top: 0, right: 180, bottom: 220, width: 180, height: 220,
    });
  });

  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("renders the versioned bear-only sprite and canvas", () => {
    const { container } = render(<DesktopPet />);
    expect(container.querySelector("img")?.getAttribute("src")).toBe(SPRITE_SRC);
    expect(container.querySelector("canvas")).toHaveAttribute("width", "384");
    expect(container.querySelector(".desktop-pet-shell")).toHaveAttribute("data-state", "busy");
    expect(SPRITE_SRC).toBe("./mascots/bear_companion_sprite-v1.png");
    expect(container.querySelector(".desktop-pet-shell")).toHaveAttribute("data-sprite-status", "loading");
  });

  it("exposes sprite readiness and fails closed to mouse passthrough", () => {
    const setMousePassthrough = vi.fn();
    const { container } = render(<DesktopPet setMousePassthrough={setMousePassthrough} />);
    const image = container.querySelector("img");
    Object.defineProperties(image, { naturalWidth: { value: 1536 }, naturalHeight: { value: 1872 } });
    fireEvent.load(image);
    expect(container.querySelector(".desktop-pet-shell")).toHaveAttribute("data-sprite-status", "ready");
    fireEvent.error(image);
    expect(container.querySelector(".desktop-pet-shell")).toHaveAttribute("data-sprite-status", "error");
    expect(setMousePassthrough).toHaveBeenLastCalledWith(true);
  });

  it("fails closed when the loaded sprite dimensions drift from the pack", () => {
    const setMousePassthrough = vi.fn();
    const { container } = render(<DesktopPet setMousePassthrough={setMousePassthrough} />);
    const image = container.querySelector("img");
    Object.defineProperties(image, { naturalWidth: { value: 768 }, naturalHeight: { value: 936 } });
    fireEvent.load(image);
    expect(container.querySelector(".desktop-pet-shell")).toHaveAttribute("data-sprite-status", "error");
    expect(setMousePassthrough).toHaveBeenLastCalledWith(true);
  });

  it("keeps Enter and Space as direct accessible main-window actions", () => {
    const openMainWindow = vi.fn();
    render(<DesktopPet openMainWindow={openMainWindow} />);
    const pet = screen.getByRole("button");
    expect(pet).toHaveAttribute("tabindex", "0");
    fireEvent.keyDown(pet, { key: "Enter" });
    fireEvent.keyDown(pet, { key: " " });
    expect(openMainWindow).toHaveBeenCalledTimes(2);
  });

  it("opens the native pet menu for right click and keyboard menu commands", () => {
    const openPetContextMenu = vi.fn();
    const beginPetGesture = vi.fn();
    render(<DesktopPet openPetContextMenu={openPetContextMenu} beginPetGesture={beginPetGesture} />);
    const pet = screen.getByRole("button");
    fireEvent.pointerDown(pet, { button: 2, pointerId: 9, pointerType: "mouse" });
    fireEvent.contextMenu(pet);
    fireEvent.keyDown(pet, { key: "ContextMenu" });
    fireEvent.keyDown(pet, { key: "F10", shiftKey: true });
    fireEvent.keyDown(pet, { key: "Escape" });
    expect(openPetContextMenu).toHaveBeenCalledTimes(3);
    expect(beginPetGesture).not.toHaveBeenCalled();
  });

  it("moves from the visible bear surface and saves the final native position", () => {
    const movePetWindow = vi.fn();
    const finishPetWindowMove = vi.fn();
    const openMainWindow = vi.fn();
    render(<DesktopPet movePetWindow={movePetWindow} finishPetWindowMove={finishPetWindowMove} openMainWindow={openMainWindow} />);
    const pet = screen.getByRole("button");
    pet.setPointerCapture = vi.fn();
    pet.releasePointerCapture = vi.fn();
    fireEvent.pointerDown(pet, { button: 0, pointerId: 1, clientX: 90, clientY: 100, screenX: 100, screenY: 120 });
    fireEvent.pointerMove(pet, { pointerId: 1, clientX: 114, clientY: 112, screenX: 124, screenY: 132 });
    fireEvent.pointerUp(pet, { pointerId: 1, screenX: 124, screenY: 132 });
    fireEvent.click(pet);
    expect(movePetWindow).toHaveBeenCalledWith({ dx: 24, dy: 12 });
    expect(finishPetWindowMove).toHaveBeenCalledTimes(1);
    expect(openMainWindow).not.toHaveBeenCalled();
  });

  it("reports bounded pointer facts and click intent to the main-process arbiter", () => {
    const beginPetGesture = vi.fn();
    const updatePetGesture = vi.fn();
    const endPetGesture = vi.fn();
    const commitPetClick = vi.fn();
    render(<DesktopPet {...{ beginPetGesture, updatePetGesture, endPetGesture, commitPetClick }} />);
    const pet = screen.getByRole("button");
    pet.setPointerCapture = vi.fn();
    pet.releasePointerCapture = vi.fn();
    fireEvent.pointerDown(pet, { button: 0, pointerId: 2, pointerType: "mouse", clientX: 90, clientY: 22, screenX: 900, screenY: 700 });
    fireEvent.pointerMove(pet, { pointerId: 2, clientX: 93, clientY: 26, screenX: 903, screenY: 704 });
    fireEvent.pointerUp(pet, { pointerId: 2, screenX: 903, screenY: 704 });
    fireEvent.click(pet, { clientX: 90, clientY: 22, detail: 1 });
    expect(beginPetGesture).toHaveBeenCalledWith(expect.objectContaining({ pointer_kind: "mouse", x: 0.5, y: 0.1 }));
    expect(updatePetGesture).toHaveBeenCalledWith(expect.objectContaining({ dx: 3, dy: 4 }));
    expect(endPetGesture).toHaveBeenCalledWith(expect.objectContaining({ cancelled: false }));
    expect(commitPetClick).toHaveBeenCalledWith(expect.objectContaining({ x: 0.5, y: 0.1, click_count: 1 }));
    const serialized = JSON.stringify([beginPetGesture.mock.calls, commitPetClick.mock.calls]);
    expect(serialized).not.toContain("900");
    expect(serialized).not.toContain("700");
  });

  it("falls back to the bounded Electron bridge", () => {
    const original = globalThis.electronAPI;
    const openMainWindow = vi.fn();
    const openPetContextMenu = vi.fn();
    const beginPetGesture = vi.fn();
    const endPetGesture = vi.fn();
    const commitPetClick = vi.fn();
    globalThis.electronAPI = { openMainWindow, openPetContextMenu, beginPetGesture, endPetGesture, commitPetClick };
    try {
      render(<DesktopPet />);
      const pet = screen.getByRole("button");
      pet.setPointerCapture = vi.fn();
      pet.releasePointerCapture = vi.fn();
      fireEvent.pointerDown(pet, { button: 0, pointerId: 3, clientX: 90, clientY: 22, screenX: 90, screenY: 22 });
      fireEvent.pointerUp(pet, { pointerId: 3, screenX: 90, screenY: 22 });
      fireEvent.click(pet, { clientX: 90, clientY: 22, detail: 1 });
      fireEvent.contextMenu(pet);
      expect(openMainWindow).not.toHaveBeenCalled();
      expect(commitPetClick).toHaveBeenCalledTimes(1);
      expect(openPetContextMenu).toHaveBeenCalledTimes(1);
    } finally {
      globalThis.electronAPI = original;
    }
  });

  it("uses manifest frames and exposes attention as a redacted confirmation state", async () => {
    vi.useFakeTimers();
    let listener;
    const unsubscribe = vi.fn();
    const subscribeState = vi.fn((next) => { listener = next; return unsubscribe; });
    const pack = validateCompanionPack({
      version: 2, pack_id: "test-pack",
      sprite: { src: "./mascots/test.png", width: 1536, height: 1872, frame_width: 384, frame_height: 468, columns: 4, rows: 4 },
      hit_region: { kind: "alpha", threshold: 18 },
      states: {
        ready: { row: 0, frames: [0], fps: 1, loop: true, fallback: null, authentic: true },
        attention: { row: 2, frames: [0, 1, 2, 3], fps: 5, loop: false, fallback: "ready", authentic: true },
      },
    });
    const loadPack = vi.fn().mockResolvedValue({ pack, fallbackUsed: false });
    const { container, unmount } = render(<DesktopPet subscribeState={subscribeState} loadPack={loadPack} />);
    await act(async () => {});
    act(() => listener({ state: "attention", mood: "celebrating", revision: 7 }));
    expect(container.querySelector(".desktop-pet-shell")).toHaveAttribute("data-state", "needs-confirmation");
    act(() => vi.advanceTimersByTime(200));
    expect(container.querySelector(".desktop-pet-shell")).toHaveAttribute("data-frame", "1");
    act(() => listener({ state: "attention", mood: "celebrating", revision: 8 }));
    expect(container.querySelector(".desktop-pet-shell")).toHaveAttribute("data-frame", "0");
    unmount();
    expect(unsubscribe).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });

  it("passes transparent pixels through and restores input on visible alpha", () => {
    const setMousePassthrough = vi.fn();
    render(<DesktopPet setMousePassthrough={setMousePassthrough} />);
    const pet = screen.getByRole("button");
    alpha = 0;
    fireEvent.mouseMove(pet, { clientX: 5, clientY: 5 });
    alpha = 255;
    fireEvent.mouseMove(pet, { clientX: 90, clientY: 100 });
    fireEvent.mouseMove(pet, { clientX: 91, clientY: 101 });
    expect(setMousePassthrough.mock.calls).toEqual([[true], [false]]);
    expect(screen.getByText("正在处理")).toBeInTheDocument();
  });

  it("exposes PET_MOOD compatibility constants", () => {
    expect(PET_MOOD.CALM).toBe("calm");
    expect(PET_MOOD.FOCUSED).toBe("focused");
    expect(PET_MOOD.ANALYZING).toBe("analyzing");
  });

  it("maps every projection to one redacted state without detail overlays", () => {
    let listener;
    const { container } = render(<DesktopPet subscribeState={(next) => { listener = next; return vi.fn(); }} />);
    const pet = container.querySelector(".desktop-pet-shell");
    const expected = {
      ready: "idle", sleeping: "idle", working: "busy", speaking: "busy", booting: "busy",
      attention: "needs-confirmation", warning: "needs-confirmation", offline: "unavailable",
    };
    for (const [state, status] of Object.entries(expected)) {
      act(() => listener({ state, mood: "calm", revision: 1 }));
      expect(pet).toHaveAttribute("data-state", status);
    }
    expect(container.querySelector(".desktop-pet-weather-overlay")).toBeNull();
    expect(container.querySelector(".desktop-pet-media-symbol")).toBeNull();
    expect(pet.innerHTML).not.toContain("本地曲目");
  });

  it("renders owned appearance ids through trusted pack overlays and local themes", async () => {
    let appearanceListener;
    const pack = validateCompanionPack({
      version: 3, pack_id: "appearance-pack",
      sprite: { src: "./mascots/test.png", width: 1536, height: 1872, frame_width: 384, frame_height: 468, columns: 4, rows: 4 },
      hit_region: { kind: "alpha", threshold: 18 },
      overlays: { outfit_red_scarf: { src: "./mascots/items/red-scarf.svg", natural_width: 96, natural_height: 96, x: 144, y: 192, width: 96, height: 96 } },
      states: { ready: { row: 0, frames: [0], fps: 1, loop: true, fallback: null, authentic: true }, booting: { fallback: "ready" } },
    });
    const { container } = render(<DesktopPet subscribeAppearance={(listener) => { appearanceListener = listener; return vi.fn(); }} loadPack={async () => ({ pack, fallbackUsed: false })} />);
    await act(async () => {});
    act(() => appearanceListener({ outfit_id: "red-scarf", background_id: "night", growth_stage: "partner", idle_variant: "smile", revision: 3 }));
    expect(container.querySelector(".desktop-pet-outfit-overlay")).toHaveAttribute("src", "./mascots/items/red-scarf.svg");
    expect(container.querySelector(".desktop-pet-shell")).toHaveClass("background-night", "growth-partner", "idle-smile");
    expect(container.querySelector(".desktop-pet-shell")).toHaveAttribute("data-outfit", "red-scarf");
  });

  it("reports playback start, end, failure, and supersede with the opaque request id", async () => {
    let listener; let end;
    const report = vi.fn();
    vi.stubGlobal("electronAPI", { reportCompanionVoicePlayback: report });
    const subscribeVoice = vi.fn((value) => { listener = value; return vi.fn(); });
    const startVoicePlayback = vi.fn(async (_audio, options) => { end = options.onEnded; return { stop: vi.fn() }; });
    render(<DesktopPet subscribeVoice={subscribeVoice} startVoicePlayback={startVoicePlayback}/>);
    await act(async () => { listener({ request_id: "voice:first", audio: new Uint8Array(44) }); });
    expect(report).toHaveBeenCalledWith("voice:first", "playing");
    await act(async () => { listener({ request_id: "voice:second", audio: new Uint8Array(44) }); });
    expect(report).toHaveBeenCalledWith("voice:first", "cancelled");
    act(() => end());
    expect(report).toHaveBeenCalledWith("voice:second", "ended");
  });
});
