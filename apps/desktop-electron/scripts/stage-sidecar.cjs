const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");
const { verifySidecarMetadata } = require("./verify-sidecar.cjs");
const { assertPackageClean, assertPythonRuntime, clearSidecarStage } = require("./package-guard.cjs");

const ELECTRON_ROOT = path.resolve(__dirname, "..");
const PROJECT_ROOT = path.resolve(ELECTRON_ROOT, "..", "..");
const STAGE_ROOT = path.join(ELECTRON_ROOT, ".sidecar-stage");
const INPUTS = [
  [path.resolve(process.env.CHRIPTMAS_SIDECAR_PYTHON_RUNTIME || path.join(PROJECT_ROOT, "python-runtime")), path.join(STAGE_ROOT, "runtime"), false],
  [path.join(PROJECT_ROOT, "src", "backend"), path.join(STAGE_ROOT, "backend"), true],
  [path.join(PROJECT_ROOT, "src", "core"), path.join(STAGE_ROOT, "core"), true],
  [path.join(PROJECT_ROOT, "src", "rebuild"), path.join(STAGE_ROOT, "rebuild"), true],
  [path.join(PROJECT_ROOT, "config", "rebuild.toml.example"), path.join(STAGE_ROOT, "config", "rebuild.toml.example"), true],
  [path.join(PROJECT_ROOT, "config", "settings.toml.example"), path.join(STAGE_ROOT, "config", "settings.toml"), true],
  [path.join(PROJECT_ROOT, "config", "codex-hooks.toml"), path.join(STAGE_ROOT, "config", "codex-hooks.toml"), true],
  [path.join(PROJECT_ROOT, "requirements.txt"), path.join(STAGE_ROOT, "requirements.txt"), true],
  [path.join(PROJECT_ROOT, "tools", "scripts", "verify-runtime-dependencies.py"), path.join(STAGE_ROOT, "verify-runtime-dependencies.py"), true],
];
const RUNTIME_SOURCE = INPUTS[0][0];
const RUNTIME_TARGET = INPUTS[0][1];
const RUNTIME_HASH_CACHE_PATH = path.join(ELECTRON_ROOT, ".cache", "sidecar-runtime-hashes-v1.json");
const RUNTIME_BYTECODE_CACHE_ROOT = path.join(ELECTRON_ROOT, ".cache", "sidecar-runtime-bytecode-v1");
const STAGE_REUSE_PROOF_ROOT = path.join(ELECTRON_ROOT, ".cache", "sidecar-stage-reuse-v1");
const APPLICATION_STAGE_PROOF_ROOT = path.join(ELECTRON_ROOT, ".cache", "sidecar-application-stage-v1");
const RUNTIME_HASH_CACHE_SCHEMA = "1.0.0";
const STAGE_REUSE_SCHEMA = "1.0.0";
const APPLICATION_STAGE_SCHEMA = "1.0.0";
const MAX_STAGE_REUSE_PROOFS = 4;
const ROBOCOPY_THREADS = 8;
const HASH_CONCURRENCY = 8;

const DEVELOPMENT_ONLY_EXTENSIONS = new Set([".a", ".gir", ".h", ".hpp", ".lib", ".pdb"]);
const EXCLUDED_DIRECTORY_NAMES = Object.freeze(["__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"]);
const PRODUCTION_EXCLUDED_DIRECTORY_NAMES = Object.freeze([
  "_pytest",
  "pytest",
  "pytest_asyncio",
  "importlinter",
  "grimp",
  "pip",
  "wheel",
  "idlelib",
  "lib2to3",
  "turtledemo",
  "test",
  "ensurepip",
]);
const PRODUCTION_EXCLUDED_DIRECTORY_PATTERNS = Object.freeze([
  /^pytest(?:[-_].*)?\.dist-info$/i,
  /^import[-_]linter(?:[-_].*)?\.dist-info$/i,
  /^grimp(?:[-_].*)?\.dist-info$/i,
  /^pip(?:[-_].*)?\.dist-info$/i,
  /^wheel(?:[-_].*)?\.dist-info$/i,
]);
const PRODUCTION_EXCLUDED_FILES = Object.freeze([
  "pip.exe",
  "pip3.exe",
  "pip-script.py",
  "pip3-script.py",
  "pytest.exe",
  "wheel.exe",
  "wheel-script.py",
]);
const PRODUCTION_EXCLUDED_RUNTIME_PATHS = Object.freeze([
  "runtime/Library/share/doc",
  "runtime/Library/share/man",
  "runtime/Library/share/info",
  "runtime/share/doc",
  "runtime/share/man",
]);
const NATIVE_EXCLUDED_FILES = Object.freeze([
  ".gitkeep",
  "*.pyc",
  ...PRODUCTION_EXCLUDED_FILES,
  ...[...DEVELOPMENT_ONLY_EXTENSIONS].map((extension) => `*${extension}`),
]);
const NATIVE_EXCLUDED_DIRECTORIES = Object.freeze([
  ...EXCLUDED_DIRECTORY_NAMES,
  ...PRODUCTION_EXCLUDED_DIRECTORY_NAMES,
  "pytest-*.dist-info",
  "pytest_*.dist-info",
  "import-linter-*.dist-info",
  "import_linter-*.dist-info",
  "grimp-*.dist-info",
  "pip-*.dist-info",
  "wheel-*.dist-info",
]);
const BASE_PACK = Object.freeze({
  pack_id: "chriptmas-windows-cpu-base",
  role: "required",
  platform: "win32",
  arch: "x64",
  capabilities: [
    "authenticated_sidecar",
    "local_intake",
    "sqlite_authority",
    "document_memory_project_skill",
    "file_grant_streaming",
    "ffmpeg_media_probe",
    "provider_policy",
  ],
});

function policyDigest(value) {
  return crypto.createHash("sha256").update(JSON.stringify(value), "utf8").digest("hex");
}

const RUNTIME_HASH_POLICY = `stage-sidecar-runtime:${policyDigest({
  version: "2.0.0",
  base_pack: BASE_PACK,
  development_extensions: [...DEVELOPMENT_ONLY_EXTENSIONS].sort(),
  excluded_directories: EXCLUDED_DIRECTORY_NAMES,
  production_excluded_directories: PRODUCTION_EXCLUDED_DIRECTORY_NAMES,
  production_excluded_directory_patterns: PRODUCTION_EXCLUDED_DIRECTORY_PATTERNS.map(String),
  production_excluded_files: PRODUCTION_EXCLUDED_FILES,
  production_excluded_runtime_paths: PRODUCTION_EXCLUDED_RUNTIME_PATHS,
  native_excluded_files: NATIVE_EXCLUDED_FILES,
  should_stage: String(shouldStage),
  runtime_path_exclusion: String(isRuntimePathExcluded),
  package_guard: fs.readFileSync(path.join(__dirname, "package-guard.cjs"), "utf8"),
  runtime_copy: String(copyRuntimeWithRobocopy),
  bytecode_copy: String(copyRuntimeBytecodeWithRobocopy),
  application_compile: String(compileApplicationBytecode),
})}`;
const STAGE_REUSE_POLICY = `verified-stage:${RUNTIME_HASH_POLICY}`;
const APPLICATION_STAGE_POLICY = `stage-sidecar-application:${policyDigest({
  version: "1.0.0",
  inputs: INPUTS.slice(1).map(([source, target]) => [
    path.relative(PROJECT_ROOT, source).replaceAll(path.sep, "/"),
    path.relative(STAGE_ROOT, target).replaceAll(path.sep, "/"),
  ]),
  should_stage: String(shouldStage),
  copy: String(copy),
  tracked_files: String(listTrackedApplicationFiles),
  application_source: String(isApplicationSource),
  tracked_path: String(isTrackedPath),
  application_entries: String(collectApplicationSourceEntries),
  package_guard: fs.readFileSync(path.join(__dirname, "package-guard.cjs"), "utf8"),
  application_compile: String(compileApplicationBytecode),
})}`;

