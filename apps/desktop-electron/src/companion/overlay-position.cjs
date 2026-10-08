const DEFAULT_OVERLAY_SIZE = Object.freeze({ width: 360, height: 180 });
const MAX_OVERLAY_SIZE = Object.freeze({ width: 440, height: 320 });
const OVERLAY_GAP = 12;

function resolveOverlayBounds({ petBounds, workArea, size = DEFAULT_OVERLAY_SIZE }) {
  for (const value of [petBounds, workArea, size]) {
    if (!value || ![value.x ?? 0, value.y ?? 0, value.width, value.height].every(Number.isFinite)) {
      throw new TypeError("overlay bounds input is invalid");
    }
  }
  const width = Math.min(Math.max(Math.round(size.width), 240), MAX_OVERLAY_SIZE.width, workArea.width);
  const height = Math.min(Math.max(Math.round(size.height), 120), MAX_OVERLAY_SIZE.height, workArea.height);
  const rightX = petBounds.x + petBounds.width + OVERLAY_GAP;
  const leftX = petBounds.x - width - OVERLAY_GAP;
  const fitsRight = rightX + width <= workArea.x + workArea.width;
  const fitsLeft = leftX >= workArea.x;
  const preferredX = fitsRight || !fitsLeft ? rightX : leftX;
  return Object.freeze({
    x: clamp(Math.round(preferredX), workArea.x, workArea.x + workArea.width - width),
    y: clamp(Math.round(petBounds.y + (petBounds.height - height) / 2), workArea.y, workArea.y + workArea.height - height),
    width,
    height,
    side: fitsRight || !fitsLeft ? "right" : "left",
  });
}

function clamp(value, minimum, maximum) {
  return Math.min(Math.max(value, minimum), maximum);
}

module.exports = { DEFAULT_OVERLAY_SIZE, MAX_OVERLAY_SIZE, OVERLAY_GAP, resolveOverlayBounds };
