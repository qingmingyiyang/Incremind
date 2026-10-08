import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
vi.mock("@src/features/workbench/Workbench", () => ({ default: () => <div>workbench</div> }));
vi.mock("@src/features/rebuild/DesktopPet", () => ({ DesktopPet: () => <div>pet</div> }));
vi.mock("@src/features/library/Library", () => ({ default: ({ projectId }) => <div>library:{projectId}</div> }));
vi.mock("@src/features/settings/SettingsPage", () => ({ default: () => <div>settings</div> }));
vi.mock("@src/features/companion/CompanionPage", () => ({ CompanionPage: ({ projectId, initialMode }) => <div>companion:{projectId}:{initialMode}</div> }));
import { WebEntry } from "@src/WebEntry";
beforeEach(() => { localStorage.clear(); vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => ({ items: [] }) }))); });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });
function navigate(hash) { act(() => { window.history.replaceState(null, "", hash || "/"); window.dispatchEvent(new Event("hashchange")); }); }
it("opens the default workbench and recognition inside the same shell", async () => {
 render(<WebEntry />); expect(await screen.findByText("workbench")).toBeInTheDocument();
 navigate("#view=recognition&project_id=verification");
 expect(await screen.findByText("library:verification")).toBeInTheDocument();
 expect(screen.getAllByRole("navigation", { name: "主导航" })).toHaveLength(1);
});
it("loads existing settings and returns to recognition", async () => {
 navigate("#view=rebuild-settings"); render(<WebEntry />); expect(await screen.findByText("settings")).toBeInTheDocument();
 navigate("#view=recognition&project_id=returning"); expect(await screen.findByText("library:returning")).toBeInTheDocument();
});
it("does not convert an initial main renderer into a pet", async () => {
 render(<WebEntry />); navigate("#view=rebuild-pet");
 expect(await screen.findByText("workbench")).toBeInTheDocument(); expect(screen.queryByText("pet")).not.toBeInTheDocument();
});
it("keeps an initial pet renderer isolated after hash changes", async () => {
 navigate("#view=rebuild-pet"); render(<WebEntry />); expect(await screen.findByText("pet")).toBeInTheDocument();
 navigate("#view=recognition"); expect(screen.getByText("pet")).toBeInTheDocument();
 expect(screen.queryByRole("navigation", { name: "主导航" })).not.toBeInTheDocument();
});
it("preserves desktop navigation and releases the one main subscription on unmount", async () => {
 let navigateFromDesktop; const unsubscribe = vi.fn();
 vi.stubGlobal("electronAPI", { subscribeMainNavigation: vi.fn(callback => { navigateFromDesktop = callback; return unsubscribe; }) });
 navigate("#view=recognition&project_id=verification"); const app = render(<WebEntry />);
 expect(await screen.findByText("library:verification")).toBeInTheDocument();
 act(() => { navigateFromDesktop({ panel: "memory", intent: "weekly_memory_review" }); window.dispatchEvent(new Event("hashchange")); });
 expect(await screen.findByText("companion:verification:review")).toBeInTheDocument();
 const params = new URLSearchParams(window.location.hash.slice(1)); expect(params.get("project_id")).toBe("verification"); expect(params.get("intent")).toBe("weekly_memory_review");
 expect(unsubscribe).not.toHaveBeenCalled(); app.unmount(); expect(unsubscribe).toHaveBeenCalledTimes(1);
});
