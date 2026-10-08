const { spawnSync } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const path = require("node:path");

const ELECTRON_ROOT = path.resolve(__dirname, "..");
const REPO_ROOT = path.resolve(ELECTRON_ROOT, "..", "..");
const FRONTEND_ROOT = path.join(REPO_ROOT, "src", "frontend");
const SOURCE_FRONTEND = path.join(FRONTEND_ROOT, "dist");
const STAGED_FRONTEND = path.join(ELECTRON_ROOT, "frontend-dist");
const PACKAGED_FRONTEND = path.join(ELECTRON_ROOT, "release", "win-unpacked", "resources", "app", "frontend-dist");
const EXE = path.join(ELECTRON_ROOT, "release", "win-unpacked", "Chriptmas OS.exe");
const CANDIDATE_STAGE_ROOT = path.join(ELECTRON_ROOT, ".candidate-identity-stage");
const STAGED_SIDECAR_MANIFEST = path.join(ELECTRON_ROOT, ".sidecar-stage", "sidecar-manifest.json");
const SIDECAR_TRANSFER_NONCE_ENV = "CHRIPTMAS_SIDECAR_TRANSFER_NONCE";
const SIDECAR_MANIFEST_SHA_ENV = "CHRIPTMAS_SIDECAR_MANIFEST_SHA256";
const {
  inspectCandidateShellCache,
  inspectFrontendBuildCache,
  refreshCandidateShellCache,
  refreshFrontendBuildCache,
} = require("./candidate-build-cache.cjs");
const {
  beginSidecarReplacement,
  commitSidecarReplacement,
  proofName,
  rollbackSidecarReplacement,
} = require("./sidecar-transfer.cjs");
const {
  readSidecarVerificationProof,
  SIDECAR_VERIFY_CACHE_PATH,
  sidecarProofMatches,
} = require("./verify-build.cjs");
const { verifySidecarMetadata } = require("./verify-sidecar.cjs");
const { refreshRotatedStageProofs } = require("./stage-sidecar.cjs");
const {
  beginDirectoryReplacement,
  commitDirectoryReplacement,
  rollbackDirectoryReplacement,
} = require("./candidate-directory-replacement.cjs");
const {
  assertBuildId,
  createCandidateIdentity,
  writeCandidateIdentity,
} = require("../src/candidate-identity.cjs");

function fail(message) {
  throw new Error(message);
}

function sourceCommit({ spawn = spawnSync } = {}) {
  const result = spawn("git", ["-C", REPO_ROOT, "rev-parse", "HEAD"], { encoding: "utf8", windowsHide: true });
  const value = result.status === 0 ? String(result.stdout || "").trim() : "";
  if (!/^[a-f0-9]{40}$/.test(value)) fail("无法读取当前源码提交，拒绝生成候选身份。");
  return value;
}

function createBuildId(commit, now = new Date()) {
  if (!/^[a-f0-9]{40}$/.test(commit || "")) fail("候选构建缺少有效source_commit。");
  const stamp = now.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
  return `windows-${stamp}-${commit.slice(0, 12)}`;
}

function candidatePaths(buildId) {
  assertBuildId(buildId);
  const outputRoot = path.join(ELECTRON_ROOT, "release", "candidates", buildId);
  return Object.freeze({
    outputRoot,
    unpackedRoot: path.join(outputRoot, "win-unpacked"),
    packagedFrontend: path.join(outputRoot, "win-unpacked", "resources", "app", "frontend-dist"),
    executable: path.join(outputRoot, "win-unpacked", "Chriptmas OS.exe"),
  });
}

function prepareCandidateIdentity(buildId, { now = new Date() } = {}) {
  const packageJson = JSON.parse(fs.readFileSync(path.join(ELECTRON_ROOT, "package.json"), "utf8"));
  const identity = createCandidateIdentity({
    buildId,
    sourceCommit: sourceCommit(),
    packageVersion: packageJson.version,
    now,
  });
  fs.mkdirSync(CANDIDATE_STAGE_ROOT, { recursive: true });
  writeCandidateIdentity(CANDIDATE_STAGE_ROOT, identity);
  return identity;
}

