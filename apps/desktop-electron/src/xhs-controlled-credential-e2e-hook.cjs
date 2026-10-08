const SWITCH = "--xhs-controlled-credential-e2e";
const SWITCH_NAME = "xhs-controlled-credential-e2e";
const TOKEN_ENV = "CHRIPTMAS_E2E_XHS_CREDENTIAL_RUN_TOKEN";
const HARNESS_PID_ENV = "CHRIPTMAS_E2E_XHS_CREDENTIAL_HARNESS_PID";
const REAL_OCR_TOKEN_ENV = "CHRIPTMAS_E2E_XHS_CREDENTIAL_REAL_OCR_TOKEN";

function resolveXhsControlledCredentialE2E({
  argv = process.argv,
  env = process.env,
  parentPid = process.ppid,
  commandLineToken = null,
} = {}) {
  const supplied = commandLineToken ?? argv.find((item) => item.startsWith(`${SWITCH}=`))?.slice(SWITCH.length + 1);
  const expected = String(env[TOKEN_ENV] || "");
  const harnessPid = Number.parseInt(String(env[HARNESS_PID_ENV] || ""), 10);
  return expected.length >= 32
    && supplied === expected
    && Number.isSafeInteger(harnessPid)
    && harnessPid > 0
    && parentPid === harnessPid;
}

function resolveXhsControlledCredentialRealOcrE2E(options = {}) {
  const env = options.env ?? process.env;
  const token = options.commandLineToken
    ?? options.argv?.find((item) => item.startsWith(`${SWITCH}=`))?.slice(SWITCH.length + 1)
    ?? null;
  return resolveXhsControlledCredentialE2E({ ...options, commandLineToken: token, env })
    && String(env[REAL_OCR_TOKEN_ENV] || "") === token;
}

module.exports = { HARNESS_PID_ENV, REAL_OCR_TOKEN_ENV, SWITCH, SWITCH_NAME, TOKEN_ENV, resolveXhsControlledCredentialE2E, resolveXhsControlledCredentialRealOcrE2E };
