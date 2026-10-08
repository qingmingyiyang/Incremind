const OPEN_THRESHOLD = 0.055;
const CLOSE_THRESHOLD = 0.032;

export async function startPetVoicePlayback(bytes, {
  onMouth = () => {},
  onEnded = () => {},
  minimumHoldMs = 70,
  audioContextFactory = () => new globalThis.AudioContext(),
  requestFrame = (callback) => globalThis.requestAnimationFrame(callback),
  cancelFrame = (id) => globalThis.cancelAnimationFrame(id),
  now = () => globalThis.performance.now(),
} = {}) {
  if (!(bytes instanceof Uint8Array) || bytes.byteLength < 12 || bytes.byteLength > 8 * 1024 * 1024 || ascii(bytes, 0, 4) !== "RIFF" || ascii(bytes, 8, 12) !== "WAVE") throw new TypeError("pet voice audio is invalid");
  const context = audioContextFactory();
  const source = context.createBufferSource();
  const analyser = context.createAnalyser();
  analyser.fftSize = 256;
  analyser.smoothingTimeConstant = 0.35;
  const data = new Uint8Array(analyser.fftSize);
  const audioBuffer = await context.decodeAudioData(bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength));
  source.buffer = audioBuffer;
  source.connect(analyser);
  analyser.connect(context.destination);
  await context.resume?.();
  let frameId = null;
  let stopped = false;
  let open = false;
  let changedAt = -Infinity;

  const monitor = () => {
    if (stopped) return;
    analyser.getByteTimeDomainData(data);
    const rms = calculateRms(data);
    const next = open ? rms >= CLOSE_THRESHOLD : rms >= OPEN_THRESHOLD;
    const time = now();
    if (next !== open && time - changedAt >= minimumHoldMs) {
      open = next;
      changedAt = time;
      onMouth(open, rms);
    }
    frameId = requestFrame(monitor);
  };
  const stop = ({ ended = false } = {}) => {
    if (stopped) return false;
    stopped = true;
    if (frameId !== null) cancelFrame(frameId);
    try { if (!ended) source.stop(); } catch {}
    source.disconnect?.(); analyser.disconnect?.();
    void context.close?.();
    if (open) onMouth(false, 0);
    if (ended) onEnded();
    return true;
  };
  source.onended = () => stop({ ended: true });
  source.start();
  frameId = requestFrame(monitor);
  return Object.freeze({ stop });
}

export function calculateRms(samples) {
  if (!(samples instanceof Uint8Array) || samples.length === 0) return 0;
  let sum = 0;
  for (const sample of samples) { const normalized = (sample - 128) / 128; sum += normalized * normalized; }
  return Math.sqrt(sum / samples.length);
}

function ascii(bytes, start, end) {
  return String.fromCharCode(...bytes.subarray(start, end));
}

export { CLOSE_THRESHOLD, OPEN_THRESHOLD };
