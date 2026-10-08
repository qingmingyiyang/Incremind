import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Receipt } from "@src/shared/ui/Receipt";
import { InsightChip } from "@src/shared/ui/InsightChip";
import { LayerBadges } from "@src/shared/ui/LayerBadges";
import { LadderTrace } from "@src/shared/ui/LadderTrace";
import { FocusPanel } from "@src/shared/ui/FocusPanel";
import { SourceDraftView } from "@src/shared/ui/SourceDraftView";
import { expectNoSevereA11yViolations } from "../../a11y/axeTestUtils";

const insight = { id: "i1", text: "数字更清晰", state: "pending" };
const citation = { n: 1, layer: "summary", persona: false, id: "s1", title: "视频开头", quote: "具体数字", locator: { start: 0, end: 4 } };
const trace = [{ layer: "insight", considered: 3, selected: 1, coverage: 0.3, stopped: false }, { layer: "summary", considered: 2, selected: 1, coverage: 1, stopped: true }];
const draft = { title: "视频开头", summary: "数字更清晰", facts: [{ text: "使用数字", evidence: { quote: "具体数字", start: 2, end: 6 } }], todos: ["改标题"] };

describe("memory presentation components", () => {
  it.each(["remember", "ask", "do", "inspiration"])("renders %s receipt and preserves child interaction", (kind) => {
    const action = vi.fn();
    const { container } = render(<Receipt kind={kind}><button onClick={action}>打开</button></Receipt>);
    expect(container.querySelector(`[data-kind="${kind}"]`)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "打开" }));
    expect(action).toHaveBeenCalledTimes(1);
  });
  it("renders pending insight and delegates confirm/drop without publishing locally", () => {
    const confirm = vi.fn(), drop = vi.fn();
    render(<InsightChip insight={insight} onConfirm={confirm} onDrop={drop}/>);
    expect(screen.getByText(insight.text)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "确认" }));
    fireEvent.click(screen.getByRole("button", { name: "丢弃" }));
    expect(confirm).toHaveBeenCalledWith(insight); expect(drop).toHaveBeenCalledWith(insight);
    expect(screen.getByRole("button", { name: "确认" })).toBeInTheDocument();
  });
  it("active insight has confirmed mark and no publishing controls", () => {
    render(<InsightChip insight={{ ...insight, state: "active" }}/>);
    expect(screen.getByLabelText("已确认")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "确认" })).not.toBeInTheDocument();
  });
  it("keeps genuine comment origin next to the pending relation while preserving manual review actions", () => {
    const comment = { ...insight, hint: { relation: 'supplement' }, comment_source: {
      type: 'original_item', id: 'synthetic-original', project_id: 'alpha', revision: 6,
      coordinate_space: 'workspace_source_text_v1', ordinal: 3, start: 21, end: 23, quote: '甲😀' } };
    const confirm = vi.fn(), drop = vi.fn(), view = render(<InsightChip insight={comment} onConfirm={confirm} onDrop={drop}/>);
    const marker = view.container.querySelector('.candidate-hint');
    expect(marker.firstElementChild).toHaveAttribute('data-icon', 'supplement');
    expect(marker.lastElementChild).toHaveClass('ui-comment-origin'); expect(marker.lastElementChild).toHaveTextContent('评');
    fireEvent.click(screen.getByRole('button', { name: '确认' })); fireEvent.click(screen.getByRole('button', { name: '丢弃' }));
    expect(confirm).toHaveBeenCalledExactlyOnceWith(comment); expect(drop).toHaveBeenCalledExactlyOnceWith(comment);
  });
  it("layer badges omit zero layers and open the trace", () => {
    const open = vi.fn();
    render(<LayerBadges layers={{ insight: 2, summary: 1, note: 0, source: 1, persona: 1 }} onOpenTrace={open}/>);
    expect(screen.queryByText("整 0")).not.toBeInTheDocument();
    expect(screen.getByText("我 1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "摘要 1 · 本次用了什么" }));
    expect(open).toHaveBeenCalledWith("summary");
  });
  it("ladder renders all layers, selected counts, stopping point, persona and citation navigation", () => {
    const open = vi.fn();
    render(<LadderTrace trace={trace} citations={[citation, { ...citation, n: 2, layer: "insight", persona: true, id: "me", title: "我" }]} onOpenCitation={open}/>);
    expect(screen.getByText("整理稿")).toBeInTheDocument(); expect(screen.getByText("原件")).toBeInTheDocument();
    expect(screen.getByTitle("已足够")).toBeInTheDocument();
    const summary = screen.getByRole("listitem", { name: "摘要" });
    expect(within(summary).getByText("1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "视频开头 · 具体数字" }));
    expect(open).toHaveBeenCalledWith(citation);
    expect(screen.getByLabelText("画像")).toBeInTheDocument();
  });
  it("focus panel renders title/actions and closes with button or Escape", () => {
    const close = vi.fn(), verify = vi.fn();
    render(<FocusPanel title="整理稿" status="unverified" actions={<button onClick={verify}>核对完成</button>} onClose={close}>正文</FocusPanel>);
    expect(screen.getByRole("dialog", { name: "整理稿" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "核对完成" })); expect(verify).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" }); expect(close).toHaveBeenCalledTimes(2);
  });
  it("focus panel traps keyboard focus and restores the opener", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: true, addEventListener() {}, removeEventListener() {} }));
    const opener = document.createElement("button"); document.body.append(opener); opener.focus();
    const { unmount } = render(<FocusPanel title="成果" onClose={() => {}}><button>查看</button></FocusPanel>);
    const first = screen.getByRole("button", { name: "关闭" }), last = screen.getByRole("button", { name: "查看" });
    expect(first).toHaveFocus(); last.focus(); fireEvent.keyDown(last, { key: "Tab" }); expect(first).toHaveFocus();
    fireEvent.keyDown(first, { key: "Tab", shiftKey: true }); expect(last).toHaveFocus();
    unmount(); expect(opener).toHaveFocus(); opener.remove(); vi.unstubAllGlobals();
  });
  it("desktop focus panel permits keyboard access to the surrounding page", () => {
    vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
    render(<FocusPanel title="成果" onClose={() => {}}><button>查看</button></FocusPanel>);
    expect(screen.getByRole("dialog")).not.toHaveAttribute("aria-modal");
    const last = screen.getByRole("button", { name: "查看" }); last.focus();
    const event = new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }); last.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    vi.unstubAllGlobals();
  });
  it("source/draft renders facts and highlights the exact quoted occurrence", () => {
    const { container } = render(<SourceDraftView source="先用具体数字" draft={draft}/>);
    expect(screen.getByText("改标题")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "定位原文：使用数字" }));
    expect(container.querySelector("mark")).toHaveTextContent("具体数字");
  });
  it("source/draft adds an in-source selected sentence through the existing evidence helper", () => {
    const add = vi.fn();
    const { container } = render(<SourceDraftView source="先用具体数字" draft={draft} onAddFact={add}/>);
    const node = container.querySelector(".ui-source-text").firstChild;
    const range = document.createRange(); range.setStart(node, 2); range.setEnd(node, 6);
    const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    fireEvent.click(screen.getByRole("button", { name: "加入事实" }));
    expect(add).toHaveBeenCalledWith({ text: "具体数字", evidence: { quote: "具体数字", start: 2, end: 6 } });
    selection.removeAllRanges();
  });
  it("source/draft ignores outside or cross-line selections and delegates conflict choices", () => {
    const add = vi.fn(), choose = vi.fn();
    render(<SourceDraftView source="第一行\n第二行" draft={draft} onAddFact={add} conflict={{ baseDraft: { title: "旧" }, editor: { title: "本机" }, server: { title: "服务端" }, fields: [["title", "标题"]], onChoose: choose }}/>);
    fireEvent.click(screen.getByRole("button", { name: "加入事实" })); expect(add).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "采用服务器版本" })); expect(choose).toHaveBeenCalledWith(true);
  });
  it("memory components have zero serious/critical accessibility violations", async () => {
    const { container } = render(<main><Receipt kind="remember"><InsightChip insight={insight} onConfirm={() => {}} onDrop={() => {}}/></Receipt><LayerBadges layers={{ insight: 2 }} onOpenTrace={() => {}}/><LadderTrace trace={trace} citations={[citation]} onOpenCitation={() => {}}/><FocusPanel title="整理稿" onClose={() => {}}><SourceDraftView source="先用具体数字" draft={draft} onAddFact={() => {}}/></FocusPanel></main>);
    await expectNoSevereA11yViolations(container);
  });
});
