const SWITCH = "--plugin-hook-fault-e2e";
const SWITCH_NAME = "plugin-hook-fault-e2e";
const TOKEN_ENV = "CHRIPTMAS_E2E_PLUGIN_HOOK_FAULT_RUN_TOKEN";
const HARNESS_PID_ENV = "CHRIPTMAS_E2E_PLUGIN_HOOK_FAULT_HARNESS_PID";
const TOKEN = /^[A-Za-z0-9_-]{32,128}$/;

function resolvePluginHookFaultE2E({ argv = process.argv, env = process.env, commandLineToken = null, parentPid = process.ppid } = {}) {
  const token = env[TOKEN_ENV];
  const harnessPid = env[HARNESS_PID_ENV];
  const prefix = `${SWITCH}=`;
  const tokens = argv.filter((value) => typeof value === "string" && value.startsWith(prefix)).map((value) => value.slice(prefix.length));
  const supplied = typeof commandLineToken === "string" ? commandLineToken : tokens.length === 1 ? tokens[0] : "";
  return tokens.length <= 1 && typeof token === "string" && TOKEN.test(token)
    && typeof harnessPid === "string" && /^[1-9][0-9]*$/.test(harnessPid)
    && Number(harnessPid) === parentPid && supplied === token;
}

module.exports = { HARNESS_PID_ENV, SWITCH, SWITCH_NAME, TOKEN_ENV, resolvePluginHookFaultE2E };
