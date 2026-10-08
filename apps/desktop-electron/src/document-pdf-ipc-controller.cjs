"use strict";

const crypto = require("node:crypto");
const { currentSessionForRequest } = require("./desktop-session.cjs");
const { SESSION_HEADER } = require("./sidecar-supervisor.cjs");

const CHANNEL = "chriptmas:document-pdf-generate";
const DELIVERY_ID = /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/;
const OPERATION_ID = DELIVERY_ID;
const PROFILE_ID = "builtin.a4-document";
const SLIDE_PROFILE_ID = "builtin.slide-document";
const CLAIM_HEADER = "X-Chriptmas-Pdf-Claim";
const MAIN_SIGNATURE_HEADER = "X-Chriptmas-Main-Signature";
const MAX_HTML_BYTES = 8 * 1024 * 1024;
const MAX_PDF_BYTES = 32 * 1024 * 1024;
const PRINT_OPTIONS = Object.freeze({
  pageSize: "A4",
  landscape: false,
  printBackground: true,
  preferCSSPageSize: false,
  displayHeaderFooter: false,
  margins: Object.freeze({ top: 0.5, bottom: 0.5, left: 0.55, right: 0.55 }),
});
const SLIDE_PRINT_OPTIONS = Object.freeze({
  pageSize: "A4",
  landscape: true,
  printBackground: true,
  preferCSSPageSize: true,
  displayHeaderFooter: false,
  margins: Object.freeze({ top: 0, bottom: 0, left: 0, right: 0 }),
});

function frozenProfileMatches(profile) {
  const options = profile?.print_options;
  const margins = options?.margins;
  return exactKeys(profile, ["electron_major", "print_options", "profile_id", "renderer_id", "renderer_revision", "revision"])
    && (profile.profile_id === PROFILE_ID || profile.profile_id === SLIDE_PROFILE_ID)
    && profile?.revision === 1
    && profile?.renderer_id === "electron.webcontents.print-to-pdf"
    && profile?.renderer_revision === 1
    && profile?.electron_major === 43
    && exactKeys(options, ["displayHeaderFooter", "landscape", "margins", "pageSize", "preferCSSPageSize", "printBackground"])
    && options.pageSize === "A4"
    && options.printBackground === true
    && options.displayHeaderFooter === false
    && exactKeys(margins, ["bottom", "left", "right", "top"])
    && (
      (profile.profile_id === PROFILE_ID
        && options.landscape === false
        && options.preferCSSPageSize === false
        && margins.top === 0.5 && margins.bottom === 0.5
        && margins.left === 0.55 && margins.right === 0.55)
      || (profile.profile_id === SLIDE_PROFILE_ID
        && options.landscape === true
        && options.preferCSSPageSize === true
        && margins.top === 0 && margins.bottom === 0
        && margins.left === 0 && margins.right === 0)
    );
}

function exactKeys(value, expected) {
  return value && typeof value === "object" && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify(expected);
}

function safeSession(session) {
  if (!session || typeof session.origin !== "string" || typeof session.secret !== "string" || typeof session.instance_id !== "string" || !session.instance_id) return null;
  try {
    const origin = new URL(session.origin);
    return origin.protocol === "http:" && origin.hostname === "127.0.0.1" && origin.origin === session.origin
      ? { origin: origin.origin, secret: session.secret, instance_id: session.instance_id }
      : null;
  } catch { return null; }
}

class DocumentPdfIpcController {
  constructor({ ipcMain, BrowserWindow, requireMainRenderer, sessionProvider, versionsProvider = () => process.versions,
    fetchImpl = fetch, cryptoApi = crypto, sessionHeader = SESSION_HEADER,
    timeoutSignal = (milliseconds) => AbortSignal.timeout(milliseconds) } = {}) {
    if (!ipcMain || typeof ipcMain.handle !== "function" || typeof ipcMain.removeHandler !== "function"
      || typeof BrowserWindow !== "function" || typeof requireMainRenderer !== "function"
      || typeof sessionProvider !== "function" || typeof versionsProvider !== "function"
      || typeof fetchImpl !== "function" || !cryptoApi?.createHmac || typeof sessionHeader !== "string") {
      throw new TypeError("document_pdf_controller_invalid");
    }
    this.ipcMain = ipcMain; this.BrowserWindow = BrowserWindow; this.requireMainRenderer = requireMainRenderer;
    this.sessionProvider = sessionProvider; this.versionsProvider = versionsProvider; this.fetch = fetchImpl;
    this.crypto = cryptoApi; this.sessionHeader = sessionHeader; this.timeoutSignal = timeoutSignal;
    this.installed = false; this.disposed = false; this.activeWindow = null; this.inflight = new Map();
  }