function isRuntimePathExcluded(sourcePath) {
  const normalized = String(sourcePath).replaceAll("\\", "/");
  const relative = path.relative(RUNTIME_SOURCE, path.resolve(sourcePath));
  const sourceRuntimePath = relative && relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative)
    ? `runtime/${relative.replaceAll(path.sep, "/")}` : null;
  return PRODUCTION_EXCLUDED_RUNTIME_PATHS.some((excluded) => normalized === excluded
    || normalized.endsWith(`/${excluded}`) || sourceRuntimePath === excluded);
}

function shouldStage(sourcePath) {
  const basename = path.basename(sourcePath);
  if (EXCLUDED_DIRECTORY_NAMES.includes(basename)
      || PRODUCTION_EXCLUDED_DIRECTORY_NAMES.includes(basename)
      || PRODUCTION_EXCLUDED_DIRECTORY_PATTERNS.some((pattern) => pattern.test(basename))
      || PRODUCTION_EXCLUDED_FILES.includes(basename)
      || isRuntimePathExcluded(sourcePath)
      || basename === ".gitkeep") return false;
  const extension = path.extname(sourcePath).toLowerCase();
  if (extension === ".pyc" || DEVELOPMENT_ONLY_EXTENSIONS.has(extension)) return false;
  return true;
}

function shouldStageRuntimeBytecode(sourcePath) {
  if (path.extname(sourcePath).toLowerCase() !== ".pyc") return false;
  const parts = String(sourcePath).replaceAll("\\", "/").split("/");
  return !parts.some((part) => PRODUCTION_EXCLUDED_DIRECTORY_NAMES.includes(part)
    || PRODUCTION_EXCLUDED_DIRECTORY_PATTERNS.some((pattern) => pattern.test(part)));
}

function isApplicationSource(source, projectRoot = PROJECT_ROOT) {
  return ["backend", "core", "rebuild"].some((name) => path.resolve(source) === path.join(path.resolve(projectRoot), "src", name));
}

function listTrackedApplicationFiles(projectRoot = PROJECT_ROOT, run = spawnSync) {
  const result = run("git", ["ls-files", "-z", "--", "src/backend", "src/core", "src/rebuild"], {
    cwd: projectRoot, encoding: "utf8", windowsHide: true,
  });
  if (result.error || result.status !== 0 || typeof result.stdout !== "string") {
    throw new Error("Git is required to stage tracked application files");
  }
  const files = result.stdout.split("\0").filter(Boolean);
  if (files.some((relative) => relative.includes("\\") || path.posix.normalize(relative) !== relative
      || !/^src\/(backend|core|rebuild)\//.test(relative) || relative.includes(":"))) {
    throw new Error("Git returned an unsafe application file path");
  }
  return new Set(files.map((relative) => path.resolve(projectRoot, ...relative.split("/"))));
}

function isTrackedPath(source, trackedFiles, directory) {
  const resolved = path.resolve(source);
  return trackedFiles.has(resolved) || (directory && [...trackedFiles].some((file) => file.startsWith(`${resolved}${path.sep}`)));
}

function copy(source, target, refresh, trackedFiles = null) {
  if (!fs.existsSync(source)) throw new Error(`sidecar input missing: ${source}`);
  if (fs.existsSync(target) && !refresh) return;
  if (fs.existsSync(target) && refresh) fs.rmSync(target, { recursive: true, force: true });
  fs.cpSync(source, target, {
    recursive: true,
    force: refresh,
    errorOnExist: false,
    verbatimSymlinks: true,
    filter: (current) => shouldStage(current) && (trackedFiles === null
      || isTrackedPath(current, trackedFiles, fs.lstatSync(current).isDirectory())),
  });
}

function assertNoLinkedEntries(root, io = fs) {
  const rootStat = io.lstatSync(root, { throwIfNoEntry: false });
  if (!rootStat?.isDirectory() || rootStat.isSymbolicLink()) {
    throw new Error(`sidecar input must be a regular non-symlink directory: ${root}`);
  }
  for (const entry of io.readdirSync(root, { withFileTypes: true })) {
    const full = path.join(root, entry.name);
    if (entry.isSymbolicLink()) {
      throw new Error(`sidecar input contains an unsupported linked entry: ${full}`);
    }
    if (entry.isDirectory()) assertNoLinkedEntries(full, io);
    else if (!entry.isFile()) throw new Error(`sidecar input contains an unsupported entry: ${full}`);
  }
}

function copyRuntimeWithRobocopy(
  source,
  target,
  refresh,
  {
    io = fs,
    run = spawnSync,
    platform = process.platform,
    validateLinks = true,
  } = {},
) {
  if (platform !== "win32") {
    copy(source, target, refresh);
    return { method: "fs.cpSync", status: 0 };
  }
  if (validateLinks) assertNoLinkedEntries(source, io);
  if (io.existsSync(target) && !refresh) return { method: "reused", status: 0 };
  if (io.existsSync(target) && refresh) io.rmSync(target, { recursive: true, force: true });
  io.mkdirSync(path.dirname(target), { recursive: true });
  const args = [
    source,
    target,
    "/E",
    "/COPY:DAT",
    "/DCOPY:DAT",
    "/R:1",
    "/W:1",
    `/MT:${ROBOCOPY_THREADS}`,
    "/XJ",
    "/NFL",
    "/NDL",
    "/NJH",
    "/NJS",
    "/NP",
    "/XF",
    ...NATIVE_EXCLUDED_FILES,
    "/XD",
    ...NATIVE_EXCLUDED_DIRECTORIES,
  ];
  const result = run("robocopy.exe", args, {
    encoding: "utf8",
    windowsHide: true,
  });
  if (result.error) throw result.error;
  if (!Number.isInteger(result.status) || result.status < 0 || result.status >= 8) {
    const detail = String(result.stderr || result.stdout || "").trim();
    throw new Error(`runtime robocopy failed (${result.status ?? "unknown"})${detail ? `: ${detail}` : ""}`);
  }
  const targetStat = io.lstatSync(target, { throwIfNoEntry: false });
  if (!targetStat?.isDirectory() || targetStat.isSymbolicLink()) {
    throw new Error("runtime robocopy did not create a safe target directory");
  }
  return { method: "robocopy", status: result.status };
}

function pruneUnstagedArtifacts(root, { preserveBytecode = false } = {}) {
  if (!fs.existsSync(root)) return;
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const full = path.join(root, entry.name);
    const retainedBytecode = preserveBytecode
      && (entry.name === "__pycache__" || path.extname(full).toLowerCase() === ".pyc");
    if (!shouldStage(full) && !retainedBytecode) {
      fs.rmSync(full, { recursive: true, force: true });
    } else if (entry.isDirectory()) {
      pruneUnstagedArtifacts(full, { preserveBytecode });
    }
  }
}

function compileApplicationBytecode(stageRoot = STAGE_ROOT, run = spawnSync) {
  const python = path.join(stageRoot, "runtime", process.platform === "win32" ? "python.exe" : "python");
  const runtimeRoot = fs.mkdtempSync(path.join(os.tmpdir(), "chriptmas-sidecar-build-"));
  const stagedConfig = path.join(stageRoot, "config");
  if (fs.existsSync(stagedConfig)) {
    fs.cpSync(stagedConfig, path.join(runtimeRoot, "config"), { recursive: true });
  }
  const script = [
    "import importlib.util, pathlib, py_compile, sys",
    `root = pathlib.Path(${JSON.stringify(stageRoot)}).resolve()`,
    "preexisting = {cache.resolve() for cache in root.rglob('*.pyc')}",
    "import backend.api.app",
    "sources = sorted({pathlib.Path(module.__file__).resolve() for module in tuple(sys.modules.values()) if getattr(module, '__file__', None) and str(module.__file__).endswith('.py') and pathlib.Path(module.__file__).resolve().is_relative_to(root)})",
    "for source in sources:",
    "    cache = pathlib.Path(importlib.util.cache_from_source(str(source))).resolve()",
    "    if cache not in preexisting: py_compile.compile(str(source), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)",
    "print(len(sources))",
  ].join("\n");
  let result;
  try {
    result = run(
      python,
      ["-c", script],
      {
        cwd: stageRoot,
        encoding: "utf8",
        env: {
          ...process.env,
          PYTHONPATH: stageRoot,
          CHRIPTMAS_APP_ROOT: runtimeRoot,
          PYTHONDONTWRITEBYTECODE: "",
        },
        timeout: 300000,
        windowsHide: true,
      },
    );
    if (result.error) throw result.error;
    if (result.status !== 0) {
      throw new Error(`sidecar bytecode compilation failed (${result.status}): ${(result.stderr || result.stdout || "").trim()}`);
    }
  } finally {
    fs.rmSync(runtimeRoot, { recursive: true, force: true });
  }
  const compiled = Number.parseInt(String(result.stdout || "").trim(), 10);
  if (compiled < 100) throw new Error("sidecar bytecode compilation produced an incomplete startup cache");
  return compiled;
}