function run(label, command, args, cwd, env = process.env) {
  console.log(`\n[windows-build] ${label}`);
  const startedAt = Date.now();
  const result = spawnSync(command, args, { cwd, env, stdio: "inherit", windowsHide: false });
  if (result.error) fail(`${label}无法启动: ${result.error.message}`);
  if (result.status !== 0) fail(`${label}失败，退出码 ${result.status ?? "unknown"}`);
  console.log(`[windows-build] ${label}完成: ${Date.now() - startedAt} ms`);
}

function candidateE2EEnvironment(candidate, baseEnv = process.env) {
  if (!candidate?.executable) fail("候选E2E缺少可执行文件路径。");
  return { ...baseEnv, CHRIPTMAS_E2E_EXE: candidate.executable };
}

function runNpm(label, args, cwd, env = process.env, {
  spawn = spawnSync,
  platform = process.platform,
  comSpec = process.env.ComSpec,
} = {}) {
  const npmCommand = platform === "win32" ? "npm.cmd" : "npm";
  console.log(`\n[windows-build] ${label}`);
  const startedAt = Date.now();
  const command = platform === "win32" ? (comSpec || "cmd.exe") : npmCommand;
  const commandArgs = platform === "win32"
    ? ["/d", "/s", "/c", [npmCommand, ...args].join(" ")]
    : args;
  const result = spawn(command, commandArgs, {
    cwd,
    env,
    stdio: "inherit",
    windowsHide: false,
  });
  if (result.error) fail(`${label}无法启动: ${result.error.message}`);
  if (result.status !== 0) fail(`${label}失败，退出码 ${result.status ?? "unknown"}`);
  console.log(`[windows-build] ${label}完成: ${Date.now() - startedAt} ms`);
}

function assertCandidateNotRunning() {
  if (process.platform !== "win32") fail("该入口仅支持Windows构建。");
  const result = spawnSync("tasklist.exe", ["/FI", "IMAGENAME eq Chriptmas OS.exe", "/FO", "CSV", "/NH"], { encoding: "utf8", windowsHide: true });
  if (result.status === 0 && result.stdout.includes('"Chriptmas OS.exe"')) {
    fail("Chriptmas OS仍在运行。请先关闭应用，再重新双击构建入口。");
  }
}

function walkFiles(root, base = root) {
  if (!fs.existsSync(root)) fail(`缺少目录: ${root}`);
  const files = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const absolute = path.join(root, entry.name);
    if (entry.isDirectory()) files.push(...walkFiles(absolute, base));
    else if (entry.isFile()) files.push(path.relative(base, absolute).replaceAll(path.sep, "/"));
  }
  return files.sort();
}

function hashFile(file) {
  return crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

function assertTreesEqual(expectedRoot, actualRoot, label) {
  const expected = walkFiles(expectedRoot);
  const actual = walkFiles(actualRoot);
  if (JSON.stringify(expected) !== JSON.stringify(actual)) fail(`${label}文件清单不一致。`);
  for (const relative of expected) {
    const expectedFile = path.join(expectedRoot, relative);
    const actualFile = path.join(actualRoot, relative);
    const expectedStat = fs.statSync(expectedFile);
    const actualStat = fs.statSync(actualFile);
    if (expectedStat.size !== actualStat.size || hashFile(expectedFile) !== hashFile(actualFile)) {
      fail(`${label}内容不一致: ${relative}`);
    }
  }
  console.log(`[windows-build] ${label}一致: ${expected.length}个文件`);
}

function requireSafeDirectory(directory, label, io = fs) {
  const stat = io.lstatSync(directory, { throwIfNoEntry: false });
  if (!stat?.isDirectory() || stat.isSymbolicLink()) fail(`${label}不是安全的普通目录: ${directory}`);
  return stat;
}

function inspectVerifiedStandbyCandidate(sidecarRoot, {
  proofPath = SIDECAR_VERIFY_CACHE_PATH,
  io = fs,
} = {}) {
  const rootStat = io.lstatSync(sidecarRoot, { throwIfNoEntry: false });
  if (!rootStat) return { hit: false, reason: "candidate_sidecar_missing" };
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) {
    throw new Error(`现有sidecar候选不安全: ${sidecarRoot}`);
  }
  let manifestSha256;
  let metadata;
  try {
    const manifestPath = path.join(sidecarRoot, "sidecar-manifest.json");
    manifestSha256 = hashFile(manifestPath);
    metadata = verifySidecarMetadata(sidecarRoot);
  } catch {
    // A previous candidate can predate a newly required base-pack file. It is
    // never trusted or retained as a verified standby; treating it as a cache
    // miss lets the atomic replacement install the current verified stage.
    return { hit: false, reason: "candidate_metadata_invalid" };
  }
  const loaded = readSidecarVerificationProof(proofPath, io);
  if (!loaded.hit) return { hit: false, reason: loaded.reason };
  if (!sidecarProofMatches(loaded.proof, manifestSha256, metadata)) {
    return { hit: false, reason: "candidate_proof_mismatch" };
  }
  return {
    hit: true,
    reason: "verified_candidate_proof",
    manifestSha256,
    verifiedSidecar: metadata,
  };
}

