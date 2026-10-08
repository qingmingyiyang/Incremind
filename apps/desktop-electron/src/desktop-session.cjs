function currentSessionForRequest(sessionProvider, startingSession, unavailable = "file_grant_sidecar_unavailable") {
  const current = sessionProvider();
  if (!current?.secret || !current?.instance_id || !current?.origin) throw new Error(unavailable);
  if (current.instance_id !== startingSession?.instance_id || current.origin !== startingSession?.origin) {
    throw new Error("desktop_session_instance_changed");
  }
  return current;
}

module.exports = { currentSessionForRequest };