function copyRuntimeBytecodeWithRobocopy(
  source,
  target,
  refresh,
  {
    io = fs,
    run = spawnSync,
    platform = process.platform,
  } = {},
) {
  if (!io.existsSync(source)) throw new Error(`runtime bytecode source missing: ${source}`);
  assertNoLinkedEntries(source, io);
  if (io.existsSync(target) && refresh) io.rmSync(target, { recursive: true, force: true });
  if (platform !== "win32") {
    io.mkdirSync(target, { recursive: true });
    for (const file of walk(source)) {
      if (!shouldStageRuntimeBytecode(file)) continue;
      const destination = path.join(target, path.relative(source, file));
      io.mkdirSync(path.dirname(destination), { recursive: true });
      io.copyFileSync(file, destination);
    }
    return { method: "fs.copyFileSync", status: 0 };
  }
  io.mkdirSync(path.dirname(target), { recursive: true });
  const result = run("robocopy.exe", [
    source,
    target,
    "*.pyc",
    "/S",
    "/COPY:DAT",
    "/DCOPY:DAT",
    "/R:1",
    "/W:1",
    `/MT:${ROBOCOPY_THREADS}`,
    "/XJ",
    "/NFL",
    "/NDL",
    "/NJH",
    "/NJS",
    "/NP",
    "/XD",
    ...NATIVE_EXCLUDED_DIRECTORIES,
  ], { encoding: "utf8", windowsHide: true });
  if (result.error) throw result.error;
  if (!Number.isInteger(result.status) || result.status < 0 || result.status >= 8) {
    const detail = String(result.stderr || result.stdout || "").trim();
    throw new Error(`runtime bytecode robocopy failed (${result.status ?? "unknown"})${detail ? `: ${detail}` : ""}`);
  }
  const targetStat = io.lstatSync(target, { throwIfNoEntry: false });
  if (!targetStat?.isDirectory() || targetStat.isSymbolicLink()) {
    throw new Error("runtime bytecode robocopy did not create a safe target directory");
  }
  return { method: "robocopy", status: result.status };
}

function walk(root) {
  const files = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    if (root === STAGE_ROOT && entry.name === "sidecar-manifest.json") continue;
    const full = path.join(root, entry.name);
    if (entry.isDirectory()) files.push(...walk(full));
    else if (entry.isFile()) files.push(full);
  }
  return files;
}

function sha256(file) {
  const hash = crypto.createHash("sha256");
  const handle = fs.openSync(file, "r");
  const buffer = Buffer.allocUnsafe(1024 * 1024);
  try {
    for (;;) {
      const bytesRead = fs.readSync(handle, buffer, 0, buffer.length, null);
      if (bytesRead === 0) break;
      hash.update(buffer.subarray(0, bytesRead));
    }
  } finally {
    fs.closeSync(handle);
  }
  return hash.digest("hex");
}

function snapshotRuntimeSource(root = RUNTIME_SOURCE, io = fs, { includeBytecode = false } = {}) {
  const rootStat = io.lstatSync(root, { throwIfNoEntry: false });
  if (!rootStat?.isDirectory() || rootStat.isSymbolicLink()) {
    throw new Error(`sidecar input must be a regular non-symlink directory: ${root}`);
  }
  const files = [];
  function visit(directory) {
    for (const entry of io.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      const isBytecode = path.extname(absolute).toLowerCase() === ".pyc";
      const isBytecodeDirectory = includeBytecode && entry.isDirectory() && entry.name === "__pycache__";
      if (!shouldStage(absolute) && !(includeBytecode && isBytecode) && !isBytecodeDirectory) continue;
      const stat = io.lstatSync(absolute, { bigint: true });
      if (stat.isSymbolicLink()) {
        throw new Error(`sidecar input contains an unsupported linked entry: ${absolute}`);
      }
      if (stat.isDirectory()) visit(absolute);
      else if (stat.isFile()) {
        files.push({
          path: path.relative(root, absolute).replaceAll(path.sep, "/"),
          size: Number(stat.size),
          mtime_ns: stat.mtimeNs.toString(),
          ctime_ns: stat.ctimeNs.toString(),
          dev: stat.dev.toString(),
          ino: stat.ino.toString(),
        });
      } else {
        throw new Error(`sidecar input contains an unsupported entry: ${absolute}`);
      }
    }
  }
  visit(root);
  files.sort((left, right) => left.path.localeCompare(right.path));
  const canonical = files
    .map((file) => `${file.path}\0${file.size}\0${file.mtime_ns}\0${file.ctime_ns}\0${file.dev}\0${file.ino}`)
    .join("\n");
  return {
    files,
    fingerprint: crypto.createHash("sha256").update(canonical, "utf8").digest("hex"),
  };
}

function snapshotApplicationStage(stageRoot = STAGE_ROOT, io = fs) {
  const rootStat = io.lstatSync(stageRoot, { throwIfNoEntry: false });
  if (!rootStat?.isDirectory() || rootStat.isSymbolicLink()) {
    throw new Error(`sidecar application stage must be a regular non-symlink directory: ${stageRoot}`);
  }
  const files = [];
  function visit(directory) {
    for (const entry of io.readdirSync(directory, { withFileTypes: true })) {
      const absolute = path.join(directory, entry.name);
      const relative = path.relative(stageRoot, absolute).replaceAll(path.sep, "/");
      if (relative === "runtime" || relative.startsWith("runtime/")) continue;
      if (relative === "sidecar-profile.json" || relative === "sidecar-manifest.json") continue;
      const stat = io.lstatSync(absolute, { bigint: true });
      if (stat.isSymbolicLink()) throw new Error(`sidecar application stage contains a linked entry: ${absolute}`);
      if (stat.isDirectory()) visit(absolute);
      else if (stat.isFile()) {
        files.push({
          path: relative,
          size: Number(stat.size),
          mtime_ns: stat.mtimeNs.toString(),
          ctime_ns: stat.ctimeNs.toString(),
          dev: stat.dev.toString(),
          ino: stat.ino.toString(),
        });
      } else {
        throw new Error(`sidecar application stage contains an unsupported entry: ${absolute}`);
      }
    }
  }
  visit(stageRoot);
  files.sort((left, right) => left.path.localeCompare(right.path));
  const canonical = files
    .map((file) => `${file.path}\0${file.size}\0${file.mtime_ns}\0${file.ctime_ns}\0${file.dev}\0${file.ino}`)
    .join("\n");
  return { files, fingerprint: crypto.createHash("sha256").update(canonical, "utf8").digest("hex") };
}

function collectApplicationSourceEntries(inputs = INPUTS.slice(1), io = fs, stageRoot = STAGE_ROOT, {
  projectRoot = PROJECT_ROOT, run = spawnSync,
} = {}) {
  const files = [];
  const trackedFiles = inputs.some(([source]) => isApplicationSource(source, projectRoot))
    ? listTrackedApplicationFiles(projectRoot, run) : null;
  function visit(sourceRoot, targetRoot, current, tracked) {
    const stat = io.lstatSync(current, { bigint: true, throwIfNoEntry: false });
    if (!stat) throw new Error(`sidecar input missing: ${current}`);
    if (tracked && !isTrackedPath(current, trackedFiles, stat.isDirectory())) return;
    if (stat.isSymbolicLink()) throw new Error(`sidecar input contains an unsupported linked entry: ${current}`);
    if (!shouldStage(current)) return;
    if (stat.isDirectory()) {
      for (const entry of io.readdirSync(current, { withFileTypes: true })) {
        visit(sourceRoot, targetRoot, path.join(current, entry.name), tracked);
      }
      return;
    }
    if (!stat.isFile()) throw new Error(`sidecar input contains an unsupported entry: ${current}`);
    const destination = sourceRoot === current ? targetRoot : path.join(targetRoot, path.relative(sourceRoot, current));
    const relative = path.relative(stageRoot, destination).replaceAll(path.sep, "/");
    if (!relative || relative === ".." || relative.startsWith("../") || path.win32.isAbsolute(relative)) {
      throw new Error(`sidecar application input escapes its stage root: ${destination}`);
    }
    files.push({ absolute: current, path: relative, size: Number(stat.size) });
  }
  for (const [source, target] of inputs) visit(source, target, source, isApplicationSource(source, projectRoot));
  files.sort((left, right) => left.path.localeCompare(right.path));
  if (new Set(files.map((file) => file.path)).size !== files.length) {
    throw new Error("sidecar application inputs contain duplicate stage paths");
  }
  return files;
}

