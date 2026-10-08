import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const styles = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf8");

const stateTokens = [
  "neutral-fg", "neutral-bg", "neutral-border",
  "success-fg", "success-bg", "success-border",
  "warning-fg", "warning-bg", "warning-border",
  "error-fg", "error-bg", "error-border",
  "conflict-fg", "conflict-bg", "conflict-border",
];

describe("canonical design token contract", () => {
it("defines the confirmed warm paper palette and inherited compatibility aliases", () => {
    expect(styles).toContain("--paper: #F7F4EE;");
    expect(styles).toContain("--panel: #FFFDF9;");
    expect(styles).toContain("--line: #E6E0D5;");
    expect(styles).toContain("--ink: #1C1A17;");
    expect(styles).toContain("--red: #C0282F;");

    for (const family of ["link", "file", "image", "audio"]) {
      for (const role of ["", "-soft", "-line"]) {
        expect(styles.match(new RegExp(`--cr-accent-${family}${role}:`, "g")) || [])
          .toHaveLength(1);
      }
    }
    for (const family of ["link", "file", "image", "audio"]) {
      expect(styles).toContain(`--cr-accent-${family}: var(--ink2);`);
      expect(styles).toContain(`--cr-accent-${family}-soft: var(--sunk);`);
      expect(styles).toContain(`--cr-accent-${family}-line: var(--line2);`);
    }
  });
it("defines the layout, focus, disabled and complete state scales", () => {
    for (let index = 1; index <= 7; index += 1) {
      expect(styles).toContain(`--cr-space-${index}:`);
    }
    for (const token of ["radius-sm", "radius-md", "radius-lg", "radius-xl", "radius-pill", "focus-color", "focus-ring", "disabled-opacity", "disabled-bg", "disabled-text"]) {
      expect(styles).toContain(`--cr-${token}:`);
    }
    for (const token of ["page-max-focus", "page-max-reading", "page-max-workflow", "page-max-collection", "page-title-size", "page-section-gap", "page-card-padding"]) {
      expect(styles).toContain(`--cr-${token}:`);
    }
    for (const token of stateTokens) {
      expect(styles).toContain(`--cr-state-${token}:`);
    }
  });
it("inherits state aliases from the palette and resolves explicit and system dark themes", () => {
    for (const token of stateTokens) {
      const occurrences = styles.match(new RegExp(`--cr-state-${token}:`, "g")) || [];
      expect(occurrences).toHaveLength(1);
      expect(styles).toMatch(new RegExp(`--cr-state-${token}: var\\(--[a-z0-9-]+\\);`));
    }
    expect(styles).toContain(':root[data-theme="dark"]');
    expect(styles).toContain(':root[data-theme="system"]');
    expect(styles).toContain("@media (prefers-color-scheme: dark)");
    for (const declaration of ["--paper: #1A1815;", "--panel: #221F1B;", "--ink: #ECE6DB;", "--red: #E66B6F;"]) {
      expect(styles.match(new RegExp(declaration, "g"))).toHaveLength(2);
    }
  });
});