async function refreshCommittedStandbyProof(transaction) {
  if (!transaction?.standbyVerification?.hit) return null;
  return refreshRotatedStageProofs({
    stageRoot: transaction.stageRoot,
    verifiedSidecar: transaction.standbyVerification.verifiedSidecar,
    expectedManifestSha256: transaction.standbyVerification.manifestSha256,
  });
}

function beginFullCandidateReplacement({ stageRoot, targetRoot, backupRoot, io = fs }) {
  requireSafeDirectory(stageRoot, "新sidecar stage", io);
  const targetStat = io.lstatSync(targetRoot, { throwIfNoEntry: false });
  if (!targetStat) return null;
  if (!targetStat.isDirectory() || targetStat.isSymbolicLink()) fail(`现有候选不安全: ${targetRoot}`);
  if (io.lstatSync(backupRoot, { throwIfNoEntry: false })) fail(`候选保留目录已存在: ${backupRoot}`);
  io.mkdirSync(path.dirname(backupRoot), { recursive: true });
  const backupParent = requireSafeDirectory(path.dirname(backupRoot), "候选保留目录父级", io);
  if (targetStat.dev !== backupParent.dev) fail("候选保留目录必须与候选位于同一卷");
  io.renameSync(targetRoot, backupRoot);
  return { stageRoot, targetRoot, backupRoot };
}

function commitFullCandidateReplacement(transaction, {
  io = fs,
  retainBackupSidecarAsStage = false,
} = {}) {
  requireSafeDirectory(transaction.targetRoot, "新候选", io);
  requireSafeDirectory(transaction.backupRoot, "旧候选保留目录", io);
  if (io.lstatSync(transaction.stageRoot, { throwIfNoEntry: false })) {
    fail("全量构建完成后sidecar stage未被原子消费");
  }
  if (retainBackupSidecarAsStage) {
    const backupSidecar = path.join(transaction.backupRoot, "resources", "sidecar");
    requireSafeDirectory(backupSidecar, "旧候选sidecar", io);
    io.renameSync(backupSidecar, transaction.stageRoot);
  }
  io.rmSync(transaction.backupRoot, { recursive: true, force: false });
}

function rollbackFullCandidateReplacement(transaction, io = fs) {
  const backup = io.lstatSync(transaction.backupRoot, { throwIfNoEntry: false });
  if (!backup) return;
  if (!backup.isDirectory() || backup.isSymbolicLink()) fail("旧候选保留目录不安全，无法回滚");
  const stage = io.lstatSync(transaction.stageRoot, { throwIfNoEntry: false });
  const target = io.lstatSync(transaction.targetRoot, { throwIfNoEntry: false });
  if (stage && (!stage.isDirectory() || stage.isSymbolicLink())) fail("sidecar stage不安全，无法回滚");
  if (target && (!target.isDirectory() || target.isSymbolicLink())) fail("新候选不安全，无法回滚");
  const transferredSidecar = target
    ? path.join(transaction.targetRoot, "resources", "sidecar")
    : null;
  const transferred = transferredSidecar
    ? io.lstatSync(transferredSidecar, { throwIfNoEntry: false })
    : null;
  if (transferred && (!transferred.isDirectory() || transferred.isSymbolicLink())) {
    fail("新候选sidecar不安全，无法回滚");
  }
  if (transferred && stage) fail("sidecar stage与新候选同时存在，无法安全回滚");
  if (transferred) io.renameSync(transferredSidecar, transaction.stageRoot);
  if (target) io.rmSync(transaction.targetRoot, { recursive: true, force: false });
  if (io.lstatSync(transaction.targetRoot, { throwIfNoEntry: false })) {
    fail("候选位置被占用，无法恢复旧候选");
  }
  io.mkdirSync(path.dirname(transaction.targetRoot), { recursive: true });
  io.renameSync(transaction.backupRoot, transaction.targetRoot);
}

