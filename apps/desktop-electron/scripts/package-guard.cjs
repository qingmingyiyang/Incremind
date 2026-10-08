const fs = require("node:fs");
const path = require("node:path");

// 这里只能添加已核验的、精确的包内相对文件路径，原因记入计划执行记录。
// 当前没有获准的放行项。
const PACKAGE_ALLOWLIST = Object.freeze([]);
const RUNTIME_DATA_DIRECTORIES = Object.freeze([".rebuild-data", "workspace", "backups", "logs", "data"]);

function clearSidecarStage(stageRoot) {
  const resolved = path.resolve(stageRoot);
  if (path.basename(resolved) !== ".sidecar-stage") throw new Error("package_stage_root_invalid");
  // rm 仅删除链接条目本身，不遍历链接指向的暂存目录。
  fs.rmSync(resolved, { recursive: true, force: true });
}

function assertPackageClean(root, {
  runtimeRoots = [],
  allowlist = PACKAGE_ALLOWLIST,
  cleanupStageRoot = null,
} = {}) {
  const resolved = path.resolve(root);
  if (cleanupStageRoot !== null && path.basename(path.resolve(cleanupStageRoot)) !== ".sidecar-stage") {
    throw new Error("package_stage_root_invalid");
  }
  try {
    if (!Array.isArray(allowlist) || allowlist.some((relative) => typeof relative !== "string"
        || !relative || relative.includes("\\") || /[*?\[\]:]/.test(relative)
        || path.posix.normalize(relative) !== relative || relative === ".." || relative.startsWith("../")
        || path.posix.isAbsolute(relative) || path.win32.isAbsolute(relative))) {
      throw new Error("package_allowlist_invalid");
    }
    const allowed = new Set(allowlist);
    if (!Array.isArray(runtimeRoots)) throw new Error("package_runtime_root_invalid");
    const runtimes = runtimeRoots.map((runtime) => {
      const absolute = path.resolve(runtime);
      const relative = path.relative(resolved, absolute);
      if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
        throw new Error("package_runtime_root_invalid");
      }
      return absolute;
    });
    const rootStat = fs.lstatSync(resolved, { throwIfNoEntry: false });
    if (!rootStat?.isDirectory() || rootStat.isSymbolicLink()) throw new Error("package_root_invalid");
    const forbidden = [];
    function visit(directory) {
      for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
        const absolute = path.join(directory, entry.name);
        const relative = path.relative(resolved, absolute).replaceAll(path.sep, "/");
        const name = entry.name.toLowerCase();
        if (entry.isSymbolicLink() || (!entry.isFile() && !entry.isDirectory())) {
          forbidden.push(relative);
          continue;
        }
        if (entry.isDirectory()) {
          if (runtimes.some((runtime) => runtime.toLowerCase() === directory.toLowerCase())
              && RUNTIME_DATA_DIRECTORIES.includes(name)) forbidden.push(relative);
          else visit(absolute);
        } else {
          const runtimeSettings = runtimes.some((runtime) =>
            path.relative(runtime, absolute).replaceAll(path.sep, "/").toLowerCase() === "config/settings.toml");
          const forbiddenFile = name === "secrets.json" || name === ".env" || name.startsWith(".env.")
            || name === "data-root.json" || [".sqlite", ".sqlite3", ".db"].includes(path.extname(name))
            || name.endsWith("-wal") || name.endsWith("-shm") || runtimeSettings;
          if (forbiddenFile && !allowed.has(relative)) forbidden.push(relative);
        }
      }
    }
    visit(resolved);
    if (forbidden.length) {
      const paths = forbidden.sort();
      const error = new Error(`package_data_forbidden: ${paths.join(", ")}`);
      error.code = "package_data_forbidden";
      error.paths = paths;
      throw error;
    }
  } catch (error) {
    if (cleanupStageRoot !== null) clearSidecarStage(cleanupStageRoot);
    throw error;
  }
}

function assertPythonRuntime(root, options = {}) {
  const resolved = path.resolve(root);
  const directory = fs.lstatSync(resolved, { throwIfNoEntry: false });
  const executable = directory?.isDirectory() && !directory.isSymbolicLink()
    ? fs.lstatSync(path.join(resolved, "python.exe"), { throwIfNoEntry: false }) : null;
  if (!executable?.isFile() || executable.isSymbolicLink()) {
    throw new Error("Place a portable Python runtime in python-runtime/ containing python.exe or set CHRIPTMAS_SIDECAR_PYTHON_RUNTIME");
  }
  assertPackageClean(resolved, { ...options, runtimeRoots: [resolved] });
}

module.exports = { PACKAGE_ALLOWLIST, RUNTIME_DATA_DIRECTORIES, assertPackageClean, assertPythonRuntime, clearSidecarStage };