async function snapshotApplicationSources(inputs = INPUTS.slice(1), options = {}) {
  const entries = collectApplicationSourceEntries(inputs, options.io || fs, options.stageRoot || STAGE_ROOT, options);
  const hashed = await hashFileEntries(entries, { concurrency: options.concurrency || HASH_CONCURRENCY, hash: options.hash || sha256Async });
  return { files: hashed.files, content_set_sha256: contentSetSha256(hashed.files) };
}

function validApplicationFile(file) {
  const relative = typeof file?.path === "string" ? file.path : "";
  return file
    && relative
    && !relative.startsWith("runtime/")
    && relative !== "runtime"
    && relative !== "sidecar-profile.json"
    && relative !== "sidecar-manifest.json"
    && !relative.includes("\\")
    && path.posix.normalize(relative) === relative
    && relative !== ".."
    && !relative.startsWith("../")
    && !path.win32.isAbsolute(relative)
    && !relative.includes(":")
    && Number.isSafeInteger(file.size)
    && file.size >= 0
    && /^[a-f0-9]{64}$/.test(file.sha256 || "");
}

function applicationSnapshotMatchesFiles(snapshot, files) {
  if (!snapshot || !Array.isArray(snapshot.files) || !Array.isArray(files)) return false;
  const expected = [...files].sort((left, right) => left.path.localeCompare(right.path));
  return expected.length === snapshot.files.length
    && snapshot.files.every((file, index) => file.path === expected[index].path && file.size === expected[index].size);
}

function applicationContainsSourceSnapshot(sourceSnapshot, applicationFiles) {
  if (!sourceSnapshot || !Array.isArray(sourceSnapshot.files) || !Array.isArray(applicationFiles)) return false;
  const staged = new Map(applicationFiles.map((file) => [file.path, file]));
  return sourceSnapshot.files.every((source) => {
    const file = staged.get(source.path);
    return file && file.size === source.size && file.sha256 === source.sha256;
  });
}

function readApplicationStageProof(sourceSnapshot, {
  stageRoot = STAGE_ROOT,
  proofRoot = APPLICATION_STAGE_PROOF_ROOT,
  io = fs,
} = {}) {
  try {
    const stageSnapshot = snapshotApplicationStage(stageRoot, io);
    const proofPath = path.join(proofRoot, `${stageSnapshot.fingerprint}.json`);
    const proofStat = io.lstatSync(proofPath, { throwIfNoEntry: false });
    if (!proofStat?.isFile() || proofStat.isSymbolicLink() || proofStat.size > 32 * 1024 * 1024) {
      return { hit: false, reason: "application_proof_missing_or_unsafe", files: [] };
    }
    const proof = JSON.parse(io.readFileSync(proofPath, "utf8"));
    if (proof?.schema_version !== APPLICATION_STAGE_SCHEMA
        || proof?.policy !== APPLICATION_STAGE_POLICY
        || proof?.source_content_set_sha256 !== sourceSnapshot.content_set_sha256
        || proof?.stage_fingerprint !== stageSnapshot.fingerprint
        || proof?.stage_files !== stageSnapshot.files.length
        || !Number.isSafeInteger(proof?.compiled_modules)
        || proof.compiled_modules < 100
        || !Array.isArray(proof?.files)
        || proof.files.length === 0
        || proof.files.some((file) => !validApplicationFile(file))
        || new Set(proof.files.map((file) => file.path)).size !== proof.files.length
        || !applicationSnapshotMatchesFiles(stageSnapshot, proof.files)
        || !applicationContainsSourceSnapshot(sourceSnapshot, proof.files)
        || proof.application_content_set_sha256 !== contentSetSha256(proof.files)
        || typeof proof?.generated_at !== "string") {
      return { hit: false, reason: "application_proof_mismatch", files: [] };
    }
    return { hit: true, reason: "application_cache_hit", proofPath, stageSnapshot, files: proof.files, compiled: proof.compiled_modules };
  } catch {
    return { hit: false, reason: "application_proof_invalid", files: [] };
  }
}

function writeApplicationStageProof(sourceSnapshot, applicationFiles, compiled, {
  stageRoot = STAGE_ROOT,
  proofRoot = APPLICATION_STAGE_PROOF_ROOT,
  io = fs,
  now = () => new Date(),
} = {}) {
  if (!Array.isArray(applicationFiles) || applicationFiles.length === 0 || applicationFiles.some((file) => !validApplicationFile(file))) {
    throw new Error("application stage proof received invalid files");
  }
  if (!Number.isSafeInteger(compiled) || compiled < 100) throw new Error("application stage proof received an invalid module count");
  if (!applicationContainsSourceSnapshot(sourceSnapshot, applicationFiles)) {
    throw new Error("verified stage application does not contain its canonical source inputs");
  }
  const stageSnapshot = snapshotApplicationStage(stageRoot, io);
  if (!applicationSnapshotMatchesFiles(stageSnapshot, applicationFiles)) {
    throw new Error("verified stage application does not match its cryptographic manifest");
  }
  const proofRootStat = io.lstatSync(proofRoot, { throwIfNoEntry: false });
  if (proofRootStat && (!proofRootStat.isDirectory() || proofRootStat.isSymbolicLink())) {
    throw new Error("application stage proof root is unsafe");
  }
  io.mkdirSync(proofRoot, { recursive: true });
  const proofPath = path.join(proofRoot, `${stageSnapshot.fingerprint}.json`);
  const proof = {
    schema_version: APPLICATION_STAGE_SCHEMA,
    policy: APPLICATION_STAGE_POLICY,
    source_content_set_sha256: sourceSnapshot.content_set_sha256,
    stage_fingerprint: stageSnapshot.fingerprint,
    stage_files: stageSnapshot.files.length,
    application_content_set_sha256: contentSetSha256(applicationFiles),
    compiled_modules: compiled,
    files: applicationFiles,
    generated_at: now().toISOString(),
  };
  const temporary = `${proofPath}.tmp-${process.pid}-${crypto.randomBytes(8).toString("hex")}`;
  io.writeFileSync(temporary, `${JSON.stringify(proof)}\n`, { encoding: "utf8", flag: "wx", mode: 0o600 });
  try {
    io.rmSync(proofPath, { force: true });
    io.renameSync(temporary, proofPath);
  } finally {
    io.rmSync(temporary, { force: true });
  }
  const proofs = io.readdirSync(proofRoot, { withFileTypes: true })
    .map((entry) => {
      const absolute = path.join(proofRoot, entry.name);
      const stat = io.lstatSync(absolute);
      if (!entry.isFile() || stat.isSymbolicLink() || !/^[a-f0-9]{64}\.json$/.test(entry.name)) {
        throw new Error(`application stage proof root contains an unsafe entry: ${absolute}`);
      }
      return { absolute, mtimeMs: stat.mtimeMs };
    })
    .sort((left, right) => right.mtimeMs - left.mtimeMs || left.absolute.localeCompare(right.absolute));
  for (const stale of proofs.slice(MAX_STAGE_REUSE_PROOFS)) io.rmSync(stale.absolute, { force: true });
  return { proofPath, stageSnapshot };
}

