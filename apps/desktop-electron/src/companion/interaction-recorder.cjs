const INTERACTION_EVENT_ID = /^gesture:petting:[a-z0-9]{1,32}$/;

async function recordCompanionInteraction({
  origin,
  secret,
  sessionHeader,
  eventId,
  fetchFn = fetch,
  timeoutSignal = () => AbortSignal.timeout(3000),
  wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds)),
}) {
  if (typeof origin !== "string" || !origin.startsWith("http://127.0.0.1:")) throw new Error("companion_origin_invalid");
  if (typeof secret !== "string" || !secret) throw new Error("companion_session_invalid");
  if (typeof sessionHeader !== "string" || !sessionHeader) throw new Error("companion_session_header_invalid");
  if (typeof eventId !== "string" || !INTERACTION_EVENT_ID.test(eventId)) throw new Error("companion_event_id_invalid");
  const body = JSON.stringify({ event_id: eventId, kind: "petting" });
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const response = await fetchFn(`${origin}/api/rebuild/companion/interactions`, {
        method: "POST",
        headers: { "Content-Type": "application/json", [sessionHeader]: secret },
        body,
        signal: timeoutSignal(),
      });
      if (response.ok) return Object.freeze({ status: "recorded", replayed: response.status === 200 });
      if (response.status < 500) return Object.freeze({ status: "rejected" });
    } catch {}
    if (attempt === 0) await wait(150);
  }
  return Object.freeze({ status: "unavailable" });
}

module.exports = { INTERACTION_EVENT_ID, recordCompanionInteraction };
