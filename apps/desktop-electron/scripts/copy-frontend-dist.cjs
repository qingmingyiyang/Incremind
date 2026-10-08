// 把前端 Vite build 产物从 src/frontend/dist/ 复制到 apps/desktop-electron/frontend-dist/
// 这样 electron-builder 可以把前端静态资源打包进最终 .exe
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.join(__dirname, "..", "..", "..");
const SRC_DIST = path.join(ROOT, "src", "frontend", "dist");
const DEST_DIST = path.join(__dirname, "..", "frontend-dist");

function copyDir(src, dest) {
  if (!fs.existsSync(src)) {
    throw new Error(`前端 build 产物不存在: ${src}\n请先运行 npm run build:frontend`);
  }
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, entry.name);
    const d = path.join(dest, entry.name);
    if (entry.isDirectory()) {
      copyDir(s, d);
    } else {
      fs.copyFileSync(s, d);
    }
  }
}

function main() {
  console.log("[copy-frontend-dist] 源:", SRC_DIST);
  console.log("[copy-frontend-dist] 目标:", DEST_DIST);
  if (fs.existsSync(DEST_DIST)) {
    fs.rmSync(DEST_DIST, { recursive: true, force: true });
  }
  copyDir(SRC_DIST, DEST_DIST);
  const fileCount = fs.readdirSync(DEST_DIST).length;
  console.log(`[copy-frontend-dist] 完成，复制 ${fileCount} 个顶层条目到 frontend-dist/`);
}

try {
  main();
} catch (err) {
  console.error("[copy-frontend-dist] 失败:", err.message);
  process.exit(1);
}