  install() {
    if (this.installed) return false;
    this.ipcMain.handle(CHANNEL, (event, value) => this.generate(event, value));
    this.installed = true; this.disposed = false; return true;
  }

  async generate(event, value = {}) {
    this.requireMainRenderer(event);
    const deliveryId = typeof value?.htmlDeliveryId === "string" ? value.htmlDeliveryId.trim() : "";
    if (!DELIVERY_ID.test(deliveryId)) return { status: "rejected", reason: "html_delivery_id_invalid" };
    const projectId = value?.projectId == null || value.projectId === "" ? "default" : value.projectId;
    if (typeof projectId !== "string" || !DELIVERY_ID.test(projectId)) {
      return { status: "rejected", reason: "project_id_invalid" };
    }
    const projectQuery = projectId === "default" ? "" : `?project_id=${encodeURIComponent(projectId)}`;
    const active = safeSession(this.sessionProvider());
    if (!active) return { status: "unavailable", reason: "sidecar_not_ready" };
    const results = {};
    for (const [kind, profileId] of [["document", PROFILE_ID], ["slides", SLIDE_PROFILE_ID]]) {
      try {
        const prepared = await this.#json(active, "POST", `/api/rebuild/document-deliveries/${encodeURIComponent(deliveryId)}/pdf-operations${projectQuery}`, { profile_id: profileId }, false);
        const operationId = prepared?.operation_id;
        if (!OPERATION_ID.test(operationId || "")) throw new Error("document_pdf_prepare_invalid");
        results[kind] = await this.process(operationId, active);
      } catch (error) {
        const reason = error?.message || "document_pdf_generate_failed";
        console.warn(`[document-pdf] ${kind} generate failed: ${reason}`);
        results[kind] = { status: "waiting_for_electron", reason };
      }
    }
    const documentResult = results.document || {};
    const slideResult = results.slides || {};
    return {
      status: documentResult.status === "completed" && slideResult.status === "completed"
        ? "completed" : "waiting_for_electron",
      operation_id: documentResult.operation_id,
      slide_operation_id: slideResult.operation_id,
      operations: results,
    };
  }

  async recoverWaiting() {
    const active = safeSession(this.sessionProvider());
    if (!active || this.disposed) return { attempted: 0, completed: [] };
    const payload = await this.#json(active, "GET", "/api/rebuild/desktop/document-pdf-operations/waiting?limit=8", undefined, true);
    const ids = Array.isArray(payload?.operations) ? payload.operations.map((item) => item?.operation_id).filter((id) => OPERATION_ID.test(id)).slice(0, 8) : [];
    const completed = [];
    for (const id of ids) {
      try { const result = await this.process(id, active); if (result.status === "completed") completed.push(id); } catch {}
    }
    return { attempted: ids.length, completed };
  }

  process(operationId, activeSession = safeSession(this.sessionProvider())) {
    if (!OPERATION_ID.test(operationId || "")) return Promise.reject(new Error("document_pdf_operation_id_invalid"));
    if (!activeSession) return Promise.reject(new Error("sidecar_not_ready"));
    if (this.inflight.has(operationId)) return this.inflight.get(operationId);
    const task = this.#processOnce(activeSession, operationId).finally(() => this.inflight.delete(operationId));
    this.inflight.set(operationId, task); return task;
  }

