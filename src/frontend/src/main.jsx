import React from "react";
import ReactDOM from "react-dom/client";
import { WebEntry } from "./WebEntry";
import "./styles.css";
import "katex/dist/katex.min.css";
import { bootstrapThemeAndPerformance } from "./features/rebuild/themeBootstrap";

// The pet is a transparent native surface. Mark it before React and the theme
// bootstrap run so the first packaged frame cannot inherit the main app's
// cream page background.
const rendererView = new URLSearchParams(window.location.hash.slice(1)).get("view");
if (rendererView === "rebuild-pet") {
  document.documentElement.dataset.rendererSurface = "pet";
}

// 首帧应用主题与性能模式，避免默认白天主题闪烁。
// 必须在 createRoot().render() 之前同步执行，
// 让首帧 HTML 就带上 data-theme / data-performance 属性。
bootstrapThemeAndPerformance();

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <WebEntry />
  </React.StrictMode>,
);
