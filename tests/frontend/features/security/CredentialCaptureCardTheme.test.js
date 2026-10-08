import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const styles = readFileSync(resolve(process.cwd(), "src/styles.css"), "utf8");
const credentialStyles = readFileSync(
  resolve(process.cwd(), "src/features/security/credentialCaptureCard.css"),
  "utf8",
);

describe("CredentialCaptureCard theme contract", () => {
  it("uses canonical theme tokens and a narrow-window layout", () => {
    expect(credentialStyles).not.toMatch(/#[0-9a-f]{3,8}\b|rgba?\(/i);
    expect(credentialStyles).not.toContain('[data-theme="dark"]');
    expect(credentialStyles).not.toContain('[data-theme="system"]');
    expect(credentialStyles).toContain("@media (max-width: 520px)");
    const referenced = [...credentialStyles.matchAll(/var\((--cr-[a-z0-9-]+)/g)]
      .map((match) => match[1]);
    for (const token of new Set(referenced)) {
      expect(styles, `missing canonical token ${token}`).toContain(`${token}:`);
    }
  });
});
