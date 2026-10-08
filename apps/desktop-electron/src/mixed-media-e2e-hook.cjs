const SWITCH = "--mixed-media-e2e-fixture";
const SWITCH_NAME = "mixed-media-e2e-fixture";
const TOKEN_ENV = "CHRIPTMAS_E2E_MIXED_MEDIA_RUN_TOKEN";
const HARNESS_PID_ENV = "CHRIPTMAS_E2E_MIXED_MEDIA_HARNESS_PID";
const TOKEN = /^[A-Za-z0-9_-]{32,128}$/;

function resolveMixedMediaE2E({ argv = process.argv, env = process.env, commandLineToken = null, parentPid = process.ppid } = {}) {
  const token = env[TOKEN_ENV];
  const harnessPid = env[HARNESS_PID_ENV];
  const argumentPrefix = `${SWITCH}=`;
  const argumentTokens = argv
    .filter((value) => typeof value === "string" && value.startsWith(argumentPrefix))
    .map((value) => value.slice(argumentPrefix.length));
  const switchToken = typeof commandLineToken === "string"
    ? commandLineToken
    : argumentTokens.length === 1 ? argumentTokens[0] : "";
  return argumentTokens.length <= 1
    && typeof token === "string"
    && TOKEN.test(token)
    && typeof harnessPid === "string"
    && /^[1-9][0-9]*$/.test(harnessPid)
    && Number(harnessPid) === parentPid
    && switchToken === token;
}

module.exports = { HARNESS_PID_ENV, SWITCH, SWITCH_NAME, TOKEN_ENV, resolveMixedMediaE2E };
