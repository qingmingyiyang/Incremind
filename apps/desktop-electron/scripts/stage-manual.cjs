const fs = require("node:fs");
const path = require("node:path");

const MAX_MANUAL_BYTES = 2 * 1024 * 1024;

function stageManual({ repositoryRoot, outputRoot } = {}) {
  const root = path.resolve(repositoryRoot || path.join(__dirname, "..", "..", ".."));
  const stage = path.resolve(outputRoot || path.join(__dirname, "..", ".manual-stage"));
  const source = path.join(root, "readme.md");
  const target = path.join(stage, "readme.md");
  const sourceStat = safeFileStat(source, "manual source");
  if (sourceStat.size > MAX_MANUAL_BYTES) throw new Error("manual source exceeds the 2 MiB limit");
  const content = fs.readFileSync(source);
  assertUtf8(content);
  fs.mkdirSync(stage, { recursive: true });
  const unexpected = fs.readdirSync(stage).filter((name) => name !== "readme.md" && !name.startsWith(".readme.md."));
  if (unexpected.length) throw new Error("manual stage contains unexpected files");
  const temporary = path.join(stage, `.readme.md.${process.pid}.${Date.now()}.tmp`);
  try {
    fs.writeFileSync(temporary, content, { flag: "wx" });
    fs.renameSync(temporary, target);
  } finally {
    try { fs.unlinkSync(temporary); } catch (error) { if (error.code !== "ENOENT") throw error; }
  }
  const targetStat = safeFileStat(target, "staged manual");
  if (targetStat.size !== sourceStat.size) throw new Error("staged manual size does not match source");
  return Object.freeze({ source, target, size: targetStat.size });
}

function safeFileStat(filePath, label) {
  let stat;
  try { stat = fs.lstatSync(filePath); } catch (error) { throw new Error(`${label} is missing`, { cause: error }); }
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error(`${label} must be a regular non-symlink file`);
  return stat;
}

function assertUtf8(buffer) {
  const decoder = new TextDecoder("utf-8", { fatal: true });
  try { decoder.decode(buffer); } catch (error) { throw new Error("manual source must be valid UTF-8", { cause: error }); }
}

if (require.main === module) {
  const result = stageManual();
  process.stdout.write(`Staged manual readme.md (${result.size} bytes)\n`);
}

module.exports = { MAX_MANUAL_BYTES, stageManual };
