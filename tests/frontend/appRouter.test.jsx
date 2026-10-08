import { readFileSync } from "node:fs";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
vi.mock("@src/features/workbench/Workbench", () => ({ default: (props) => <div data-testid="page">Workbench:{props.projectId}:{props.turnId}:{props.threadId}</div> }));
vi.mock("@src/features/settings/SettingsPage", () => ({ default: props => <div data-testid="page">SettingsPage:{props.projectId}:{props.initialSection}:{props.expandProject}</div> }));
vi.mock("@src/features/library/Library", () => ({ default: props => <div data-testid="page">Library:{props.projectId}:{props.documentId}:{props.initialLayer}</div> }));
vi.mock("@src/features/companion/CompanionPage", () => ({ CompanionPage: props => <div data-testid="page">CompanionPage:{props.projectId}</div> }));
vi.mock("@src/features/rebuild/DesktopPet", () => ({ DesktopPet: (props) => <div data-testid="page">DesktopPet:{props.projectId || ""}{props.taskRef || ""}{props.turnId || ""}</div> }));
vi.mock("@src/shared/api/workspaceApi", () => ({ workspaceApi: { list: vi.fn(async () => ({ items: [{ id: "item-1", status: "failed" }] })), retry: vi.fn(async () => ({ status: "staged" })) } }));
import { workspaceApi } from "@src/shared/api/workspaceApi";
import { AppRouter } from "@src/AppRouter";
const projects = [{ id: "alpha", name: "甲" }, { id: "beta", name: "乙" }];
let jobs;
beforeEach(() => {
  localStorage.clear(); localStorage.setItem("chriptmas-os-developer-mode", "true"); jobs = [];
  window.history.replaceState(null, "", "/#view=workbench&project_id=alpha");
  vi.stubGlobal("fetch", vi.fn(async (url) => ({ ok: true, json: async () => String(url).includes("/api/v2/projects") ? { items: projects } : { items: jobs } })));
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); window.history.replaceState(null, "", "/"); });
function navigate(hash) { act(() => { window.history.replaceState(null, "", hash); window.dispatchEvent(new Event("hashchange")); }); }
it('retries a failed backup through the original tray', async () => {
  jobs = [{ id: 'snap-failed', title: '备份失败', state: 'failed', target: { type: 'backup', id: 'snap-failed' } }];
  render(<AppRouter/>); await act(async () => {});
  fireEvent.click(screen.getByRole('button', { name: /进度/ }));
  fireEvent.click(screen.getByRole('button', { name: '重试' })); await act(async () => {});
  expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/v2/jobs/snap-failed/retry'), expect.objectContaining({ method: 'POST' }));
});
const routes = [
 ["workbench", "Workbench"],
 ["home", "Workbench"],
 ["workspace", "Workbench"],
 ["library", "Library"],
 ["settings", "SettingsPage"],
 ["companion", "CompanionPage"],
 ["recognition", "Library"],
 ["rebuild-library-overview", "Library"],
 ["rebuild-library-tag-filter", "Workbench"],
 ["rebuild-whitebox-export", "Workbench"],
 ["rebuild-four-layer-memory", "Workbench"],
 ["rebuild-media-workflow", "Workbench"],
 ["rebuild-settings", "SettingsPage"],
 ["rebuild-model-selection", "SettingsPage"],
 ["rebuild-agent-organization", "Workbench"],
 ["rebuild-companion", "CompanionPage"],
 ["rebuild-developer-studio", "Workbench"],
 ["rebuild-video-detail", "Library"],
 ["rebuild-video-workflow", "Library"],
 ["rebuild-project-skill-overview", "Workbench"],
 ["rebuild-project-brain", "Library"],
 ["rebuild-local-vault", "Workbench"],
 ["rebuild-provider-checkup", "Workbench"],
 ["rebuild-self-use-alpha", "Workbench"],
 ["rebuild-self-use-alpha-readiness", "Workbench"],
 ["rebuild-task-center", "Workbench"],
 ["rebuild-world-intelligence", "Workbench"],
 ["rebuild-task-detail", "Workbench"],
 ["legacy-intake", "Workbench"],
 ["rebuild-contract", "Workbench"],
 ["rebuild-workbench-intake", "Workbench"],
];
it.each(routes)("renders %s through exactly one real shared Shell", async (view, page) => {
 navigate(`#view=${view}&project_id=alpha`); render(<AppRouter />); await act(async () => {});
 expect((await screen.findByTestId("page")).textContent.split(':')[0]).toBe(page);
 expect(document.querySelectorAll(".ui-shell")).toHaveLength(1);
 expect(screen.getAllByRole("navigation", { name: "主导航" })).toHaveLength(1);
 expect(within(screen.getByRole("navigation", { name: "主导航" })).getAllByRole("button")).toHaveLength(3);
 expect(document.querySelector(".rebuild-home-shell")).toBeNull();
});
it("defaults to workbench and restores project before rendering hash-reading pages", async () => {
 localStorage.setItem("chriptmas-v2-project-id", "beta"); navigate("/"); render(<AppRouter />); await act(async () => {});
 expect(await screen.findByTestId("page")).toHaveTextContent("Workbench");
 expect(new URLSearchParams(window.location.hash.slice(1)).get("project_id")).toBe("beta");
});
it("switches projects preserving page options and removing old object targets", async () => {
 navigate("#view=settings&project_id=alpha&task=privacy&filter=pending&item_id=old&task_ref=task:old&turn_id=old&intent=review"); render(<AppRouter />); await act(async () => {});
 fireEvent.click(await screen.findByRole("button", { name: "切换项目" }));
 fireEvent.click(await screen.findByRole("option", { name: "乙" }));
 const params = new URLSearchParams(window.location.hash.slice(1));
 expect(params.get("project_id")).toBe("beta"); expect(params.get("task")).toBe("privacy"); expect(params.get("filter")).toBe("pending"); expect(params.get("intent")).toBe("review");
 for (const key of ["item_id", "task_ref", "turn_id"]) expect(params.has(key)).toBe(false);
 expect(localStorage.getItem("chriptmas-v2-project-id")).toBe("beta");
});
it("renders with inaccessible localStorage", async () => {
 const getter = vi.spyOn(window, "localStorage", "get").mockImplementation(() => { throw new Error("blocked"); });
 render(<AppRouter />); await act(async () => {}); expect(await screen.findByTestId("page")).toHaveTextContent("Workbench"); getter.mockRestore();
});
it("opens intake and do receipts in their threads and old documents in the library", async () => {
 jobs = [{ id: "i", title: "旧材料", state: "failed", target: { type: "document", id: "doc-1" } }, { id: "t", title: "干活", state: "pending", target: { type: "turn", id: "task-turn", thread_id: "task-thread", turn_id: "task-turn" } }, { id: "u", title: "记住", state: "processing", target: { type: "turn", id: "turn-1", thread_id: "thread-1", turn_id: "turn-1" } }];
 render(<AppRouter />); await act(async () => {}); fireEvent.click(screen.getByRole("button", { name: "进度" }));
 fireEvent.click(await screen.findByRole("button", { name: "旧材料" }));
 expect(new URLSearchParams(window.location.hash.slice(1)).get("view")).toBe("library");
 expect(new URLSearchParams(window.location.hash.slice(1)).get("document_id")).toBe("doc-1");
 expect(await screen.findByTestId('page')).toHaveTextContent('Library:alpha:doc-1:note');
 fireEvent.click(screen.getByRole("button", { name: "进度" }));
 fireEvent.click(screen.getByRole("button", { name: "干活" }));
 expect(new URLSearchParams(window.location.hash.slice(1)).get("view")).toBe("workbench");
 expect(new URLSearchParams(window.location.hash.slice(1)).get("turn_id")).toBe("task-turn");
 expect(new URLSearchParams(window.location.hash.slice(1)).get("thread_id")).toBe("task-thread");
 fireEvent.click(screen.getByRole("button", { name: "进度" }));
 fireEvent.click(screen.getByRole("button", { name: "记住" }));
 expect(new URLSearchParams(window.location.hash.slice(1)).get("turn_id")).toBe("turn-1");
 expect(new URLSearchParams(window.location.hash.slice(1)).get("thread_id")).toBe("thread-1");
 expect(new URLSearchParams(window.location.hash.slice(1)).get("view")).toBe("workbench");
});

