import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import path from "node:path";

const css = fs.readFileSync(path.resolve(process.cwd(), "src/shared/ui/atoms.css"), "utf8");
import { StatusDot } from "@src/shared/ui/StatusDot";
import { ProgressDots } from "@src/shared/ui/ProgressDots";
import { LayerTabs } from "@src/shared/ui/LayerTabs";
import { FilterBar } from "@src/shared/ui/FilterBar";
import { Row } from "@src/shared/ui/Row";
import { Breadcrumb } from "@src/shared/ui/Breadcrumb";
import { Switch } from "@src/shared/ui/Switch";
import { Icon } from "@src/shared/ui/Icon";
import { expectNoSevereA11yViolations } from "../../a11y/axeTestUtils";

describe("shared UI atoms", () => {
  it.each([["processing", "处理中"], ["pending", "待确认"], ["done", "已沉淀"], ["failed", "失败"], ["unverified", "未核对"], ["forgotten", "已遗忘"]])("renders %s status with accessible meaning", (state, label) => {
    render(<StatusDot state={state} />);
    expect(screen.getByRole("img", { name: label })).toHaveAttribute("data-state", state);
  });
  it("updates a displayed status through its parent action", () => {
    function Example() { const [state, set] = useState("processing"); return <button onClick={() => set("done")}><StatusDot state={state} /></button>; }
    render(<Example />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByRole("img", { name: "已沉淀" })).toBeInTheDocument();
  });
  it("renders completed, running and waiting progress dots", () => {
    const { container } = render(<ProgressDots total={4} done={1} running title="原件 · 正文 · 整理 · 入库" />);
    expect(screen.getByRole("img", { name: "原件 · 正文 · 整理 · 入库" })).toBeInTheDocument();
    expect([...container.querySelectorAll("[data-progress]")].map(x => x.dataset.progress)).toEqual(["done", "running", "waiting", "waiting"]);
  });
  it("advances progress through a parent action", () => {
    function Example() { const [done, set] = useState(0); return <button onClick={() => set(1)}><ProgressDots total={3} done={done} running /></button>; }
    const { container } = render(<Example />);
    fireEvent.click(screen.getByRole("button"));
    expect(container.querySelector("[data-progress]")).toHaveAttribute("data-progress", "done");
  });
  it("renders four Chinese layers and counts", () => {
    render(<LayerTabs value="insight" counts={{ insight: 2, summary: 1 }} />);
    expect(screen.getAllByRole("button")).toHaveLength(4);
    expect(screen.getByRole("button", { name: "认识 2" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "原件 0" })).toBeInTheDocument();
  });
  it("selects layers with a native keyboard reachable button", () => {
    const change = vi.fn(); render(<LayerTabs value="insight" onChange={change} />);
    const target = screen.getByRole("button", { name: "摘要 0" }); target.focus();
    expect(target).toHaveFocus(); fireEvent.click(target); expect(change).toHaveBeenCalledWith("summary");
  });
  it("renders all four insight filters", () => {
    render(<FilterBar value="pending" counts={{ pending: 3 }} />);
    expect(screen.getByRole("button", { name: "待确认 3" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "需复核 0" })).toBeInTheDocument();
  });
  it("changes insight filter", () => {
    const change = vi.fn(); render(<FilterBar value="pending" onChange={change} />);
    fireEvent.click(screen.getByRole("button", { name: "已遗忘 0" })); expect(change).toHaveBeenCalledWith("forgotten");
  });
  it("renders a selected forgotten row with secondary text and metadata", () => {
    const { container } = render(<Row dot="forgotten" title="认识正文" sub="副文本" meta="场景 · 2 源" selected />);
    expect(screen.getByText("场景 · 2 源")).toBeInTheDocument();
    expect(container.firstChild).toHaveClass("ui-row-forgotten", "is-selected");
    expect(screen.getByRole("button", { name: "认识正文 副文本" })).toHaveAttribute("aria-pressed", "true");
  });
  it("opens row independently from trailing action", () => {
    const open = vi.fn(), file = vi.fn();
    render(<Row title="原件" onOpen={open} trailing={<button onClick={file}>归入</button>} />);
    fireEvent.click(screen.getByRole("button", { name: "归入" })); expect(file).toHaveBeenCalledOnce(); expect(open).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "原件" })); expect(open).toHaveBeenCalledOnce();
  });
  it("renders Chinese breadcrumbs with current layer", () => {
    render(<Breadcrumb current="summary" />);
    expect(screen.getByRole("navigation", { name: "下钻层级" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "摘要" })).toHaveAttribute("aria-current", "step");
  });
  it("jumps to any breadcrumb level", () => {
    const jump = vi.fn(); render(<Breadcrumb current="insight" onJump={jump} />);
    fireEvent.click(screen.getByRole("button", { name: "原件" })); expect(jump).toHaveBeenCalledWith("source");
  });
  it("renders labelled controlled switch", () => {
    render(<Switch checked label="允许发送到你的模型" />);
    expect(screen.getByRole("switch", { name: "允许发送到你的模型" })).toHaveAttribute("aria-checked", "true");
  });
  it("switches boolean value and respects disabled state", () => {
    const change = vi.fn(); const { rerender } = render(<Switch checked={false} label="模型" onChange={change} />);
    fireEvent.click(screen.getByRole("switch")); expect(change).toHaveBeenCalledWith(true);
    rerender(<Switch checked label="模型" onChange={change} disabled />);
    fireEvent.click(screen.getByRole("switch")); expect(change).toHaveBeenCalledOnce();
  });
  it("renders line icons with uniform stroke", () => {
    const { container } = render(<Icon name="arrow-up-right" />);
    expect(container.querySelector("svg")).toHaveAttribute("stroke-width", "1.6");
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });
  it("allows icon button actions without competing accessible name", () => {
    const click = vi.fn(); render(<button aria-label="关闭" onClick={click}><Icon name="close" /></button>);
    fireEvent.click(screen.getByRole("button", { name: "关闭" })); expect(click).toHaveBeenCalledOnce();
  });
  it("uses only design tokens for colors and allowed radii", () => {
    expect(css).not.toMatch(/#[\da-f]{3,8}\b/i);
    expect(css).not.toMatch(/\[data-theme/);
    expect(css).not.toMatch(/border-radius:\s*(?!var\(|999px)[\d.]+(?:px|%)/);
    expect(css).toContain("outline: 2px solid var(--red)");
  });
  it("has no serious accessibility violations for composed atoms", async () => {
    const { container } = render(<main><h1>资料库</h1><LayerTabs value="insight" /><FilterBar value="pending" /><Breadcrumb current="summary" /><Row dot="pending" title="认识正文" /><Switch label="模型" checked /><StatusDot state="failed" /><ProgressDots total={4} done={2} /></main>);
    await expectNoSevereA11yViolations(container);
  });
});
