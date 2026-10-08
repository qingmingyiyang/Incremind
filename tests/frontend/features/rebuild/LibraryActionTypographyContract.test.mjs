import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const libraryStyles = readFileSync("src/features/rebuild/libraryOverview.css", "utf8");

function cssRule(selector) {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return libraryStyles.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, "s"))?.[1] || "";
}

const selectors = [
  ".library-overview-item-actions button",
  ".library-item-job-status-button",
  ".library-bulk-action-toolbar-info button",
  ".library-bulk-action-toolbar-actions button",
  ".library-bulk-action-confirm",
  ".library-bulk-action-input-group input",
  ".library-bulk-action-message",
  ".library-bulk-action-error",
  ".library-bulk-action-hint",
];

for (const selector of selectors) {
  const rule = cssRule(selector);
  assert.ok(rule.includes("font-family: var(--font-sans-cn)"), `${selector}: Chinese sans family`);
  assert.ok(rule.includes("font-size: 12px"), `${selector}: 12px minimum`);
  assert.ok(rule.includes("font-style: normal"), `${selector}: normal style`);
  assert.ok(rule.includes("letter-spacing: 0"), `${selector}: zero extra spacing`);
  assert.ok(rule.includes("text-transform: none"), `${selector}: no forced transform`);
}

console.log(`Library action typography contract passed (${selectors.length} selectors).`);
