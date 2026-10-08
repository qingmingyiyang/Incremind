const DEFAULT_TIMEOUT_MS = 15000;

function requireSession(session) {
  if (!session?.child || typeof session.child.once !== "function") {
    throw new TypeError("shutdown_handshake_child_invalid");
  }
  if (!Number.isInteger(session.sidecarPid) || session.sidecarPid <= 0) {
    throw new TypeError("shutdown_handshake_sidecar_pid_invalid");
  }
}

async function runPackagedShutdownHandshake({
  session,
  waitFor,
  processIsAlive,
  triggerShutdown,
  now = Date.now,
  timeoutMs = DEFAULT_TIMEOUT_MS,
}) {
  requireSession(session);
  if (typeof waitFor !== "function" || typeof processIsAlive !== "function" || typeof triggerShutdown !== "function" || typeof now !== "function") {
    throw new TypeError("shutdown_handshake_dependency_invalid");
  }
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 30000) {
    throw new TypeError("shutdown_handshake_timeout_invalid");
  }

  const startedAt = now();
  let observationSequence = 0;
  const observe = (details) => ({ at: now(), order: ++observationSequence, ...details });
  const observations = {
    electron_exit: null,
    sidecar_exit: null,
  };
  session.child.once("exit", (code, signal) => {
    observations.electron_exit ||= observe({
      exit_code: code,
      signal: signal || null,
    });
  });

  const shutdownTriggeredAt = now();
  const trigger = await triggerShutdown();
  if (!trigger || trigger.action_id !== "app.quit" || trigger.status !== "opened") {
    throw new Error("native menu shutdown action was not accepted");
  }
  await waitFor(() => {
    if (!observations.electron_exit && session.child.exitCode !== null) {
      observations.electron_exit = observe({
        exit_code: session.child.exitCode,
        signal: session.child.signalCode || null,
      });
    }
    if (!observations.sidecar_exit && !processIsAlive(session.sidecarPid)) {
      observations.sidecar_exit = observe({});
    }
    if (!observations.electron_exit || !observations.sidecar_exit) {
      throw new Error("normal shutdown is still pending");
    }
    return true;
  }, "packaged normal shutdown", timeoutMs);

  if (observations.electron_exit.exit_code !== 0 || observations.electron_exit.signal !== null) {
    throw new Error(`Electron normal shutdown exited unexpectedly: code=${observations.electron_exit.exit_code} signal=${observations.electron_exit.signal || "none"}`);
  }
  const completedAt = now();
  return Object.freeze({
    trigger_route: "pet_native_menu_action_registry_app_quit",
    child_exit_code: observations.electron_exit.exit_code,
    child_exit_signal: observations.electron_exit.signal,
    observation_order: observations.sidecar_exit.order < observations.electron_exit.order
      ? ["sidecar_absent", "electron_exit"]
      : ["electron_exit", "sidecar_absent"],
    launch_to_shutdown_trigger_ms: shutdownTriggeredAt - startedAt,
    shutdown_trigger_to_sidecar_absent_ms: observations.sidecar_exit.at - shutdownTriggeredAt,
    shutdown_trigger_to_electron_exit_ms: observations.electron_exit.at - shutdownTriggeredAt,
    total_shutdown_ms: completedAt - shutdownTriggeredAt,
  });
}

module.exports = { runPackagedShutdownHandshake };
