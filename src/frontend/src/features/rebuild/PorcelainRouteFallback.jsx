// PorcelainRouteFallback.jsx — 路由懒加载占位与失败回退

import { Component, Suspense, useCallback, useEffect, useState } from "react";
import "./porcelainRouteFallback.css";

const DEFAULT_LABEL = "页面加载中";
const DEFAULT_TITLE = "正在打开这一页";
const DEFAULT_HINT = "内容即将就绪。";
const MODULE_LOAD_FAILURE = /Failed to fetch dynamically imported module|Importing a module script failed|ChunkLoadError|error loading dynamically imported module/i;

export function PorcelainRouteFallback({
  label = DEFAULT_LABEL,
  title = DEFAULT_TITLE,
  hint = DEFAULT_HINT,
}) {
  return (
    <section className="porcelain-route-fallback" role="status" aria-live="polite">
      <header className="porcelain-route-fallback__head">
        <span className="porcelain-route-fallback__label">{label}</span>
        <h2 className="porcelain-route-fallback__title">{title}</h2>
        <p className="porcelain-route-fallback__hint">{hint}</p>
      </header>
      <div className="porcelain-route-fallback__skeleton" aria-hidden="true">
        <div className="porcelain-route-fallback__rail">
          <span className="porcelain-route-fallback__chip" />
          <span className="porcelain-route-fallback__chip porcelain-route-fallback__chip--soft" />
          <span className="porcelain-route-fallback__chip porcelain-route-fallback__chip--short" />
        </div>
        <div className="porcelain-route-fallback__panel">
          <span className="porcelain-route-fallback__line porcelain-route-fallback__line--strong" />
          <span className="porcelain-route-fallback__line" />
          <span className="porcelain-route-fallback__line porcelain-route-fallback__line--short" />
        </div>
        <div className="porcelain-route-fallback__grid">
          <span />
          <span />
          <span />
        </div>
      </div>
    </section>
  );
}

/**
 * 失败回退卡片。提供"重试"按钮，可被用户键盘聚焦触发。
 * @param {{label?: string, title?: string, hint?: string, onRetry?: () => void}} props
 */
export function PorcelainRouteError({
  label = "页面加载中",
  title = "这一块内容暂时打不开",
  hint = "可能是网络或本地服务未就绪，可以稍后再试一次。",
  onRetry,
}) {
  return (
    <div className="porcelain-route-fallback" role="alert" aria-live="assertive">
      <div className="porcelain-route-fallback__card">
        <span className="porcelain-route-fallback__label">{label}</span>
        <h2 className="porcelain-route-fallback__title">{title}</h2>
        <p className="porcelain-route-fallback__hint">{hint}</p>
        {onRetry ? (
          <button
            type="button"
            className="porcelain-route-fallback__retry"
            onClick={onRetry}
          >
            重试
          </button>
        ) : null}
      </div>
    </div>
  );
}

/**
 * 错误边界：捕获懒加载组件抛出的加载失败，提供重试入口。
 * 模块下载失败时刷新页面，清除 React.lazy 缓存的失败 Promise；
 * 其他渲染错误仍通过重新挂载组件重试。
 */
export class PorcelainRouteBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, boundaryKey: 0 };
    this.lastError = null;
    this.handleRetry = this.handleRetry.bind(this);
  }

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error) {
    this.lastError = error;
    // 仅在控制台输出简短信息，不向用户暴露技术细节
    if (typeof console !== "undefined" && console.warn) {
      console.warn("route lazy load failed:", error?.message || error);
    }
  }

  handleRetry() {
    if (MODULE_LOAD_FAILURE.test(this.lastError?.message || "")) {
      (this.props.reloadPage || (() => window.location.reload()))();
      return;
    }
    this.lastError = null;
    this.setState((prev) => ({
      hasError: false,
      boundaryKey: prev.boundaryKey + 1,
    }));
  }

  render() {
    if (this.state.hasError) {
      return (
        <PorcelainRouteError
          title="这一块内容暂时打不开"
          hint="可能是网络或本地服务未就绪，可以稍后再试一次。"
          onRetry={this.handleRetry}
        />
      );
    }
    // 通过 key 重挂载普通渲染错误；失败的 lazy import 需要刷新页面。
    return (
      <div className="porcelain-route-boundary" key={this.state.boundaryKey}>
        {this.props.children}
      </div>
    );
  }
}

/**
 * 组合 Suspense + 错误边界，提供统一的懒加载包裹器。
 * @param {{children: import("react").ReactNode, label?: string, title?: string, hint?: string}} props
 */
export function PorcelainLazyRoute({ children, label, title, hint }) {
  return (
    <PorcelainRouteBoundary>
      <Suspense
        fallback={
          <PorcelainRouteFallback label={label} title={title} hint={hint} />
        }
      >
        {children}
      </Suspense>
    </PorcelainRouteBoundary>
  );
}

/**
 * 钩子：在懒加载完成时给页面一个轻微的进入提示（可选使用）。
 * 仅在 prefers-reduced-motion 未开启时生效。
 */
export function useRouteReadyAnnouncement() {
  const [ready, setReady] = useState(false);
  const announce = useCallback(() => {
    if (typeof window === "undefined") return;
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (reduce) {
      setReady(true);
      return;
    }
    // 给懒加载完成一个 80ms 的延迟，避免视觉跳动
    const timer = window.setTimeout(() => setReady(true), 80);
    return () => window.clearTimeout(timer);
  }, []);
  useEffect(() => {
    const cleanup = announce();
    return cleanup;
  }, [announce]);
  return ready;
}