function findApplicationCompiledModules(applicationFiles, {
  proofRoot = APPLICATION_STAGE_PROOF_ROOT,
  io = fs,
} = {}) {
  const expectedContentSet = contentSetSha256(applicationFiles);
  const proofRootStat = io.lstatSync(proofRoot, { throwIfNoEntry: false });
  if (!proofRootStat) return null;
  if (!proofRootStat.isDirectory() || proofRootStat.isSymbolicLink()) {
    throw new Error("application stage proof root is unsafe");
  }
  for (const entry of io.readdirSync(proofRoot, { withFileTypes: true })) {
    const absolute = path.join(proofRoot, entry.name);
    const stat = io.lstatSync(absolute);
    if (!entry.isFile() || stat.isSymbolicLink() || !/^[a-f0-9]{64}\.json$/.test(entry.name)) {
      throw new Error(`application stage proof root contains an unsafe entry: ${absolute}`);
    }
    if (stat.size > 32 * 1024 * 1024) continue;
    try {
      const proof = JSON.parse(io.readFileSync(absolute, "utf8"));
      if (proof?.schema_version === APPLICATION_STAGE_SCHEMA
          && proof?.policy === APPLICATION_STAGE_POLICY
          && proof?.application_content_set_sha256 === expectedContentSet
          && Number.isSafeInteger(proof?.compiled_modules)
          && proof.compiled_modules >= 100
          && Array.isArray(proof?.files)
          && proof.files.length === applicationFiles.length
          && proof.files.every((file, index) => (
            file.path === applicationFiles[index].path
            && file.size === applicationFiles[index].size
            && file.sha256 === applicationFiles[index].sha256
          ))) {
        return proof.compiled_modules;
      }
    } catch {
      // An unrelated malformed proof is not authority for this content set.
    }
  }
  return null;
}

async function refreshRotatedStageProofs({
  stageRoot = STAGE_ROOT,
  verifiedSidecar,
  expectedManifestSha256,
  runtimeSourceRoot = RUNTIME_SOURCE,
  applicationInputs = INPUTS.slice(1),
  runtimeProofRoot = STAGE_REUSE_PROOF_ROOT,
  applicationProofRoot = APPLICATION_STAGE_PROOF_ROOT,
  compiledModules = null,
  io = fs,
} = {}) {
  if (!verifiedSidecar
      || !Number.isSafeInteger(verifiedSidecar.files)
      || !Number.isSafeInteger(verifiedSidecar.total_size)
      || !/^[a-f0-9]{64}$/.test(verifiedSidecar.content_set_sha256 || "")
      || !/^[a-f0-9]{64}$/.test(expectedManifestSha256 || "")) {
    throw new Error("rotated standby proof requires a verified sidecar identity");
  }
  const manifestPath = path.join(stageRoot, "sidecar-manifest.json");
  if (sha256(manifestPath) !== expectedManifestSha256) {
    throw new Error("rotated standby manifest changed during candidate rotation");
  }
  const metadata = verifySidecarMetadata(stageRoot);
  if (metadata.files !== verifiedSidecar.files
      || metadata.total_size !== verifiedSidecar.total_size
      || metadata.content_set_sha256 !== verifiedSidecar.content_set_sha256) {
    throw new Error("rotated standby identity does not match the verified candidate");
  }
  const manifest = JSON.parse(io.readFileSync(manifestPath, "utf8"));
  const runtimeFiles = manifest.files
    .filter((file) => file.path.startsWith("runtime/"))
    .sort((left, right) => left.path.localeCompare(right.path));
  const applicationFiles = manifest.files
    .filter((file) => !file.path.startsWith("runtime/") && file.path !== "sidecar-profile.json")
    .sort((left, right) => left.path.localeCompare(right.path));
  const runtimeSnapshot = snapshotRuntimeSource(runtimeSourceRoot, io);
  const runtimeProof = writeStageReuseProof(runtimeSnapshot, runtimeFiles, {
    stageRoot,
    proofRoot: runtimeProofRoot,
    io,
  });
  const applicationSourceSnapshot = await snapshotApplicationSources(applicationInputs, {
    io,
    stageRoot,
  });
  if (!applicationContainsSourceSnapshot(applicationSourceSnapshot, applicationFiles)) {
    return {
      runtime: { hit: true, proofPath: runtimeProof.proofPath },
      application: { hit: false, reason: "application_source_changed" },
    };
  }
  const reusableCompiledModules = compiledModules ?? findApplicationCompiledModules(applicationFiles, {
    proofRoot: applicationProofRoot,
    io,
  });
  if (!Number.isSafeInteger(reusableCompiledModules) || reusableCompiledModules < 100) {
    return {
      runtime: { hit: true, proofPath: runtimeProof.proofPath },
      application: { hit: false, reason: "compiled_module_proof_missing" },
    };
  }
  const applicationProof = writeApplicationStageProof(
    applicationSourceSnapshot,
    applicationFiles,
    reusableCompiledModules,
    { stageRoot, proofRoot: applicationProofRoot, io },
  );
  return {
    runtime: { hit: true, proofPath: runtimeProof.proofPath },
    application: { hit: true, proofPath: applicationProof.proofPath },
  };
}

function stageSnapshotMatchesRuntime(stageSnapshot, runtimeFiles) {
  if (!stageSnapshot || !Array.isArray(stageSnapshot.files) || !Array.isArray(runtimeFiles)) return false;
  const expected = runtimeFiles
    .map((file) => ({ path: file.path.slice("runtime/".length), size: file.size }))
    .sort((left, right) => left.path.localeCompare(right.path));
  return expected.length === stageSnapshot.files.length
    && stageSnapshot.files.every((file, index) => file.path === expected[index].path && file.size === expected[index].size);
}

function readStageReuseProof(runtimeSnapshot, runtimeFiles, {
  stageRoot = STAGE_ROOT,
  proofRoot = STAGE_REUSE_PROOF_ROOT,
  io = fs,
} = {}) {
  const stageStat = io.lstatSync(stageRoot, { throwIfNoEntry: false });
  if (!stageStat) return { hit: false, reason: "stage_missing" };
  if (!stageStat.isDirectory() || stageStat.isSymbolicLink()) return { hit: false, reason: "stage_unsafe" };
  try {
    const stageSnapshot = snapshotRuntimeSource(path.join(stageRoot, "runtime"), io, { includeBytecode: true });
    if (!stageSnapshotMatchesRuntime(stageSnapshot, runtimeFiles)) {
      return { hit: false, reason: "stage_file_set_changed" };
    }
    const proofPath = path.join(proofRoot, `${stageSnapshot.fingerprint}.json`);
    const proofStat = io.lstatSync(proofPath, { throwIfNoEntry: false });
    if (!proofStat?.isFile() || proofStat.isSymbolicLink() || proofStat.size > 16 * 1024) {
      return { hit: false, reason: "stage_proof_missing_or_unsafe" };
    }
    const proof = JSON.parse(io.readFileSync(proofPath, "utf8"));
    if (proof?.schema_version !== STAGE_REUSE_SCHEMA
        || proof?.policy !== STAGE_REUSE_POLICY
        || proof?.source_fingerprint !== runtimeSnapshot.fingerprint
        || proof?.stage_fingerprint !== stageSnapshot.fingerprint
        || proof?.stage_files !== stageSnapshot.files.length
        || proof?.runtime_content_set_sha256 !== contentSetSha256(runtimeFiles)
        || typeof proof?.generated_at !== "string") {
      return { hit: false, reason: "stage_proof_mismatch" };
    }
    return { hit: true, reason: "stage_cache_hit", proofPath, stageSnapshot };
  } catch {
    return { hit: false, reason: "stage_proof_invalid" };
  }
}

