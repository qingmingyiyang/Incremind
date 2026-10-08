function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    "'": "&#39;",
    '"': "&quot;",
  })[character]);
}

function startupDocument(version = "") {
  const safeVersion = escapeHtml(version);
  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Chriptmas OS 正在启动</title>
  <style>
    :root { color-scheme: light; font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; }
    * { box-sizing: border-box; }
    html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; -webkit-app-region: drag; }
    body {
      display: grid;
      place-items: center;
      color: #2d2926;
      background: radial-gradient(circle at 22% 10%, #fffaf2 0, #f7eee6 52%, #eeddd8 100%);
      user-select: none;
    }
    main { width: 100%; padding: 34px 42px 30px; text-align: center; }
    .mark {
      display: grid;
      width: 54px;
      height: 54px;
      margin: 0 auto 18px;
      place-items: center;
      border-radius: 18px;
      color: #fff8f3;
      background: #910101;
      box-shadow: 0 12px 30px rgba(145, 1, 1, .18);
      font: 700 25px Georgia, serif;
    }
    h1 { margin: 0; font: 600 25px Georgia, "Songti SC", serif; letter-spacing: .02em; }
    p { margin: 10px 0 22px; color: rgba(45, 41, 38, .68); font-size: 13px; }
    .track { width: 210px; height: 3px; margin: auto; overflow: hidden; border-radius: 99px; background: rgba(145, 1, 1, .12); }
    .bar { width: 42%; height: 100%; border-radius: inherit; background: #910101; animation: travel 1.15s ease-in-out infinite alternate; }
    small { display: block; min-height: 14px; margin-top: 18px; color: rgba(45, 41, 38, .4); font-size: 10px; letter-spacing: .08em; }
    button, a, input, select, textarea { -webkit-app-region: no-drag; }
    @keyframes travel { from { transform: translateX(-18%); } to { transform: translateX(158%); } }
    @media (prefers-reduced-motion: reduce) { .bar { width: 100%; animation: none; opacity: .65; } }
  </style>
</head>
<body>
  <main role="status" aria-live="polite">
    <div class="mark" aria-hidden="true">C</div>
    <h1>Chriptmas OS</h1>
    <p>正在唤醒你的本地记忆空间</p>
    <div class="track" aria-hidden="true"><div class="bar"></div></div>
    <small>${safeVersion ? `VERSION ${safeVersion}` : "LOCAL FIRST · PRIVATE BY DEFAULT"}</small>
  </main>
</body>
</html>`;
}

function createStartupWindow({ BrowserWindow, version = "" }) {
  if (typeof BrowserWindow !== "function") throw new TypeError("startup_window_requires_browser_window");
  const window = new BrowserWindow({
    width: 420,
    height: 260,
    minWidth: 420,
    minHeight: 260,
    maxWidth: 420,
    maxHeight: 260,
    center: true,
    frame: false,
    transparent: false,
    resizable: false,
    movable: true,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    alwaysOnTop: true,
    skipTaskbar: false,
    show: true,
    backgroundColor: "#f7eee6",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      devTools: false,
    },
  });
  window.removeMenu?.();
  window.webContents?.setWindowOpenHandler?.(() => ({ action: "deny" }));
  window.webContents?.on?.("will-navigate", (event) => event.preventDefault());
  window.loadURL(`data:text/html;charset=UTF-8,${encodeURIComponent(startupDocument(version))}`);
  return window;
}

module.exports = { createStartupWindow, escapeHtml, startupDocument };