it("polls processing jobs at 1.5 seconds and settled jobs at 15 seconds", async () => {
 vi.useFakeTimers(); jobs = [{ id: "i", state: "processing", title: "进行中" }]; render(<AppRouter />); await act(async () => {}); await act(async () => {});
 const calls = () => fetch.mock.calls.filter(([url]) => String(url).includes("/api/v2/jobs")).length;
 expect(calls()).toBe(1);
 await act(async () => { await vi.advanceTimersByTimeAsync(1499); }); expect(calls()).toBe(1);
 jobs = []; await act(async () => { await vi.advanceTimersByTimeAsync(1); }); expect(calls()).toBe(2);
 await act(async () => { await vi.advanceTimersByTimeAsync(14999); }); expect(calls()).toBe(2);
 await act(async () => { await vi.advanceTimersByTimeAsync(1); }); expect(calls()).toBe(3);
});
it("aborts old project requests and ignores late results", async () => {
 let completeAlpha; let alphaSignal;
 fetch.mockImplementation((url, options) => String(url).includes("/projects") ? Promise.resolve({ ok: true, json: async () => ({ items: projects }) })
   : String(url).includes("project_id=alpha") ? new Promise(resolve => { alphaSignal = options.signal; completeAlpha = resolve; })
   : Promise.resolve({ ok: true, json: async () => ({ items: [{ id: "b", title: "乙材料", state: "pending" }] }) }));
 render(<AppRouter />); await act(async () => {}); fireEvent.click(screen.getByRole("button", { name: "进度" })); navigate("#view=workbench&project_id=beta");
 expect(await screen.findByRole("button", { name: "乙材料" })).toBeInTheDocument(); expect(alphaSignal.aborted).toBe(true);
 await act(async () => { completeAlpha({ ok: true, json: async () => ({ items: [{ id: "a", title: "旧材料", state: "failed" }] }) }); });
 expect(screen.queryByRole("button", { name: "旧材料" })).not.toBeInTheDocument();
});
it.each(["failed", "ready"])("opens %s intake using only its supported retry action", async status => {
 workspaceApi.retry.mockClear(); workspaceApi.list.mockResolvedValue({ items: [{ id: "item-1", status }] });
 jobs = [{ id: "i", state: "failed", title: "材料", target: { type: "item", id: "item-1" } }];
 render(<AppRouter />); await act(async () => {}); fireEvent.click(screen.getByRole("button", { name: "进度" })); fireEvent.click(await screen.findByRole("button", { name: "重试" }));
 await act(async () => {});
 if (status === "failed") expect(workspaceApi.retry).toHaveBeenCalledWith("alpha", "item-1"); else expect(workspaceApi.retry).not.toHaveBeenCalled();
 expect(new URLSearchParams(window.location.hash.slice(1)).get("view")).toBe("library");
 expect(new URLSearchParams(window.location.hash.slice(1)).get("layer")).toBe("note");
});
it("retries a v2 turn and opens its exact workbench thread", async () => {
 jobs = [{ id: 'turn-job', title: '材料', state: 'failed', target: { type: 'turn', id: 'turn-1' } }];
 fetch.mockImplementation(async (url) => ({ ok: true, json: async () => String(url).includes('/retry') ? { id: 'turn-1', thread_id: 'thread-1' } : String(url).includes('/projects') ? { items: projects } : { items: jobs } }));
 render(<AppRouter/>); await act(async () => {}); fireEvent.click(screen.getByRole('button', { name: '进度' })); fireEvent.click(await screen.findByRole('button', { name: '重试' })); await act(async () => {});
 const retry = fetch.mock.calls.find(([url]) => String(url).includes('/api/v2/workbench/turns/turn-1/retry'));
 expect(JSON.parse(retry[1].body)).toEqual({ project_id: 'alpha' });
 const params = new URLSearchParams(window.location.hash.slice(1)); expect(params.get('view')).toBe('workbench'); expect(params.get('thread_id')).toBe('thread-1'); expect(params.get('turn_id')).toBe('turn-1');
});
it("opens retired global review in the selected project's library", async () => {
 localStorage.setItem("chriptmas-v2-project-id", "alpha"); navigate("#view=rebuild-library-overview&filter=pending_memory"); render(<AppRouter />); await act(async () => {});
 expect(new URLSearchParams(window.location.hash.slice(1)).get("project_id")).toBe("alpha");
 expect(await screen.findByTestId("page")).toHaveTextContent("Library:alpha");
 expect(screen.queryByRole("option", { name: "全部项目" })).not.toBeInTheDocument();
 fireEvent.click(screen.getByRole("button", { name: "切换项目" })); fireEvent.click(await screen.findByRole("option", { name: "乙" }));
 expect(new URLSearchParams(window.location.hash.slice(1)).get("project_id")).toBe("beta");
});
it("preserves failure status when the intake retry request fails", async () => {
 workspaceApi.list.mockResolvedValue({ items: [{ id: "item-1", status: "failed" }] }); workspaceApi.retry.mockRejectedValueOnce(new Error("offline"));
 jobs = [{ id: "i", state: "failed", title: "材料", target: { type: "item", id: "item-1" } }];
 render(<AppRouter />); await act(async () => {}); fireEvent.click(screen.getByRole("button", { name: "进度" })); fireEvent.click(await screen.findByRole("button", { name: "重试" }));
 expect(await screen.findByRole("button", { name: "重试未完成 · 刷新" })).toBeInTheDocument();
 expect(new URLSearchParams(window.location.hash.slice(1)).get("view")).toBe("workbench");
});

