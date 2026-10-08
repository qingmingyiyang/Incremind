const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");

const desktopRoot = path.resolve(__dirname, "..");
const script = fs.readFileSync(path.join(desktopRoot, "scripts", "e2e-electron.cjs"), "utf8");
const packageJson = JSON.parse(fs.readFileSync(path.join(desktopRoot, "package.json"), "utf8"));

test("packaged public MCP Gate is explicit, isolated, reviewed, and read-only", () => {
  assert.equal(packageJson.scripts["test:e2e:packaged-public-mcp"], "node scripts/e2e-electron.cjs --packaged-public-mcp-only");
  assert.match(script, /PACKAGED_PUBLIC_MCP_ONLY = process\.argv\.includes\("--packaged-public-mcp-only"\)/);
  assert.match(script, /PACKAGED_PUBLIC_MCP_ONLY\) \{/);
  assert.match(script, /protocol_profile: "stateless_2026_07_28"/);
  assert.match(script, /tool_name: "list_docs_urls"/);
  assert.match(script, /reviewed_input_schema: inputSchema/);
  assert.match(script, /requires_approval: false/);
  assert.match(script, /remote_mutation: false/);
  assert.match(script, /packaged public MCP Gate requires the repository packaged candidate/);
});

test("Gate uses production capability and Boundary commands before one exact Tool call", () => {
  assert.match(script, /capability-selection\/confirmations/);
  assert.match(script, /\/capability-selection/);
  assert.match(script, /\/boundary-grants/);
  assert.match(script, /mode: "execute_exact_v1"/);
  assert.match(script, /tool\.attempts\?\.length !== 1/);
  assert.match(script, /tool\.receipt_available !== true/);
  assert.match(script, /methods: \["server\/discover", "tools\/list", "tools\/call"\]/);
});

test("restart proves durable Turn replay without a second remote attempt", () => {
  assert.match(script, /await closeWorkspaceSessionNormally\(session\)/);
  assert.match(script, /replay\.body\.replayed !== true/);
  assert.match(script, /projection\?\.current_sequence !== terminalSequence/);
  assert.match(script, /replayTool\?\.attempts\?\.length !== 1/);
  assert.match(script, /restart_sequence_unchanged: true/);
  assert.match(script, /restart_tool_attempts: replayTool\.attempts\.length/);
});
