const crypto = require("node:crypto");
const fs = require("node:fs");
const http = require("node:http");
const os = require("node:os");
const path = require("node:path");
const { spawn, spawnSync } = require("node:child_process");
const { Transform } = require("node:stream");

const ELECTRON_ROOT = path.resolve(__dirname, "..");
const RESOURCES_ROOT = path.join(ELECTRON_ROOT, "release", "win-unpacked", "resources");
const LONG_MEDIA_SECONDS = 2 * 60 * 60;

function packagedLayout(resourcesRoot = RESOURCES_ROOT) {
  const sidecarRoot = path.join(resourcesRoot, "sidecar");
  const appRoot = path.join(resourcesRoot, "app");
  const pythonPath = path.join(sidecarRoot, "runtime", process.platform === "win32" ? "python.exe" : "python");
  const binRoot = path.join(sidecarRoot, "runtime", "Library", "bin");
  const layout = {
    appRoot,
    sidecarRoot,
    pythonPath,
    ffmpegPath: path.join(binRoot, process.platform === "win32" ? "ffmpeg.exe" : "ffmpeg"),
    ffprobePath: path.join(binRoot, process.platform === "win32" ? "ffprobe.exe" : "ffprobe"),
  };
  for (const [name, value] of Object.entries(layout)) {
    if (!fs.existsSync(value)) throw new Error(`long_media_packaged_${name}_missing`);
  }
  return layout;
}

function runChecked(executable, args, options = {}) {
  const result = spawnSync(executable, args, {
    encoding: "utf8",
    windowsHide: true,
    timeout: options.timeout || 15 * 60 * 1000,
    env: options.env || process.env,
    cwd: options.cwd,
  });
  if (result.status !== 0) {
    throw new Error(`${path.basename(executable)} failed: ${String(result.stderr || result.error || "unknown error").slice(-4000)}`);
  }
  return result.stdout;
}

function generateLongMedia(ffmpegPath, outputPath, durationSeconds = LONG_MEDIA_SECONDS) {
  fs.mkdirSync(path.dirname(outputPath), { recursive: true });
  const started = Date.now();
  runChecked(ffmpegPath, [
    "-hide_banner", "-loglevel", "error", "-y",
    "-f", "lavfi", "-i", `color=c=0x1f6feb:s=320x180:r=1:d=${durationSeconds}`,
    "-f", "lavfi", "-i", `sine=frequency=440:sample_rate=16000:duration=${durationSeconds}`,
    "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "40", "-g", "60",
    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "24k", "-movflags", "+faststart", outputPath,
  ]);
  const stat = fs.statSync(outputPath);
  if (!stat.isFile() || stat.size <= 0) throw new Error("long_media_generation_empty");
  return { elapsed_ms: Date.now() - started, size_bytes: stat.size, sha256: hashFileSync(outputPath) };
}

function inspectLongMedia(ffprobePath, filePath, expectedSeconds = LONG_MEDIA_SECONDS) {
  const raw = runChecked(ffprobePath, [
    "-v", "error", "-show_entries", "format=duration:stream=codec_type,codec_name,sample_rate,channels",
    "-of", "json", filePath,
  ]);
  const probe = JSON.parse(raw);
  const duration = Number(probe?.format?.duration);
  if (!Number.isFinite(duration) || Math.abs(duration - expectedSeconds) > 1) {
    throw new Error(`long_media_duration_mismatch:${duration}`);
  }
  const types = new Set((probe.streams || []).map((stream) => stream.codec_type));
  if (!types.has("video") || !types.has("audio")) throw new Error("long_media_streams_missing");
  return { duration_seconds: duration, streams: probe.streams };
}

function hashFileSync(filePath) {
  const hash = crypto.createHash("sha256");
  const descriptor = fs.openSync(filePath, "r");
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  try {
    for (;;) {
      const read = fs.readSync(descriptor, buffer, 0, buffer.length, null);
      if (!read) break;
      hash.update(buffer.subarray(0, read));
    }
  } finally {
    fs.closeSync(descriptor);
  }
  return hash.digest("hex");
}