function parseArgs(argv) {
  const options = {
    installer: false,
    runE2e: false,
    finalGate: false,
    verifyOnly: false,
    refreshSidecarCache: false,
    useSidecarCache: true,
    refreshCandidateCache: false,
    useCandidateCache: true,
    fastLocalCandidate: false,
    candidateId: null,
  };
  let e2eExplicitlySkipped = false;
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === "--installer") {
      options.installer = true;
      options.runE2e = true;
      options.finalGate = true;
    }
    else if (arg === "--dir") options.installer = false;
    else if (arg === "--skip-e2e") {
      options.runE2e = false;
      e2eExplicitlySkipped = true;
    }
    else if (arg === "--gate") {
      options.runE2e = true;
      options.finalGate = true;
    }
    else if (arg === "--verify-only") options.verifyOnly = true;
    else if (arg === "--refresh-sidecar-cache") options.refreshSidecarCache = true;
    else if (arg === "--no-sidecar-cache") options.useSidecarCache = false;
    else if (arg === "--refresh-candidate-cache") options.refreshCandidateCache = true;
    else if (arg === "--no-candidate-cache") options.useCandidateCache = false;
    else if (arg === "--fast") {
      options.fastLocalCandidate = true;
      options.runE2e = false;
    }
    else if (arg === "--candidate-id") {
      options.candidateId = argv[index + 1] || "";
      index += 1;
    }
    else fail(`未知参数: ${arg}`);
  }
  if (options.refreshSidecarCache && !options.useSidecarCache) {
    fail("--refresh-sidecar-cache不能与--no-sidecar-cache同时使用");
  }
  if (options.refreshCandidateCache && !options.useCandidateCache) {
    fail("--refresh-candidate-cache不能与--no-candidate-cache同时使用");
  }
  if (options.verifyOnly && (options.refreshSidecarCache || !options.useSidecarCache)) {
    fail("verify-only不接受sidecar缓存参数");
  }
  if (options.verifyOnly && (options.refreshCandidateCache || !options.useCandidateCache)) {
    fail("verify-only不接受candidate缓存参数");
  }
  if (options.installer && options.fastLocalCandidate) {
    fail("安装包构建不能跳过真实启动smoke");
  }
  if (options.installer && !options.runE2e) {
    fail("安装包构建必须运行完整Electron E2E");
  }
  if (options.finalGate && e2eExplicitlySkipped) {
    fail("--gate与--skip-e2e不能同时使用");
  }
  if (options.candidateId !== null) assertBuildId(options.candidateId);
  if (options.verifyOnly && options.candidateId === null) {
    fail("verify-only必须指定--candidate-id，避免把历史候选当作当前证据。");
  }
  return options;
}