function writeStageReuseProof(runtimeSnapshot, runtimeFiles, {
  stageRoot = STAGE_ROOT,
  proofRoot = STAGE_REUSE_PROOF_ROOT,
  io = fs,
  now = () => new Date(),
} = {}) {
  const stageSnapshot = snapshotRuntimeSource(path.join(stageRoot, "runtime"), io, { includeBytecode: true });
  if (!stageSnapshotMatchesRuntime(stageSnapshot, runtimeFiles)) {
    throw new Error("verified stage runtime does not match its cryptographic manifest");
  }
  const proofRootStat = io.lstatSync(proofRoot, { throwIfNoEntry: false });
  if (proofRootStat && (!proofRootStat.isDirectory() || proofRootStat.isSymbolicLink())) {
    throw new Error("verified stage proof root is unsafe");
  }
  io.mkdirSync(proofRoot, { recursive: true });
  const proofPath = path.join(proofRoot, `${stageSnapshot.fingerprint}.json`);
  const proof = {
    schema_version: STAGE_REUSE_SCHEMA,
    policy: STAGE_REUSE_POLICY,
    source_fingerprint: runtimeSnapshot.fingerprint,
    stage_fingerprint: stageSnapshot.fingerprint,
    stage_files: stageSnapshot.files.length,
    runtime_content_set_sha256: contentSetSha256(runtimeFiles),
    generated_at: now().toISOString(),
  };
  const temporary = `${proofPath}.tmp-${process.pid}-${crypto.randomBytes(8).toString("hex")}`;
  io.writeFileSync(temporary, `${JSON.stringify(proof)}\n`, { encoding: "utf8", flag: "wx", mode: 0o600 });
  try {
    io.rmSync(proofPath, { force: true });
    io.renameSync(temporary, proofPath);
  } finally {
    io.rmSync(temporary, { force: true });
  }
  const proofs = io.readdirSync(proofRoot, { withFileTypes: true })
    .map((entry) => {
      const absolute = path.join(proofRoot, entry.name);
      const stat = io.lstatSync(absolute);
      if (!entry.isFile() || stat.isSymbolicLink() || !/^[a-f0-9]{64}\.json$/.test(entry.name)) {
        throw new Error(`verified stage proof root contains an unsafe entry: ${absolute}`);
      }
      return { absolute, mtimeMs: stat.mtimeMs };
    })
    .sort((left, right) => right.mtimeMs - left.mtimeMs || left.absolute.localeCompare(right.absolute));
  for (const stale of proofs.slice(MAX_STAGE_REUSE_PROOFS)) io.rmSync(stale.absolute, { force: true });
  return { proofPath, stageSnapshot };
}

function sha256Async(file) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash("sha256");
    const stream = fs.createReadStream(file);
    stream.on("error", reject);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("end", () => resolve(hash.digest("hex")));
  });
}

async function hashFileEntries(entries, {
  cached = new Map(),
  concurrency = HASH_CONCURRENCY,
  hash = sha256Async,
} = {}) {
  if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 64) {
    throw new TypeError("sidecar_hash_concurrency_invalid");
  }
  const results = new Array(entries.length);
  let cursor = 0;
  let reused = 0;
  async function worker() {
    for (;;) {
      const index = cursor;
      cursor += 1;
      if (index >= entries.length) return;
      const entry = entries[index];
      const cachedEntry = cached.get(entry.path);
      if (cachedEntry && cachedEntry.size === entry.size && /^[a-f0-9]{64}$/.test(cachedEntry.sha256 || "")) {
        results[index] = { path: entry.path, size: entry.size, sha256: cachedEntry.sha256 };
        reused += 1;
      } else {
        results[index] = { path: entry.path, size: entry.size, sha256: await hash(entry.absolute) };
      }
    }
  }
  const workers = Math.min(concurrency, Math.max(1, entries.length));
  await Promise.all(Array.from({ length: workers }, () => worker()));
  return { files: results, reused, hashed: entries.length - reused };
}

function validManifestFile(file) {
  const relative = typeof file?.path === "string" ? file.path.slice("runtime/".length) : "";
  return file
    && typeof file.path === "string"
    && file.path.startsWith("runtime/")
    && !file.path.includes("\\")
    && relative
    && path.posix.normalize(relative) === relative
    && relative !== ".."
    && !relative.startsWith("../")
    && !path.win32.isAbsolute(relative)
    && !relative.includes(":")
    && Number.isSafeInteger(file.size)
    && file.size >= 0
    && /^[a-f0-9]{64}$/.test(file.sha256 || "");
}

function readRuntimeHashCache(snapshot, {
  cachePath = RUNTIME_HASH_CACHE_PATH,
  io = fs,
  refresh = false,
} = {}) {
  if (refresh) return { hit: false, reason: "refresh_requested", files: [] };
  const stat = io.lstatSync(cachePath, { throwIfNoEntry: false });
  if (!stat) return { hit: false, reason: "cache_missing", files: [] };
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > 16 * 1024 * 1024) {
    return { hit: false, reason: "cache_unsafe", files: [] };
  }
  try {
    const cache = JSON.parse(io.readFileSync(cachePath, "utf8"));
    if (cache.schema_version !== RUNTIME_HASH_CACHE_SCHEMA
        || cache.policy !== RUNTIME_HASH_POLICY
        || cache.source_fingerprint !== snapshot.fingerprint
        || !Array.isArray(cache.files)
        || cache.files.length === 0
        || cache.files.some((file) => !validManifestFile(file))
        || new Set(cache.files.map((file) => file.path)).size !== cache.files.length
        || cache.content_set_sha256 !== contentSetSha256(cache.files)) {
      return { hit: false, reason: "cache_mismatch", files: [] };
    }
    return { hit: true, reason: "cache_hit", files: cache.files };
  } catch {
    return { hit: false, reason: "cache_invalid", files: [] };
  }
}

function writeRuntimeHashCache(snapshot, files, {
  cachePath = RUNTIME_HASH_CACHE_PATH,
  io = fs,
  now = () => new Date(),
} = {}) {
  if (!Array.isArray(files) || files.length === 0 || files.some((file) => !validManifestFile(file))) {
    throw new Error("runtime hash cache received invalid files");
  }
  io.mkdirSync(path.dirname(cachePath), { recursive: true });
  const cache = {
    schema_version: RUNTIME_HASH_CACHE_SCHEMA,
    policy: RUNTIME_HASH_POLICY,
    source_fingerprint: snapshot.fingerprint,
    content_set_sha256: contentSetSha256(files),
    files,
    generated_at: now().toISOString(),
  };
  const temporary = `${cachePath}.tmp-${process.pid}-${crypto.randomBytes(8).toString("hex")}`;
  io.writeFileSync(temporary, `${JSON.stringify(cache)}\n`, { encoding: "utf8", flag: "wx", mode: 0o600 });
  try {
    io.rmSync(cachePath, { force: true });
    io.renameSync(temporary, cachePath);
  } finally {
    io.rmSync(temporary, { force: true });
  }
  return cache;
}

async function validateRuntimeBytecodeCache(runtimeFiles, {
  cacheRoot = RUNTIME_BYTECODE_CACHE_ROOT,
  io = fs,
} = {}) {
  const rootStat = io.lstatSync(cacheRoot, { throwIfNoEntry: false });
  if (!rootStat) return { hit: false, reason: "bytecode_cache_missing", files: 0 };
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) {
    return { hit: false, reason: "bytecode_cache_unsafe", files: 0 };
  }
  try {
    assertNoLinkedEntries(cacheRoot, io);
    const expected = runtimeFiles
      .filter((file) => file.path.endsWith(".pyc"))
      .sort((left, right) => left.path.localeCompare(right.path));
    const actual = walk(cacheRoot)
      .map((absolute) => ({
        absolute,
        path: `runtime/${path.relative(cacheRoot, absolute).replaceAll(path.sep, "/")}`,
        size: io.statSync(absolute).size,
      }))
      .sort((left, right) => left.path.localeCompare(right.path));
    if (actual.some((file) => !file.path.endsWith(".pyc"))
        || actual.length !== expected.length
        || actual.some((file, index) => file.path !== expected[index].path || file.size !== expected[index].size)) {
      return { hit: false, reason: "bytecode_cache_mismatch", files: 0 };
    }
    const observed = await hashFileEntries(actual);
    if (observed.files.some((file, index) => file.sha256 !== expected[index].sha256)) {
      return { hit: false, reason: "bytecode_cache_corrupt", files: 0 };
    }
    return { hit: true, reason: "bytecode_cache_hit", files: actual.length };
  } catch {
    return { hit: false, reason: "bytecode_cache_invalid", files: 0 };
  }
}

function refreshRuntimeBytecodeCache(stageRuntime = RUNTIME_TARGET, cacheRoot = RUNTIME_BYTECODE_CACHE_ROOT) {
  return copyRuntimeBytecodeWithRobocopy(stageRuntime, cacheRoot, true);
}