  async #processOnce(active, operationId) {
    const versions = this.versionsProvider();
    const claim = await this.#json(active, "POST", `/api/rebuild/desktop/document-pdf-operations/${encodeURIComponent(operationId)}/claim`, {
      electron_version: String(versions.electron || ""), chrome_version: String(versions.chrome || ""),
    }, true);
    if (claim?.status === "completed") return { status: "completed", operation_id: operationId, replayed: true };
    if (!frozenProfileMatches(claim?.profile)) throw new Error("document_pdf_profile_drifted");
    const claimToken = claim?.claim_token;
    if (typeof claimToken !== "string" || claimToken.length < 24) throw new Error("document_pdf_claim_invalid");
    const htmlPath = `/api/rebuild/desktop/document-pdf-operations/${encodeURIComponent(operationId)}/print-input`;
    const input = await this.#json(active, "GET", htmlPath, undefined, true, 10000, { [CLAIM_HEADER]: claimToken });
    const html = typeof input?.html === "string" ? Buffer.from(input.html, "utf8") : Buffer.alloc(0);
    if (html.length === 0 || html.length > MAX_HTML_BYTES) throw new Error("document_pdf_input_invalid");
    const pdf = await this.#print(html, claim.profile);
    if (!Buffer.isBuffer(pdf) || pdf.length < 5 || pdf.length > MAX_PDF_BYTES || pdf.subarray(0, 5).toString("ascii") !== "%PDF-") {
      throw new Error("document_pdf_output_invalid");
    }
    const completed = await this.#json(active, "POST", `/api/rebuild/desktop/document-pdf-operations/${encodeURIComponent(operationId)}/complete`, {
      electron_version: String(versions.electron || ""), chrome_version: String(versions.chrome || ""),
      pdf_base64: pdf.toString("base64"),
    }, true, 120000, { [CLAIM_HEADER]: claimToken });
    return { status: completed?.status || "completed", operation_id: operationId, replayed: completed?.replayed === true };
  }

  async #print(html, profile) {
    if (this.disposed) throw new Error("document_pdf_controller_disposed");
    const slideProfile = profile?.profile_id === SLIDE_PROFILE_ID;
    const window = new this.BrowserWindow({ show: false, skipTaskbar: true,
      width: slideProfile ? 1280 : 794, height: slideProfile ? 720 : 1123,
      webPreferences: { partition: "chriptmas-document-print", contextIsolation: true, nodeIntegration: false, sandbox: true, devTools: false } });
    this.activeWindow = window;
    try {
      window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      const csp = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:">`;
      const text = html.toString("utf8").replace(/<head(\s[^>]*)?>/i, (match) => `${match}${csp}`);
      await window.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(text)}`);
      window.webContents.on("will-navigate", (event) => event.preventDefault());
      return await window.webContents.printToPDF(slideProfile ? SLIDE_PRINT_OPTIONS : PRINT_OPTIONS);
    } finally {
      if (!window.isDestroyed()) window.destroy();
      if (this.activeWindow === window) this.activeWindow = null;
    }
  }

  #headers(active, method, pathname, extra = {}) {
    const signature = this.crypto.createHmac("sha256", active.secret).update(`document-pdf:${method}:${pathname}`).digest("hex");
    return { Accept: "application/json", [this.sessionHeader]: active.secret, [MAIN_SIGNATURE_HEADER]: signature, ...extra };
  }

  async #json(active, method, pathname, body, mainOnly, timeoutMs = 10000, extraHeaders = {}) {
    const requestSession = currentSessionForRequest(this.sessionProvider, active, "sidecar_not_ready");
    const headers = mainOnly ? this.#headers(requestSession, method, pathname, extraHeaders) : { Accept: "application/json", [this.sessionHeader]: requestSession.secret, ...extraHeaders };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await this.fetch(`${requestSession.origin}${pathname}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal: this.timeoutSignal(timeoutMs) });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(payload?.reason || payload?.detail || `document_pdf_http_${response.status}`);
    return payload;
  }

  dispose() {
    if (!this.installed) return false;
    this.disposed = true;
    if (this.activeWindow && !this.activeWindow.isDestroyed()) this.activeWindow.destroy();
    this.activeWindow = null; this.ipcMain.removeHandler(CHANNEL); this.installed = false; return true;
  }
}

module.exports = {
  CHANNEL, DocumentPdfIpcController, PRINT_OPTIONS, PROFILE_ID,
  SLIDE_PRINT_OPTIONS, SLIDE_PROFILE_ID, frozenProfileMatches,
};
