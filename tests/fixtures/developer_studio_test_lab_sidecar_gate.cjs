const fs = require("node:fs");
const path = require("node:path");

const [repoRoot, root, readyPath, commandPath] = process.argv.slice(2).map((value) => path.resolve(value));
const { SidecarSupervisor } = require(path.join(repoRoot, "apps", "desktop-electron", "src", "sidecar-supervisor.cjs"));

function writeJson(target, value) {
  const pending = `${target}.pending`;
  fs.writeFileSync(pending, JSON.stringify(value), "utf8");
  fs.renameSync(pending, target);
}

async function waitForCommand() {
  while (true) {
    if (fs.statSync(commandPath, { throwIfNoEntry: false })?.isFile()) {
      const command = JSON.parse(fs.readFileSync(commandPath, "utf8"));
      fs.unlinkSync(commandPath);
      return command;
    }
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
}

async function main() {
  fs.mkdirSync(root, { recursive: true });
  const supervisor = new SidecarSupervisor({
    rootDir: root,
    moduleRoot: path.join(repoRoot, "src"),
    workingDir: root,
    pythonPath: path.join(repoRoot, "runtime", "python.exe"),
    startupTimeoutMs: 15000,
  });
  let generation = 0;
  try {
    while (true) {
      const session = await supervisor.start();
      generation += 1;
      writeJson(readyPath, { generation, session });
      const command = await waitForCommand();
      if (command?.action === "restart") {
        await supervisor.stop();
        continue;
      }
      if (command?.action === "stop") return;
      throw new Error("fixture_command_invalid");
    }
  } finally {
    await supervisor.stop();
  }
}

main().catch((error) => {
  process.stderr.write(`${String(error?.stack || error)}\n${String(error?.diagnostic || "")}\n`);
  process.exitCode = 1;
});