function aggregate(files, keyFor) {
  const groups = new Map();
  for (const file of files) {
    const key = keyFor(file);
    const current = groups.get(key) || { name: key, files: 0, bytes: 0 };
    current.files += 1;
    current.bytes += file.size;
    groups.set(key, current);
  }
  return [...groups.values()].sort((left, right) => right.bytes - left.bytes || left.name.localeCompare(right.name));
}

function pythonPackage(file) {
  const prefix = "runtime/Lib/site-packages/";
  if (!file.path.startsWith(prefix)) return null;
  const relative = file.path.slice(prefix.length);
  const first = relative.split("/", 1)[0];
  if (!first) return null;
  return first.replace(/\.(dist-info|data)$/, "");
}

function buildProfile(files) {
  const packageFiles = files.filter((file) => pythonPackage(file));
  const nativeExtensions = new Set([".dll", ".exe", ".pyd", ".so"]);
  return {
    schema_version: "1.0.0",
    files: files.length,
    total_size: files.reduce((total, file) => total + file.size, 0),
    top_level: aggregate(files, (file) => file.path.split("/", 1)[0]),
    extensions: aggregate(files, (file) => path.posix.extname(file.path).toLowerCase() || "[none]"),
    python_packages: aggregate(packageFiles, pythonPackage),
    native_binaries: aggregate(
      files.filter((file) => nativeExtensions.has(path.posix.extname(file.path).toLowerCase())),
      (file) => path.posix.extname(file.path).toLowerCase(),
    ),
    largest_files: [...files]
      .sort((left, right) => right.size - left.size || left.path.localeCompare(right.path))
      .slice(0, 50)
      .map(({ path: relativePath, size }) => ({ path: relativePath, size })),
  };
}

function contentSetSha256(files) {
  const canonical = files.map((file) => `${file.path}\0${file.size}\0${file.sha256}`).join("\n");
  return crypto.createHash("sha256").update(canonical, "utf8").digest("hex");
}

function parseArgs(argv) {
  const options = { clean: false, refreshRuntimeCache: false, useRuntimeCache: true, reuseVerifiedStage: false };
  for (const arg of argv) {
    if (arg === "--clean") options.clean = true;
    else if (arg === "--refresh-runtime-cache") options.refreshRuntimeCache = true;
    else if (arg === "--no-runtime-cache") options.useRuntimeCache = false;
    else if (arg === "--reuse-verified-stage") options.reuseVerifiedStage = true;
    else throw new Error(`unknown stage-sidecar argument: ${arg}`);
  }
  if (options.refreshRuntimeCache && !options.useRuntimeCache) {
    throw new Error("--refresh-runtime-cache cannot be combined with --no-runtime-cache");
  }
  if (options.clean && options.reuseVerifiedStage) {
    throw new Error("--clean cannot be combined with --reuse-verified-stage");
  }
  return options;
}

function elapsedMs(startedAt) {
  return Date.now() - startedAt;
}

