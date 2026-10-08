import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "@src/App";

const originalFetch = globalThis.fetch;
const originalElectronApi = globalThis.electronAPI;

function setHash(hash) {
  window.history.replaceState({}, "", hash);
  act(() => {
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  });
}

describe("pet renderer surface scope", () => {
  beforeEach(() => {
    window.history.replaceState({}, "", "#view=rebuild-pet");
    localStorage.clear();
    globalThis.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ mood: "calm" }),
    });
    globalThis.electronAPI = {
      openMainWindow: vi.fn(),
      beginPetGesture: vi.fn(),
      endPetGesture: vi.fn(),
      commitPetClick: vi.fn(),
    };
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
    globalThis.electronAPI = originalElectronApi;
    vi.restoreAllMocks();
  });

  it.each([false, true])("renders only the pet surface when developer mode is %s", async (developerMode) => {
    localStorage.setItem("chriptmas-os-developer-mode", String(developerMode));

    render(<App />);

    expect(await screen.findByRole("button", { name: /桌面宠物/ })).toBeInTheDocument();
    expect(screen.queryByLabelText("主导航")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Developer Studio/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: "Command Palette" })).not.toBeInTheDocument();
    expect(document.querySelector(".global-dev-badge")).not.toBeInTheDocument();
  });

  it("keeps the initial pet renderer scoped after a main-app hash is injected", async () => {
    localStorage.setItem("chriptmas-os-developer-mode", "true");
    render(<App />);

    expect(await screen.findByRole("button", { name: /桌面宠物/ })).toBeInTheDocument();
    setHash("#view=rebuild-developer-studio");

    expect(screen.getByRole("button", { name: /桌面宠物/ })).toBeInTheDocument();
    expect(screen.queryByLabelText("Developer Studio")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("主导航")).not.toBeInTheDocument();
    expect(document.querySelector(".global-dev-badge")).not.toBeInTheDocument();
  });

  it("does not treat pointer click as direct open and keeps keyboard opening the main window", async () => {
    render(<App />);
    const pet = await screen.findByRole("button", { name: /桌面宠物/ });

    pet.setPointerCapture = vi.fn();
    pet.releasePointerCapture = vi.fn();
    fireEvent.pointerDown(pet, { button: 0, pointerId: 1, clientX: 40, clientY: 20, screenX: 40, screenY: 20 });
    fireEvent.pointerUp(pet, { pointerId: 1, screenX: 40, screenY: 20 });
    fireEvent.click(pet, { clientX: 40, clientY: 20, detail: 1 });
    fireEvent.keyDown(pet, { key: "Enter" });
    fireEvent.keyDown(pet, { key: " " });

    expect(globalThis.electronAPI.openMainWindow).toHaveBeenCalledTimes(2);
  });

  it("does not pin a renderer that initially loaded a main-app route", async () => {
    window.history.replaceState({}, "", "#view=home");
    render(<App />);
    expect(await screen.findByLabelText("输入")).toBeInTheDocument();

    setHash("#view=library");

    await waitFor(() => expect(document.querySelector('.app-router-page')).toHaveAttribute('data-page-view', 'library'));
    expect(screen.queryByRole("button", { name: /桌面宠物/ })).not.toBeInTheDocument();
  });
});