it("opens retired developer entries in the workbench", async () => {
 localStorage.setItem("chriptmas-os-developer-mode", "false"); navigate("#view=rebuild-contract&project_id=alpha"); render(<AppRouter />); await act(async () => {});
 expect(await screen.findByTestId("page")).toHaveTextContent("Workbench:alpha");
 expect(screen.queryByLabelText("开发者模式未开启")).not.toBeInTheDocument();
});

it("uses the configured base for avatar assets and only tokens in router styles", async () => {
 render(<AppRouter />); await act(async () => {});
 expect(screen.getByRole("button", { name: "伙伴", exact: true }).querySelector("img").getAttribute("src")).toBe(`${import.meta.env.BASE_URL}mascots/bear-head-ready.webp`);
 const css = readFileSync("src/AppRouter.css", "utf8");
 expect(css).not.toMatch(/#[0-9a-f]{3,8}\b/i); expect(css).not.toContain('[data-theme="dark"]');
});

it("opens retired companion panels in the four-mode companion", async () => {
  navigate("#view=rebuild-companion&project_id=alpha&panel=character");
  render(<AppRouter/>);
  expect(await screen.findByTestId("page")).toHaveTextContent("CompanionPage:alpha");
});

it("backs off failed jobs to 30 seconds and resets after recovery", async () => {
 vi.useFakeTimers(); let failed = true; const times = [];
 fetch.mockImplementation(async url => {
   if (String(url).includes('/projects')) return { ok: true, json: async () => ({ items: projects }) };
   times.push(Date.now());
   return failed ? { ok: false, status: 502, json: async () => { throw new SyntaxError('Unexpected end of JSON input'); } }
     : { ok: true, json: async () => ({ items: [{ id: 'job', state: 'processing', title: '材料' }] }) };
 });
 render(<AppRouter />); await act(async () => {});
 expect(times).toHaveLength(1);
 for (const delay of [1000, 2000, 4000, 8000, 16000, 30000, 30000]) {
   const count = times.length;
   await act(async () => { await vi.advanceTimersByTimeAsync(delay - 1); });
   expect(times).toHaveLength(count);
   await act(async () => { await vi.advanceTimersByTimeAsync(1); });
   expect(times).toHaveLength(count + 1);
   expect(times.at(-1) - times.at(-2)).toBe(delay);
 }
 expect(screen.getByText('进度暂不可用 · 重试')).toBeInTheDocument();
 expect(document.body.textContent).not.toMatch(/Unexpected|JSON|Bad Gateway/);
 failed = false;
 await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
 expect(screen.queryByText('进度暂不可用 · 重试')).not.toBeInTheDocument();
 const recovered = times.length;
 await act(async () => { await vi.advanceTimersByTimeAsync(1499); }); expect(times).toHaveLength(recovered);
 await act(async () => { await vi.advanceTimersByTimeAsync(1); }); expect(times).toHaveLength(recovered + 1);
 failed = true;
 await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
 const again = times.length;
 await act(async () => { await vi.advanceTimersByTimeAsync(1000); }); expect(times).toHaveLength(again + 1);
});

it('marks the companion avatar instead of settings on the companion page', async () => {
 navigate('#view=companion&project_id=alpha'); render(<AppRouter />); await act(async () => {});
 expect(screen.getByRole('button', { name: '伙伴', exact: true })).toHaveAttribute('aria-current', 'page');
 expect(screen.getByRole('button', { name: '设置', exact: true })).not.toHaveAttribute('aria-current');
});