async function stageSidecar(argv) {
  const options = parseArgs(argv);
  const totalStartedAt = Date.now();
  const snapshotStartedAt = Date.now();
  const runtimeSnapshot = snapshotRuntimeSource();
  const applicationSourceSnapshot = await snapshotApplicationSources();
  const trackedFiles = listTrackedApplicationFiles();
  const runtimeCache = options.useRuntimeCache
    ? readRuntimeHashCache(runtimeSnapshot, { refresh: options.refreshRuntimeCache })
    : { hit: false, reason: "cache_disabled", files: [] };
  console.log(
    `[stage-sidecar] runtime hash ${runtimeCache.hit ? "cache hit" : `cache miss (${runtimeCache.reason})`} `
      + `(${runtimeSnapshot.files.length} source files, snapshot ${elapsedMs(snapshotStartedAt)} ms)`,
  );
  const stageReuse = options.reuseVerifiedStage && runtimeCache.hit
    ? readStageReuseProof(runtimeSnapshot, runtimeCache.files)
    : { hit: false, reason: options.reuseVerifiedStage ? "runtime_cache_miss" : "stage_reuse_disabled" };
  if (options.reuseVerifiedStage) {
    console.log(`[stage-sidecar] verified standby stage ${stageReuse.hit ? "hit" : `miss (${stageReuse.reason})`}`);
  }
  const applicationReuse = stageReuse.hit
    ? readApplicationStageProof(applicationSourceSnapshot)
    : { hit: false, reason: "runtime_stage_miss", files: [] };
  if (options.reuseVerifiedStage) {
    console.log(`[stage-sidecar] verified application stage ${applicationReuse.hit ? "hit" : `miss (${applicationReuse.reason})`}`);
  }
  if (options.clean || (options.reuseVerifiedStage && !stageReuse.hit)) {
    fs.rmSync(STAGE_ROOT, { recursive: true, force: true });
  }
  fs.mkdirSync(STAGE_ROOT, { recursive: true });
  const copyStartedAt = Date.now();
  for (const [source, target, refresh] of INPUTS) {
    if (source === RUNTIME_SOURCE && target === RUNTIME_TARGET) {
      const copied = copyRuntimeWithRobocopy(source, target, refresh || !stageReuse.hit, { validateLinks: false });
      console.log(`[stage-sidecar] runtime staged via ${copied.method} (status ${copied.status})`);
    } else if (applicationReuse.hit) {
      continue;
    } else {
      copy(source, target, refresh, isApplicationSource(source) ? trackedFiles : null);
    }
  }
  const postCopySnapshot = snapshotRuntimeSource();
  if (postCopySnapshot.fingerprint !== runtimeSnapshot.fingerprint) {
    throw new Error("runtime input changed while sidecar staging was in progress");
  }
  if (!applicationReuse.hit) pruneUnstagedArtifacts(STAGE_ROOT, { preserveBytecode: true });
  assertPackageClean(STAGE_ROOT, { runtimeRoots: [RUNTIME_TARGET], cleanupStageRoot: STAGE_ROOT });
  console.log(`[stage-sidecar] canonical inputs copied in ${elapsedMs(copyStartedAt)} ms`);
  const bytecodeStartedAt = Date.now();
  const bytecodeCache = stageReuse.hit
    ? { hit: true, reason: "verified_stage", files: runtimeCache.files.filter((file) => file.path.endsWith(".pyc")).length }
    : runtimeCache.hit
    ? await validateRuntimeBytecodeCache(runtimeCache.files)
    : { hit: false, reason: "runtime_cache_miss", files: 0 };
  if (stageReuse.hit) {
    console.log(`[stage-sidecar] verified standby stage retained ${bytecodeCache.files} runtime bytecode files`);
  } else if (bytecodeCache.hit) {
    const copied = copyRuntimeBytecodeWithRobocopy(RUNTIME_BYTECODE_CACHE_ROOT, RUNTIME_TARGET, false);
    console.log(
      `[stage-sidecar] runtime bytecode cache hit (${bytecodeCache.files} files, ${copied.method} status ${copied.status}) `
        + `in ${elapsedMs(bytecodeStartedAt)} ms`,
    );
  } else {
    console.log(`[stage-sidecar] runtime bytecode cache miss (${bytecodeCache.reason})`);
  }
  const compileStartedAt = Date.now();
  const compiled = applicationReuse.hit ? applicationReuse.compiled : compileApplicationBytecode(STAGE_ROOT);
  assertPackageClean(STAGE_ROOT, { runtimeRoots: [RUNTIME_TARGET], cleanupStageRoot: STAGE_ROOT });
  console.log(
    `[stage-sidecar] application import and bytecode cache ${applicationReuse.hit ? "reused" : "completed"} `
      + `in ${elapsedMs(compileStartedAt)} ms`,
  );
  fs.rmSync(path.join(STAGE_ROOT, "sidecar-profile.json"), { force: true });
  const fullyReused = stageReuse.hit && applicationReuse.hit;
  const payloadEntries = (fullyReused
    ? [...runtimeCache.files, ...applicationReuse.files].map((file) => ({
      absolute: path.join(STAGE_ROOT, ...file.path.split("/")),
      path: file.path,
      size: file.size,
    }))
    : walk(STAGE_ROOT).map((file) => ({
      absolute: file,
      path: path.relative(STAGE_ROOT, file).replaceAll(path.sep, "/"),
      size: fs.statSync(file).size,
    })))
    .sort((left, right) => left.path.localeCompare(right.path));
  if (fullyReused) {
    console.log(`[stage-sidecar] verified inventories reused (${payloadEntries.length} payload files)`);
  }
  const profile = buildProfile(payloadEntries.map(({ path: relativePath, size }) => ({ path: relativePath, size })));
  const profilePath = path.join(STAGE_ROOT, "sidecar-profile.json");
  fs.writeFileSync(profilePath, JSON.stringify(profile, null, 2) + "\n", "utf8");
  const inventoryStartedAt = Date.now();
  const stagedFiles = [
    ...payloadEntries,
    { path: "sidecar-profile.json", size: fs.statSync(profilePath).size, absolute: profilePath },
  ]
    .sort((left, right) => left.path.localeCompare(right.path));
  const runtimeEntries = stagedFiles.filter((file) => file.path.startsWith("runtime/"));
  const applicationEntries = stagedFiles.filter((file) => !file.path.startsWith("runtime/"));
  const cachedRuntime = new Map(runtimeCache.files.map((file) => [file.path, file]));
  const cachedApplication = new Map(applicationReuse.files.map((file) => [file.path, file]));
  const [runtimeHashes, applicationHashes] = await Promise.all([
    hashFileEntries(runtimeEntries, { cached: cachedRuntime }),
    hashFileEntries(applicationEntries, { cached: cachedApplication }),
  ]);
  const files = [...runtimeHashes.files, ...applicationHashes.files]
    .sort((left, right) => left.path.localeCompare(right.path));
  console.log(
    `[stage-sidecar] inventory hashed in ${elapsedMs(inventoryStartedAt)} ms `
      + `(runtime reused ${runtimeHashes.reused}, hashed ${runtimeHashes.hashed}; `
      + `application reused ${applicationHashes.reused}, hashed ${applicationHashes.hashed})`,
  );
  const runtimeFiles = runtimeHashes.files.sort((left, right) => left.path.localeCompare(right.path));
  if (options.useRuntimeCache
      && (!runtimeCache.hit
        || runtimeHashes.hashed > 0
        || runtimeFiles.length !== runtimeCache.files.length
        || contentSetSha256(runtimeFiles) !== contentSetSha256(runtimeCache.files))) {
    writeRuntimeHashCache(postCopySnapshot, runtimeFiles);
    console.log(`[stage-sidecar] runtime hash cache updated (${runtimeFiles.length} files)`);
  }
  if (options.useRuntimeCache && !stageReuse.hit) {
    const reuseProof = writeStageReuseProof(postCopySnapshot, runtimeFiles);
    console.log(
      `[stage-sidecar] verified standby proof ${reuseProof.stageSnapshot.fingerprint} `
        + `(${reuseProof.stageSnapshot.files.length} runtime files)`,
    );
  } else if (stageReuse.hit) {
    console.log(
      `[stage-sidecar] verified standby proof retained ${stageReuse.stageSnapshot.fingerprint} `
        + `(${stageReuse.stageSnapshot.files.length} runtime files)`,
    );
  }
  const postApplicationSourceSnapshot = await snapshotApplicationSources();
  if (postApplicationSourceSnapshot.content_set_sha256 !== applicationSourceSnapshot.content_set_sha256) {
    throw new Error("application input changed while sidecar staging was in progress");
  }
  if (options.useRuntimeCache && !applicationReuse.hit) {
    const applicationFiles = applicationHashes.files
      .filter((file) => file.path !== "sidecar-profile.json")
      .sort((left, right) => left.path.localeCompare(right.path));
    const applicationProof = writeApplicationStageProof(postApplicationSourceSnapshot, applicationFiles, compiled);
    console.log(
      `[stage-sidecar] verified application proof ${applicationProof.stageSnapshot.fingerprint} `
        + `(${applicationProof.stageSnapshot.files.length} application files)`,
    );
  } else if (applicationReuse.hit) {
    console.log(
      `[stage-sidecar] verified application proof retained ${applicationReuse.stageSnapshot.fingerprint} `
        + `(${applicationReuse.stageSnapshot.files.length} application files)`,
    );
  }
  if (options.useRuntimeCache
      && (!bytecodeCache.hit
        || runtimeHashes.hashed > 0
        || runtimeFiles.filter((file) => file.path.endsWith(".pyc")).length !== bytecodeCache.files)) {
    const copied = refreshRuntimeBytecodeCache();
    console.log(`[stage-sidecar] runtime bytecode cache updated via ${copied.method} (status ${copied.status})`);
  }
  const manifest = {
    schema_version: "2.0.0",
    build_kind: "windows-cpu-sidecar",
    pack: BASE_PACK,
    generated_at: new Date().toISOString(),
    inputs: ["runtime", "src/backend", "src/core", "src/rebuild", "config/rebuild.toml.example", "config/settings.toml.example", "config/codex-hooks.toml", "requirements.txt", "verify-runtime-dependencies.py"],
    files,
    content_set_sha256: contentSetSha256(files),
    total_size: files.reduce((total, file) => total + file.size, 0),
  };
  fs.writeFileSync(path.join(STAGE_ROOT, "sidecar-manifest.json"), JSON.stringify(manifest, null, 2) + "\n", "utf8");
  console.log(
    `[stage-sidecar] staged ${files.length} files (${manifest.total_size} bytes, ${compiled} precompiled application modules) `
      + `in ${elapsedMs(totalStartedAt)} ms`,
  );
}

async function main(argv = process.argv.slice(2)) {
  try {
    assertPythonRuntime(RUNTIME_SOURCE);
    await stageSidecar(argv);
  } catch (error) {
    clearSidecarStage(STAGE_ROOT);
    throw error;
  }
}

if (require.main === module) {
  main().catch((error) => { console.error(`[stage-sidecar] ${error.message}`); process.exitCode = 1; });
}

module.exports = {
  APPLICATION_STAGE_POLICY,
  APPLICATION_STAGE_PROOF_ROOT,
  APPLICATION_STAGE_SCHEMA,
  BASE_PACK,
  DEVELOPMENT_ONLY_EXTENSIONS,
  EXCLUDED_DIRECTORY_NAMES,
  PRODUCTION_EXCLUDED_DIRECTORY_NAMES,
  PRODUCTION_EXCLUDED_DIRECTORY_PATTERNS,
  PRODUCTION_EXCLUDED_FILES,
  PRODUCTION_EXCLUDED_RUNTIME_PATHS,
  NATIVE_EXCLUDED_DIRECTORIES,
  INPUTS,
  NATIVE_EXCLUDED_FILES,
  ROBOCOPY_THREADS,
  HASH_CONCURRENCY,
  RUNTIME_BYTECODE_CACHE_ROOT,
  RUNTIME_HASH_CACHE_PATH,
  RUNTIME_HASH_CACHE_SCHEMA,
  RUNTIME_HASH_POLICY,
  STAGE_REUSE_POLICY,
  STAGE_REUSE_PROOF_ROOT,
  STAGE_REUSE_SCHEMA,
  RUNTIME_SOURCE,
  RUNTIME_TARGET,
  aggregate,
  assertNoLinkedEntries,
  buildProfile,
  compileApplicationBytecode,
  collectApplicationSourceEntries,
  copy,
  contentSetSha256,
  copyRuntimeBytecodeWithRobocopy,
  copyRuntimeWithRobocopy,
  hashFileEntries,
  listTrackedApplicationFiles,
  findApplicationCompiledModules,
  main,
  parseArgs,
  pruneUnstagedArtifacts,
  readRuntimeHashCache,
  readApplicationStageProof,
  readStageReuseProof,
  refreshRotatedStageProofs,
  refreshRuntimeBytecodeCache,
  sha256,
  sha256Async,
  shouldStage,
  shouldStageRuntimeBytecode,
  snapshotRuntimeSource,
  snapshotApplicationSources,
  snapshotApplicationStage,
  stageSnapshotMatchesRuntime,
  validateRuntimeBytecodeCache,
  walk,
  writeRuntimeHashCache,
  writeApplicationStageProof,
  writeStageReuseProof,
};
