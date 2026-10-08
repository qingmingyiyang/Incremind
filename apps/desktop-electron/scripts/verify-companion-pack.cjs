const fs = require("node:fs");
const path = require("node:path");

const repositoryRoot = path.resolve(__dirname, "..", "..", "..");
const defaultManifest = path.join(repositoryRoot, "src", "frontend", "public", "mascots", "companion-pack.json");

function verifyCompanionPack(manifestPath = defaultManifest) {
  const absolute = path.resolve(manifestPath);
  const bytes = fs.readFileSync(absolute);
  if (bytes.length < 2 || bytes.length > 64 * 1024) throw new Error("companion pack manifest size is invalid");
  const pack = JSON.parse(bytes.toString("utf8"));
  if (![2, 3].includes(pack?.version) || typeof pack.pack_id !== "string" || !pack.pack_id) throw new Error("companion pack version or id is invalid");
  const sprite = pack.sprite;
  if (!sprite || typeof sprite.src !== "string" || !/^\.\/mascots\/[a-z0-9][a-z0-9_-]*\.png$/.test(sprite.src)) throw new Error("companion pack sprite path is invalid");
  const spritePath = path.resolve(path.dirname(absolute), path.basename(sprite.src));
  if (path.dirname(spritePath) !== path.dirname(absolute) || !fs.statSync(spritePath).isFile()) throw new Error("companion pack sprite is missing");
  const dimensions = pngDimensions(fs.readFileSync(spritePath));
  if (dimensions.width !== sprite.width || dimensions.height !== sprite.height || sprite.width !== sprite.frame_width * sprite.columns || sprite.height !== sprite.frame_height * sprite.rows) throw new Error("companion pack sprite dimensions do not match manifest");
  if (!pack.states || !pack.states.ready) throw new Error("companion pack ready state is missing");
  const overlays = pack.version === 3 ? verifyOverlays(pack.overlays, sprite, absolute) : [];
  for (const [stateId, state] of Object.entries(pack.states)) {
    if (!/^[a-z][a-z0-9_]{0,31}$/.test(stateId) || !state || typeof state !== "object" || Array.isArray(state)) throw new Error("companion pack state is invalid");
    if (Array.isArray(state.frames)) {
      if (!Number.isInteger(state.row) || state.row < 0 || state.row >= sprite.rows || state.frames.some((frame) => !Number.isInteger(frame) || frame < 0 || frame >= sprite.columns)) throw new Error("companion pack frame is invalid");
    } else if (typeof state.fallback !== "string" || !pack.states[state.fallback]) throw new Error("companion pack fallback is invalid");
  }
  for (const stateId of Object.keys(pack.states)) resolveState(pack.states, stateId);
  return Object.freeze({ pack_id: pack.pack_id, sprite: path.basename(spritePath), ...dimensions, states: Object.keys(pack.states).length, overlays });
}

function verifyOverlays(overlays, sprite, manifestPath) {
  if (!overlays || typeof overlays !== "object" || Array.isArray(overlays) || Object.keys(overlays).length > 8) throw new Error("companion pack overlays are invalid");
  return Object.entries(overlays).map(([overlayId, overlay]) => {
    if (!/^[a-z][a-z0-9_]{0,31}$/.test(overlayId) || !overlay || typeof overlay !== "object" || Array.isArray(overlay)) throw new Error("companion pack overlay is invalid");
    if (typeof overlay.src !== "string" || !/^\.\/mascots\/(?:[a-z0-9][a-z0-9_-]*\.png|items\/[a-z0-9][a-z0-9-]*\.svg)$/.test(overlay.src)) throw new Error("companion pack overlay path is invalid");
    const overlayKeys = Object.keys(overlay).sort().join();
    if (overlayKeys !== "height,natural_height,natural_width,src,width,x,y") throw new Error("companion pack overlay schema is invalid");
    for (const field of ["x", "y", "width", "height", "natural_width", "natural_height"]) if (!Number.isInteger(overlay[field])) throw new Error("companion pack overlay bounds are invalid");
    if (overlay.x < 0 || overlay.y < 0 || overlay.width < 1 || overlay.height < 1 || overlay.x + overlay.width > sprite.frame_width || overlay.y + overlay.height > sprite.frame_height) throw new Error("companion pack overlay bounds are invalid");
    const relative = overlay.src.slice("./mascots/".length);
    const overlayRoot = path.dirname(manifestPath);
    const overlayPath = path.resolve(overlayRoot, relative);
    if (!overlayPath.startsWith(`${overlayRoot}${path.sep}`) || !fs.lstatSync(overlayPath).isFile() || fs.lstatSync(overlayPath).isSymbolicLink()) throw new Error("companion pack overlay is missing");
    const overlayBytes = fs.readFileSync(overlayPath);
    const isSvg = overlayPath.endsWith(".svg");
    const dimensions = isSvg ? svgDimensions(overlayBytes) : pngDimensions(overlayBytes);
    if (!isSvg && overlayBytes[25] !== 6) throw new Error("companion pack overlay must be RGBA PNG");
    if (dimensions.width !== overlay.natural_width || dimensions.height !== overlay.natural_height || dimensions.width > 4096 || dimensions.height > 4096) throw new Error("companion pack overlay dimensions are invalid");
    return Object.freeze({ id: overlayId, file: path.basename(overlayPath), ...dimensions });
  });
}

function resolveState(states, start) {
  const visited = new Set();
  let current = start;
  while (!Array.isArray(states[current]?.frames)) {
    if (visited.has(current)) throw new Error("companion pack fallback cycle");
    visited.add(current);
    current = states[current]?.fallback;
    if (!states[current]) throw new Error("companion pack fallback target is missing");
  }
}

function pngDimensions(buffer) {
  const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  if (buffer.length < 24 || !buffer.subarray(0, 8).equals(signature) || buffer.toString("ascii", 12, 16) !== "IHDR") throw new Error("companion sprite is not a PNG");
  return { width: buffer.readUInt32BE(16), height: buffer.readUInt32BE(20) };
}

function svgDimensions(buffer) {
  if (buffer.length < 32 || buffer.length > 64 * 1024) throw new Error("companion pack SVG overlay is invalid");
  const source = buffer.toString("utf8");
  if (/<(?:script|image|foreignObject)\b|(?:href|src)\s*=|file:/i.test(source)) throw new Error("companion pack SVG overlay is unsafe");
  const match = source.match(/^<svg\s[^>]*\bwidth="(\d+)"[^>]*\bheight="(\d+)"[^>]*>/);
  if (!match) throw new Error("companion pack SVG overlay dimensions are invalid");
  return { width: Number(match[1]), height: Number(match[2]) };
}

if (require.main === module) process.stdout.write(`${JSON.stringify({ status: "passed", ...verifyCompanionPack() })}\n`);

module.exports = { defaultManifest, pngDimensions, verifyCompanionPack };
