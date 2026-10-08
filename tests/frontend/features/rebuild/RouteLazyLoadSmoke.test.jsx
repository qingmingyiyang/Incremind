import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PorcelainRouteBoundary, PorcelainRouteError, PorcelainRouteFallback, PorcelainLazyRoute } from "@src/features/rebuild/PorcelainRouteFallback";

function mockFetchOk(payload = {}) {
  return vi.fn((url) =>
    Promise.resolve({
      ok: true,
      status: 200,
      json: async () => {
        if (typeof url === "string" && url.includes("/api/providers")) return [];
        return payload;
      },
    }),
  );
}

const originalFetch = globalThis.fetch;

beforeEach(() => {
  // 重置路由到首页，避免上一个测试残留
  window.history.replaceState({}, "", "/");
  localStorage.clear();
  // 默认 mock fetch
  globalThis.fetch = mockFetchOk();
});

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

describe("Stage 8 smoke: Porcelain fallback 显示", () => {
it("renders the loading fallback card with correct ARIA semantics", () => {
    render(<PorcelainRouteFallback />);

    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    expect(region).toHaveTextContent("正在打开这一页");
    expect(region).toHaveTextContent("页面加载中");
  });
it("accepts custom label/title/hint for different routes", () => {
    render(
      <PorcelainRouteFallback
        label="LIBRARY"
        title="正在准备资料库"
        hint="加载你的记忆卡片中。"
      />,
    );

    expect(screen.getByText("LIBRARY")).toBeInTheDocument();
    expect(screen.getByText("正在准备资料库")).toBeInTheDocument();
    expect(screen.getByText("加载你的记忆卡片中。")).toBeInTheDocument();
  });
});

describe("Stage 8 smoke: 错误边界与重试", () => {
it("renders error fallback with retry button when load fails", () => {
    // 故意抛错的懒加载组件
    const BadComponent = () => {
      throw new Error("smoke load failure");
    };

    render(
      <PorcelainRouteBoundary>
        <BadComponent />
      </PorcelainRouteBoundary>,
    );

    const alert = screen.getByRole("alert");
    expect(alert).toHaveAttribute("aria-live", "assertive");
    expect(alert).toHaveTextContent("这一块内容暂时打不开");
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
  });
it("PorcelainRouteError without onRetry renders without retry button", () => {
    render(<PorcelainRouteError />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
  });
it("retry button resets error state and re-renders children", () => {
    let shouldFail = true;
    const Flaky = () => {
      if (shouldFail) throw new Error("smoke flaky");
      return <div data-testid="flaky-ok">加载成功</div>;
    };

    render(
      <PorcelainRouteBoundary>
        <Flaky />
      </PorcelainRouteBoundary>,
    );

    expect(screen.getByRole("alert")).toBeInTheDocument();

    // 修复故障后点重试
    shouldFail = false;
    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    expect(screen.getByTestId("flaky-ok")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
it("reloads the page when a dynamic import failed, instead of retrying the cached rejection", () => {
    const reloadPage = vi.fn();
    const FailedImport = () => {
      throw new TypeError("Failed to fetch dynamically imported module: http://127.0.0.1:4173/src/App.jsx");
    };

    render(
      <PorcelainRouteBoundary reloadPage={reloadPage}>
        <FailedImport />
      </PorcelainRouteBoundary>,
    );

    fireEvent.click(screen.getByRole("button", { name: "重试" }));

    expect(reloadPage).toHaveBeenCalledOnce();
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });
it("PorcelainLazyRoute wraps children with Suspense and boundary", async () => {
    render(
      <PorcelainLazyRoute>
        <span data-testid="lazy-child">内容</span>
      </PorcelainLazyRoute>,
    );

    expect(screen.getByTestId("lazy-child")).toBeInTheDocument();
  });
});

describe("Stage 8 smoke: prefers-reduced-motion", () => {
it("fallback respects reduced motion via CSS media query (smoke: class present)", () => {
    // CSS 层面的 prefers-reduced-motion 由 styles.css 全局处理 + porcelainRouteFallback.css 局部处理
    // 这里仅断言 fallback 容器有正确的 class，CSS 应用由浏览器负责
    render(<PorcelainRouteFallback />);
    const fallback = document.querySelector(".porcelain-route-fallback");
    expect(fallback).toBeInTheDocument();
    // chip 也存在（reduced-motion 下会停止动画）
    expect(document.querySelector(".porcelain-route-fallback__chip")).toBeInTheDocument();
  });
});

describe("Stage 8 smoke: 深色模式", () => {
it("applies dark mode styles via data-theme attribute (smoke: stylesheet linked)", () => {
    // 深色模式由 [data-theme="dark"] 选择器覆盖，CSS 已包含
    // 这里仅断言 fallback DOM 结构可被深色模式选择器匹配
    document.documentElement.setAttribute("data-theme", "dark");
    try {
      render(<PorcelainRouteFallback />);
      const fallback = document.querySelector(".porcelain-route-fallback");
      expect(fallback).toBeInTheDocument();
      // 深色模式下样式由 CSS 负责，这里只验证 DOM 可被 [data-theme="dark"] 选择器匹配
      expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    } finally {
      document.documentElement.removeAttribute("data-theme");
    }
  });
});
