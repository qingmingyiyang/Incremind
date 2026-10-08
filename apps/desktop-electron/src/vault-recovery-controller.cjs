const path = require("node:path");
const { spawn } = require("node:child_process");

const OPERATION_ID = /^[a-z0-9][a-z0-9._-]{0,95}$/;

class VaultRecoveryController {
  constructor({
    pythonPath,
    moduleRoot,
    workingRoot,
    sidecarOffline,
    spawnChild = spawn,
  }) {
    if (![pythonPath, moduleRoot, workingRoot].every((value) => typeof value === "string" && value)) {
      throw new TypeError("vault_recovery_configuration_invalid");
    }
    if (typeof sidecarOffline !== "function") throw new TypeError("vault_recovery_offline_probe_required");
    this.pythonPath = pythonPath;
    this.moduleRoot = moduleRoot;
    this.workingRoot = workingRoot;
    this.sidecarOffline = sidecarOffline;
    this.spawnChild = spawnChild;
  }

  recoverPending() {
    return this.#run(["--recover-pending"]);
  }

  adopt(operationId) {
    if (!OPERATION_ID.test(operationId)) return Promise.reject(new Error("vault_recovery_operation_invalid"));
    return this.#run(["--operation-id", operationId]);
  }

  #run(argumentsList) {
    if (!this.sidecarOffline()) return Promise.reject(new Error("vault_recovery_sidecar_must_be_offline"));
    return new Promise((resolve, reject) => {
      const child = this.spawnChild(
        this.pythonPath,
        ["-m", "backend.vault_recovery_cli", "--working-root", this.workingRoot, ...argumentsList],
        {
          cwd: path.dirname(this.workingRoot),
          env: { ...process.env, PYTHONPATH: this.moduleRoot, PYTHONDONTWRITEBYTECODE: "1" },
          windowsHide: true,
          stdio: ["ignore", "pipe", "pipe"],
        },
      );
      let stdout = "";
      let stderr = "";
      child.stdout?.setEncoding("utf8");
      child.stderr?.setEncoding("utf8");
      child.stdout?.on("data", (chunk) => { stdout += chunk; });
      child.stderr?.on("data", (chunk) => { stderr += chunk; });
      child.once("error", reject);
      child.once("exit", (code) => {
        let payload = null;
        try { payload = JSON.parse((code === 0 ? stdout : stderr).trim()); } catch {}
        if (code !== 0 || !payload || payload.status === "failed") {
          reject(new Error(payload?.error || `vault_recovery_exit_${code}`));
          return;
        }
        resolve(Object.freeze(payload));
      });
    });
  }
}

module.exports = { VaultRecoveryController };