async function waitFor(check, label, timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try { return await check(); } catch (error) { lastError = error; }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`${label}: ${lastError?.message || "timeout"}`);
}

async function postJson(session, pathname, body) {
  const response = await fetch(`${session.origin}${pathname}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Chriptmas-Desktop-Session": session.secret },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(`${pathname} failed (${response.status}): ${JSON.stringify(payload)}`);
  return payload;
}

function createSlowReadStream(filePath, options) {
  const delay = new Transform({
    transform(chunk, _encoding, callback) { setTimeout(() => callback(null, chunk), 15); },
  });
  return fs.createReadStream(filePath, { ...options, highWaterMark: 64 * 1024 }).pipe(delay);
}

function listPartials(workRoot) {
  const incoming = path.join(workRoot, "library", "assets", "originals", ".incoming");
  if (!fs.existsSync(incoming)) return [];
  return fs.readdirSync(incoming).filter((name) => name.endsWith(".part")).map((name) => path.join(incoming, name));
}

function startProcessSampler(pythonPath, pid, root) {
  const stopPath = path.join(root, "sampler.stop");
  const outputPath = path.join(root, "sampler.json");
  const child = spawn(pythonPath, [path.join(__dirname, "process-tree-sampler.py"), String(pid), stopPath, outputPath], {
    stdio: ["ignore", "ignore", "pipe"], windowsHide: true,
  });
  return {
    async stop() {
      fs.writeFileSync(stopPath, "stop\n");
      await waitFor(() => child.exitCode !== null ? true : Promise.reject(new Error("sampler active")), "process sampler stop", 10000);
      return JSON.parse(fs.readFileSync(outputPath, "utf8"));
    },
  };
}

function enableLocalExtractor(layout, workRoot, outputRoot) {
  const script = [
    "import sys",
    "from pathlib import Path",
    "from rebuild.storage_provider import JsonObjectStore",
    "from rebuild.product_core import SaveVideoAudioExtractorSettings",
    "root=Path(sys.argv[1])",
    "store=JsonObjectStore(root/'.rebuild-data', legacy_root=root/'library')",
    "SaveVideoAudioExtractorSettings(store).execute(enabled=True,ffmpeg_path=sys.argv[2],ffprobe_path=sys.argv[3],output_root=sys.argv[4],confirm_enable=True)",
  ].join(";");
  runChecked(layout.pythonPath, ["-c", script, workRoot, layout.ffmpegPath, layout.ffprobePath, outputRoot], {
    cwd: workRoot,
    env: { ...process.env, PYTHONPATH: layout.sidecarRoot, PYTHONDONTWRITEBYTECODE: "1" },
  });
}

async function forceKill(child) {
  if (!child || child.exitCode !== null) return;
  if (process.platform === "win32") {
    spawnSync("taskkill.exe", ["/pid", String(child.pid), "/t", "/f"], { stdio: "ignore", windowsHide: true });
  } else child.kill("SIGKILL");
  await waitFor(() => child.exitCode !== null ? true : Promise.reject(new Error("sidecar alive")), "forced sidecar exit", 10000);
}

function directoryBytes(root) {
  if (!fs.existsSync(root)) return 0;
  let total = 0;
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const item = path.join(root, entry.name);
    if (entry.isDirectory()) total += directoryBytes(item);
    else if (entry.isFile()) total += fs.statSync(item).size;
  }
  return total;
}

function readAuthorityRecord(workRoot, collection, recordId) {
  const recordPath = path.join(workRoot, ".rebuild-data", "objects", "default", collection, `${recordId}.json`);
  return { recordPath, bytes: fs.readFileSync(recordPath), payload: JSON.parse(fs.readFileSync(recordPath, "utf8")) };
}

async function runLongMediaProfile({ resourcesRoot = RESOURCES_ROOT, keepArtifacts = false } = {}) {
  const layout = packagedLayout(resourcesRoot);
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-long-media-"));
  const workRoot = path.join(root, "appData");
  const mediaPath = path.join(root, "generated-two-hour.mp4");
  const outputRoot = path.join(workRoot, "generated-audio");
  fs.mkdirSync(path.join(workRoot, "config"), { recursive: true });
  fs.copyFileSync(path.join(layout.sidecarRoot, "config", "settings.toml"), path.join(workRoot, "config", "settings.toml"));
  const generation = generateLongMedia(layout.ffmpegPath, mediaPath);
  const probe = inspectLongMedia(layout.ffprobePath, mediaPath);
  enableLocalExtractor(layout, workRoot, outputRoot);
  const { SidecarSupervisor } = require(path.join(layout.appRoot, "src", "sidecar-supervisor.cjs"));
  const { createFileGrant, uploadFileGrant } = require(path.join(layout.appRoot, "src", "file-grant.cjs"));
  let supervisor;
  let sampler;
  const nodePeak = { rss: process.memoryUsage().rss };
  const nodeTimer = setInterval(() => { nodePeak.rss = Math.max(nodePeak.rss, process.memoryUsage().rss); }, 50);
  const started = Date.now();
  try {
    supervisor = new SidecarSupervisor({ rootDir: workRoot, moduleRoot: layout.sidecarRoot, workingDir: workRoot, pythonPath: layout.pythonPath });
    let session = await supervisor.start();
    sampler = startProcessSampler(layout.pythonPath, session.child_pid, root);
    const grant = await createFileGrant({ filePath: mediaPath, session, mediaType: "video/mp4", sourceKind: "video" });
    const upload = await uploadFileGrant(grant, session);
    if (upload.sha256 !== generation.sha256 || upload.byte_count !== generation.size_bytes) throw new Error("long_media_upload_identity_mismatch");
    const intakeBody = {
      content: "", media_type: "video/mp4", file_name: path.basename(mediaPath), urls: [],
      add_to_knowledge_base: true, title: "Generated two-hour media", original_asset_ref: upload.asset_ref,
    };
    const intake = await postJson(session, "/api/rebuild/workbench/auto-intake", intakeBody);
    const item = intake.items?.[0];
    const workflow = item?.auto_organization?.media_auto_workflow;
    if (!item?.source_id || workflow?.steps?.[0]?.name !== "extract_audio" || workflow.steps[0].status !== "completed") {
      throw new Error(`long_media_audio_extraction_not_completed:${JSON.stringify(workflow)}`);
    }
    if (workflow?.steps?.[1]?.name !== "transcribe_audio" || workflow.steps[1].status === "completed") {
      throw new Error("long_media_asr_boundary_not_honest");
    }
    const assetAuthorityBeforeReplay = readAuthorityRecord(workRoot, "workbench_original_assets", upload.asset_id);
    const sourceAuthorityBeforeReplay = readAuthorityRecord(workRoot, "sources", item.source_id);
    const jobAuthorityBeforeReplay = readAuthorityRecord(workRoot, "jobs", intake.job_id);

    const replayGrant = await createFileGrant({ filePath: mediaPath, session, mediaType: "video/mp4", sourceKind: "video" });
    const replay = await uploadFileGrant(replayGrant, session);
    if (replay.asset_id !== upload.asset_id) throw new Error("long_media_replay_created_duplicate_asset");
    const replayIntake = await postJson(session, "/api/rebuild/workbench/auto-intake", intakeBody);
    const replayItem = replayIntake.items?.[0];
    if (replayItem?.source_id !== item.source_id || replayIntake.job_id !== intake.job_id) {
      throw new Error("long_media_replay_created_duplicate_source_or_job");
    }
    const assetAuthorityAfterReplay = readAuthorityRecord(workRoot, "workbench_original_assets", upload.asset_id);
    const sourceAuthorityAfterReplay = readAuthorityRecord(workRoot, "sources", item.source_id);
    const jobAuthorityAfterReplay = readAuthorityRecord(workRoot, "jobs", intake.job_id);
    if (!assetAuthorityBeforeReplay.bytes.equals(assetAuthorityAfterReplay.bytes)) {
      throw new Error("long_media_replay_changed_asset_revision");
    }
    if (!sourceAuthorityBeforeReplay.bytes.equals(sourceAuthorityAfterReplay.bytes)) {
      throw new Error("long_media_replay_changed_source_revision");
    }
    if (!jobAuthorityBeforeReplay.bytes.equals(jobAuthorityAfterReplay.bytes)) {
      throw new Error("long_media_replay_changed_job_revision");
    }

    const cancelGrant = await createFileGrant({ filePath: mediaPath, session, mediaType: "video/mp4", sourceKind: "video" });
    const controller = new AbortController();
    const cancelStarted = Date.now();
    const cancelling = uploadFileGrant(cancelGrant, session, {
      signal: controller.signal,
      createReadStream: createSlowReadStream,
    }).then(() => null, (error) => error);
    await waitFor(() => listPartials(workRoot).length ? true : Promise.reject(new Error("partial absent")), "cancel partial creation");
    controller.abort();
    const cancelError = await cancelling;
    if (!(cancelError instanceof Error)) throw new Error("cancelled upload completed");
    await waitFor(() => listPartials(workRoot).length === 0 ? true : Promise.reject(new Error("partial remains")), "cancel partial cleanup");
    const cancelMs = Date.now() - cancelStarted;

    const killGrant = await createFileGrant({ filePath: mediaPath, session, mediaType: "video/mp4", sourceKind: "video" });
    const interrupted = uploadFileGrant(killGrant, session, { createReadStream: createSlowReadStream })
      .then(() => null, (error) => error);
    await waitFor(() => listPartials(workRoot).length ? true : Promise.reject(new Error("partial absent")), "kill partial creation");
    const killedPid = supervisor.child.pid;
    await forceKill(supervisor.child);
    const interruptedError = await interrupted;
    if (!(interruptedError instanceof Error)) throw new Error("killed upload completed");
    const killedPartials = listPartials(workRoot);
    for (const part of killedPartials) fs.utimesSync(part, new Date(0), new Date(0));
    supervisor = new SidecarSupervisor({ rootDir: workRoot, moduleRoot: layout.sidecarRoot, workingDir: workRoot, pythonPath: layout.pythonPath });
    session = await supervisor.start();
    await waitFor(() => listPartials(workRoot).length === 0 ? true : Promise.reject(new Error("stale partial remains")), "startup partial recovery");
    const restartGrant = await createFileGrant({ filePath: mediaPath, session, mediaType: "video/mp4", sourceKind: "video" });
    const restartReplay = await uploadFileGrant(restartGrant, session);
    if (restartReplay.asset_id !== upload.asset_id) throw new Error("long_media_restart_created_duplicate_asset");

    const processProfile = await sampler.stop();
    sampler = null;
    const result = {
      status: "passed",
      generated_non_private_fixture: true,
      fixture: { ...generation, ...probe },
      upload: { asset_id: upload.asset_id, asset_ref: upload.asset_ref, source_id: item.source_id, job_id: intake.job_id },
      workflow: { status: workflow.status, steps: workflow.steps, asr_blocked: true },
      replay: { same_asset: true, same_source: true, same_job: true, revisions_unchanged: true, after_restart_same_asset: true },
      cancellation: { elapsed_ms: cancelMs, partial_cleaned: true },
      process_kill: { killed_pid: killedPid, partial_observed: killedPartials.length > 0, startup_recovered: true },
      resources: {
        node_peak_rss_bytes: nodePeak.rss,
        sidecar_tree: processProfile,
        vault_disk_bytes: directoryBytes(workRoot),
        elapsed_ms: Date.now() - started,
      },
      temporary_root: keepArtifacts ? root : "[removed]",
    };
    return result;
  } finally {
    clearInterval(nodeTimer);
    if (sampler) await sampler.stop().catch(() => {});
    if (supervisor) await supervisor.stop().catch(() => {});
    if (!keepArtifacts) fs.rmSync(root, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
}

if (require.main === module) {
  runLongMediaProfile({ keepArtifacts: process.argv.includes("--keep-artifacts") })
    .then((result) => console.log(JSON.stringify(result, null, 2)))
    .catch((error) => { console.error(`[profile:long-media] ${error.stack || error.message}`); process.exitCode = 1; });
}

module.exports = { LONG_MEDIA_SECONDS, generateLongMedia, inspectLongMedia, packagedLayout, runLongMediaProfile };
