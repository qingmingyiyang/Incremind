const os = require("node:os");
const path = require("node:path");

const TEST_SWITCH = "--companion-multicharacter-e2e";
const TEST_ENV = "CHRIPTMAS_E2E_COMPANION_MULTICHARACTER";
const TEST_DIRECTORY = "chriptmas-companion-multicharacter";
const RUN_ID_PATTERN = /^[a-f0-9]{32}$/;

function resolveMultiCharacterE2ERoot({
  argv = process.argv,
  env = process.env,
  userDataRoot,
  tempRoot = os.tmpdir(),
} = {}) {
  const runId = env?.[TEST_ENV];
  if (!Array.isArray(argv) || !argv.includes(TEST_SWITCH) || !RUN_ID_PATTERN.test(runId || "")) return null;
  if (typeof userDataRoot !== "string" || !path.isAbsolute(userDataRoot)) return null;
  if (typeof tempRoot !== "string" || !path.isAbsolute(tempRoot)) return null;

  const userData = path.resolve(userDataRoot);
  const temporary = path.resolve(tempRoot);
  const relative = path.relative(temporary, userData);
  if (!relative || path.isAbsolute(relative) || relative === ".." || relative.startsWith(`..${path.sep}`)) return null;

  return path.join(temporary, TEST_DIRECTORY, runId);
}

module.exports = {
  TEST_DIRECTORY,
  TEST_ENV,
  TEST_SWITCH,
  resolveMultiCharacterE2ERoot,
};