async function main() {
  const options = parseArgs(process.argv.slice(2));
  assertCandidateNotRunning();
  const candidateId = options.candidateId || createBuildId(sourceCommit());
  const candidate = candidatePaths(candidateId);
  let buildStartedAtMs = null;
  let sidecarTransfer = null;
  let sidecarReplacement = null;
  let frontendReplacement = null;
  let fullCandidateReplacement = null;
  let shellCache = null;

  try {
    if (!options.verifyOnly) {
      if (fs.existsSync(candidate.outputRoot)) fail(`候选目录已存在，拒绝覆盖: ${candidate.outputRoot}`);
      const identity = prepareCandidateIdentity(candidateId);
      console.log(`[windows-build] 候选身份: ${identity.build_id} source ${identity.source_commit.slice(0, 12)}`);
      buildStartedAtMs = Date.now();
      runNpm("验证宠物资源包", ["run", "verify:companion-pack"], ELECTRON_ROOT);
      const frontendCache = await inspectFrontendBuildCache({
        refresh: options.refreshCandidateCache,
        useCache: options.useCandidateCache,
      });
      if (frontendCache.hit) {
        console.log(`[windows-build] 前端构建缓存命中，跳过Vite: ${frontendCache.elapsed_ms} ms`);
      } else {
        console.log(`[windows-build] 前端构建缓存未命中 (${frontendCache.reason})`);
        runNpm("构建前端", ["run", "build"], FRONTEND_ROOT);
        if (options.useCandidateCache) {
          await refreshFrontendBuildCache({ expectedSource: frontendCache.source });
        }
      }
      runNpm("复制前端产物", ["run", "copy:frontend"], ELECTRON_ROOT);
      assertTreesEqual(SOURCE_FRONTEND, STAGED_FRONTEND, "前端复制产物");
      runNpm("准备内置帮助", ["run", "stage:manual"], ELECTRON_ROOT);
      const reuseVerifiedStage = options.useCandidateCache
        && !options.refreshCandidateCache
        && options.useSidecarCache
        && !options.refreshSidecarCache;
      const sidecarStageArgs = ["run", "stage:sidecar", "--", reuseVerifiedStage ? "--reuse-verified-stage" : "--clean"];
      if (options.refreshSidecarCache) sidecarStageArgs.push("--refresh-runtime-cache");
      if (!options.useSidecarCache) sidecarStageArgs.push("--no-runtime-cache");
      runNpm("准备sidecar", sidecarStageArgs, ELECTRON_ROOT);
      const manifestStat = fs.lstatSync(STAGED_SIDECAR_MANIFEST, { throwIfNoEntry: false });
      if (!manifestStat?.isFile() || manifestStat.isSymbolicLink()) {
        fail(`sidecar manifest缺失或不安全: ${STAGED_SIDECAR_MANIFEST}`);
      }
      sidecarTransfer = {
        nonce: crypto.randomBytes(32).toString("hex"),
        manifestSha256: hashFile(STAGED_SIDECAR_MANIFEST),
      };
      shellCache = { hit: false, reason: "isolated_candidate_required" };
      if (shellCache.hit) {
        const replacementStartedAt = Date.now();
        const targetRoot = path.join(ELECTRON_ROOT, "release", "win-unpacked", "resources", "sidecar");
        const backupRoot = path.join(ELECTRON_ROOT, "release", `.chriptmas-sidecar-backup-${sidecarTransfer.nonce}`);
        const proofPath = path.join(ELECTRON_ROOT, "release", proofName(sidecarTransfer.nonce));
        const standbyVerification = options.useCandidateCache && options.useSidecarCache
          ? inspectVerifiedStandbyCandidate(targetRoot)
          : { hit: false, reason: "standby_cache_disabled" };
        console.log(
          `[windows-build] 旧候选standby证明${standbyVerification.hit ? "有效" : `不可复用 (${standbyVerification.reason})`}`,
        );
        sidecarReplacement = beginSidecarReplacement({
          stageRoot: path.join(ELECTRON_ROOT, ".sidecar-stage"),
          targetRoot,
          backupRoot,
          proofPath,
          nonce: sidecarTransfer.nonce,
          expectedManifestSha256: sidecarTransfer.manifestSha256,
        });
        sidecarReplacement.standbyVerification = standbyVerification;
        frontendReplacement = beginDirectoryReplacement({
          sourceRoot: STAGED_FRONTEND,
          targetRoot: PACKAGED_FRONTEND,
          stagingRoot: path.join(ELECTRON_ROOT, "release", `.chriptmas-frontend-stage-${sidecarTransfer.nonce}`),
          backupRoot: path.join(ELECTRON_ROOT, "release", `.chriptmas-frontend-backup-${sidecarTransfer.nonce}`),
        });
        console.log(
          `[windows-build] 候选基础壳缓存命中，跳过Electron Builder并原子替换frontend与sidecar: `
            + `${Date.now() - replacementStartedAt} ms (identity ${shellCache.elapsed_ms} ms)`,
        );
      } else {
        console.log(`[windows-build] 候选桌面壳缓存未命中 (${shellCache.reason})`);
        const candidateRoot = candidate.unpackedRoot;
        const candidateSidecar = path.join(candidateRoot, "resources", "sidecar");
        let standbyVerification = { hit: false, reason: "standby_cache_disabled" };
        if (options.useCandidateCache && options.useSidecarCache) {
          standbyVerification = inspectVerifiedStandbyCandidate(candidateSidecar);
          if (!standbyVerification.hit) {
            console.log(`[windows-build] 旧候选standby证明不可复用 (${standbyVerification.reason})，本轮不保留。`);
          }
        }
        fullCandidateReplacement = beginFullCandidateReplacement({
          stageRoot: path.join(ELECTRON_ROOT, ".sidecar-stage"),
          targetRoot: candidateRoot,
          backupRoot: path.join(candidate.outputRoot, `.chriptmas-candidate-backup-${sidecarTransfer.nonce}`),
        });
        if (fullCandidateReplacement) fullCandidateReplacement.standbyVerification = standbyVerification;
        if (fullCandidateReplacement) console.log("[windows-build] 已原子保留上一完整候选；全量壳只写新目录。");
        const buildArgs = [path.join("scripts", "build.cjs"), "--win"];
        if (!options.installer) buildArgs.push("--dir");
        buildArgs.push("--config.electronDist=node_modules/electron/dist", `--config.directories.output=${candidate.outputRoot}`);
        run(
          options.installer ? "构建Windows安装包" : "构建Windows解压候选",
          process.execPath,
          buildArgs,
          ELECTRON_ROOT,
          {
            ...process.env,
            [SIDECAR_TRANSFER_NONCE_ENV]: sidecarTransfer.nonce,
            [SIDECAR_MANIFEST_SHA_ENV]: sidecarTransfer.manifestSha256,
          },
        );
      }
    }

    if (!fs.existsSync(candidate.executable)) fail(`缺少候选可执行文件: ${candidate.executable}`);
    assertTreesEqual(SOURCE_FRONTEND, candidate.packagedFrontend, "打包前端产物");
    const verifyArgs = [
      "run",
      "verify:build",
      "--",
      "--mode",
      options.installer ? "installer" : "dir",
      "--candidate-dir",
      candidate.outputRoot,
      "--candidate-id",
      candidateId,
    ];
    if (options.installer && buildStartedAtMs !== null) {
      verifyArgs.push("--installer-not-older-than-ms", String(buildStartedAtMs));
    }
    if (sidecarTransfer) {
      verifyArgs.push(
        "--sidecar-transfer-nonce",
        sidecarTransfer.nonce,
        "--sidecar-manifest-sha256",
        sidecarTransfer.manifestSha256,
      );
    }
    if (options.fastLocalCandidate) verifyArgs.push("--skip-packaged-smoke");
    runNpm("验证打包产物", verifyArgs, ELECTRON_ROOT);
    if (sidecarReplacement) {
      const recheckedShell = await inspectCandidateShellCache();
      if (!recheckedShell.hit) {
        fail(`增量候选桌面壳在验证期间发生变化 (${recheckedShell.reason})`);
      }
      console.log(`[windows-build] 增量候选桌面壳复核通过: ${recheckedShell.elapsed_ms} ms`);
    }
    if (options.runE2e) runNpm("运行真实Electron E2E", ["run", "test:e2e:electron"], ELECTRON_ROOT, candidateE2EEnvironment(candidate));

    // Every build receives a fresh candidate directory. Reusing a mutable shell
    // cache here would make the candidate identity ambiguous, so cache refresh
    // is intentionally omitted from the isolated-candidate path.
    if (sidecarReplacement) {
      const committedReplacement = sidecarReplacement;
      sidecarReplacement = null;
      commitSidecarReplacement(committedReplacement, {
        retainBackupAsStage: options.useCandidateCache
          && options.useSidecarCache
          && committedReplacement.standbyVerification?.hit === true,
      });
      if (committedReplacement.standbyVerification?.hit) {
        const refreshed = await refreshCommittedStandbyProof(committedReplacement);
        console.log(
          `[windows-build] 上一个已验证sidecar已轮换为下一轮standby stage；`
            + `runtime proof已刷新，application proof ${refreshed.application.hit ? "已刷新" : `未刷新 (${refreshed.application.reason})`}。`,
        );
      }
    }
    if (frontendReplacement) {
      const committedReplacement = frontendReplacement;
      commitDirectoryReplacement(committedReplacement);
      frontendReplacement = null;
      console.log("[windows-build] frontend原子替换已提交。");
    }
    if (fullCandidateReplacement) {
      const committedCandidate = fullCandidateReplacement;
      fullCandidateReplacement = null;
      commitFullCandidateReplacement(committedCandidate, {
        retainBackupSidecarAsStage: committedCandidate.standbyVerification?.hit === true,
      });
      if (committedCandidate.standbyVerification?.hit) {
        const refreshed = await refreshCommittedStandbyProof(committedCandidate);
        console.log(
          `[windows-build] 全量壳候选已提交，上一候选sidecar已轮换为standby stage；`
            + `runtime proof已刷新，application proof ${refreshed.application.hit ? "已刷新" : `未刷新 (${refreshed.application.reason})`}。`,
        );
      } else {
        console.log("[windows-build] 全量壳候选已提交；上一完整候选已删除。");
      }
    }

    if (options.fastLocalCandidate) {
      console.log("\n[windows-build] 快速本地候选构建与完整性验证通过；发布Gate尚未执行真实启动smoke。");
    } else if (options.finalGate) {
      console.log("\n[windows-build] 最终候选Gate通过：完整性、真实启动smoke与全量Electron E2E均已验证。");
    } else {
      console.log("\n[windows-build] 已验证候选构建通过：完整性与真实启动smoke已验证；全量Electron E2E请运行build:windows:gate。");
    }
    console.log(`[windows-build] 可执行文件: ${candidate.executable}`);
  } finally {
    if (frontendReplacement) {
      try {
        rollbackDirectoryReplacement(frontendReplacement);
        console.error("[windows-build] 增量候选验证未完成，已恢复上一个frontend。");
      } catch (rollbackError) {
        console.error(`[windows-build] 增量frontend回滚失败: ${rollbackError.message}`);
      }
    }
    if (sidecarReplacement) {
      try {
        rollbackSidecarReplacement(sidecarReplacement);
        console.error("[windows-build] 增量候选验证未完成，已恢复上一个sidecar。");
      } catch (rollbackError) {
        console.error(`[windows-build] 增量sidecar回滚失败: ${rollbackError.message}`);
      }
    }
    if (fullCandidateReplacement) {
      try {
        rollbackFullCandidateReplacement(fullCandidateReplacement);
        console.error("[windows-build] 全量壳构建未完成，已恢复上一完整候选并保留新sidecar stage。");
      } catch (rollbackError) {
        console.error(`[windows-build] 全量壳候选回滚失败: ${rollbackError.message}`);
      }
    }
    const proofPath = sidecarTransfer
      ? path.join(ELECTRON_ROOT, "release", `.chriptmas-sidecar-transfer-${sidecarTransfer.nonce}.json`)
      : null;
    if (proofPath && fs.existsSync(proofPath)) {
      fs.rmSync(proofPath, { force: true });
    }
  }
}

if (require.main === module) {
  main().catch((error) => {
    console.error(`\n[windows-build] 失败: ${error.message}`);
    process.exitCode = 1;
  });
}

module.exports = {
  candidateE2EEnvironment,
  candidatePaths,
  createBuildId,
  beginFullCandidateReplacement,
  commitFullCandidateReplacement,
  inspectVerifiedStandbyCandidate,
  main,
  parseArgs,
  prepareCandidateIdentity,
  rollbackFullCandidateReplacement,
  runNpm,
  sourceCommit,
};
