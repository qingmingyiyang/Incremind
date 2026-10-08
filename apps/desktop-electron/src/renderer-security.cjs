const LOOPBACK_ORIGIN = /^http:\/\/127\.0\.0\.1:\d+$/;

function requireLoopbackOrigin(origin) {
  if (!LOOPBACK_ORIGIN.test(origin || "")) {
    throw new Error(`renderer security requires an exact loopback origin, received: ${origin || "(missing)"}`);
  }
  return origin;
}

function rendererCsp({ backendOrigin, developmentOrigin = "" } = {}) {
  const backend = requireLoopbackOrigin(backendOrigin);
  const backendWebSocket = backend.replace(/^http:/, "ws:");
  const development = developmentOrigin ? new URL(developmentOrigin).origin : "";
  const directives = [
    "default-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    // The renderer still uses dynamic React style attributes. Keep the exception
    // attribute-only until the Phase 4 style-attribute migration removes it.
    "style-src-attr 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    `connect-src 'self' ${backend} ${backendWebSocket}`,
    "media-src 'self' blob:",
    "worker-src 'self' blob:",
    "frame-src 'none'",
    "form-action 'self'",
  ];
  if (development) {
    const websocketOrigin = development.replace(/^http/, "ws");
    directives[3] = "script-src 'self' 'unsafe-eval'";
    directives[4] = "style-src 'self' 'unsafe-inline'";
    directives[8] = `connect-src 'self' ${backend} ${development} ${websocketOrigin}`;
  }
  return directives.join("; ");
}

function petRendererCsp({ developmentOrigin = "" } = {}) {
  const development = developmentOrigin ? new URL(developmentOrigin).origin : "";
  const directives = [
    "default-src 'self'",
    "base-uri 'none'",
    "object-src 'none'",
    "script-src 'self'",
    "style-src 'self'",
    "style-src-attr 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "media-src 'self' blob:",
    "worker-src 'none'",
    "frame-src 'none'",
    "form-action 'none'",
  ];
  if (development) {
    const websocketOrigin = development.replace(/^http/, "ws");
    directives[3] = "script-src 'self' 'unsafe-eval'";
    directives[4] = "style-src 'self' 'unsafe-inline'";
    directives[8] = `connect-src 'self' ${development} ${websocketOrigin}`;
  }
  return directives.join("; ");
}

function allowsNavigation(targetUrl, allowedOrigins) {
  try {
    const target = new URL(targetUrl);
    return target.protocol === "file:"
      ? allowedOrigins.has("file://")
      : allowedOrigins.has(target.origin);
  } catch {
    return false;
  }
}

function installRendererNavigationPolicy(webContents, { rendererOrigin, backendOrigin }) {
  const allowedOrigins = new Set([rendererOrigin]);
  if (backendOrigin) allowedOrigins.add(requireLoopbackOrigin(backendOrigin));
  webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  webContents.on("will-navigate", (event, targetUrl) => {
    if (!allowsNavigation(targetUrl, allowedOrigins)) event.preventDefault();
  });
}

function installRendererSessionPolicy(electronSession, { backendOrigin, developmentOrigin = "", permissionRequest = null, permissionCheck = null }) {
  const csp = rendererCsp({ backendOrigin, developmentOrigin });
  return installSessionPolicy(electronSession, csp, { permissionRequest, permissionCheck });
}

function installPetRendererSessionPolicy(electronSession, { developmentOrigin = "" } = {}) {
  const csp = petRendererCsp({ developmentOrigin });
  installSessionPolicy(electronSession, csp);
  electronSession.webRequest.onBeforeRequest((details, callback) => {
    let blocked = true;
    try {
      const target = new URL(details.url);
      blocked = target.pathname === "/api" || target.pathname.startsWith("/api/");
    } catch {
      blocked = true;
    }
    callback({ cancel: blocked });
  });
  return csp;
}

function installSessionPolicy(electronSession, csp, { permissionRequest = null, permissionCheck = null } = {}) {
  electronSession.setPermissionRequestHandler((webContents, permission, callback, details) => callback(typeof permissionRequest === "function" && permissionRequest(webContents, permission, details) === true));
  electronSession.setPermissionCheckHandler?.((webContents, permission, requestingOrigin, details) => typeof permissionCheck === "function" && permissionCheck(webContents, permission, { ...details, requestingOrigin }) === true);
  electronSession.webRequest.onHeadersReceived((details, callback) => {
    if (details.resourceType !== "mainFrame") return callback({ responseHeaders: details.responseHeaders });
    callback({
      responseHeaders: {
        ...details.responseHeaders,
        "Content-Security-Policy": [csp],
      },
    });
  });
  return csp;
}

module.exports = {
  allowsNavigation,
  installPetRendererSessionPolicy,
  installRendererNavigationPolicy,
  installRendererSessionPolicy,
  petRendererCsp,
  rendererCsp,
  requireLoopbackOrigin,
};
