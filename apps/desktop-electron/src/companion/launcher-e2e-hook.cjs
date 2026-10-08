"use strict";

const fs = require("node:fs");
const path = require("node:path");

const SWITCH = "--chriptmas-e2e-companion-launcher";
const MODE_KEY = "CHRIPTMAS_E2E_COMPANION_LAUNCHER_MODE";
const MODE = "candidate-self";
const RECEIPT_NAME = "companion-e2e-launcher-receipt.json";
const ENTRY_NAME = "Companion E2E launcher target";

function resolveCompanionLauncherE2E({ argv = process.argv, env = process.env, executablePath, userDataRoot } = {}) {
  if (!Array.isArray(argv) || !argv.includes(SWITCH) || !env || typeof env !== "object") return null;
  const ownedKeys = Object.keys(env).filter((key) => key.startsWith("CHRIPTMAS_E2E_COMPANION_LAUNCHER_")).sort();
  if (ownedKeys.join("|") !== MODE_KEY || env[MODE_KEY] !== MODE) return null;
  if (typeof executablePath !== "string" || !path.isAbsolute(executablePath) || path.extname(executablePath).toLowerCase() !== ".exe") return null;
  if (typeof userDataRoot !== "string" || !path.isAbsolute(userDataRoot)) return null;
  const root = path.resolve(userDataRoot);
  return Object.freeze({
    entryName: ENTRY_NAME,
    targetPath: path.resolve(executablePath),
    userDataRoot: root,
    receiptPath: path.join(root, RECEIPT_NAME),
  });
}

function seedCompanionLauncherE2ETarget(controller, target) {
  if (!controller || typeof controller.ensureProgram !== "function" || !target) return null;
  return controller.ensureProgram({ name: target.entryName, selectedPath: target.targetPath });
}

function launcherE2ELaunchArgs(entry, target) {
  // This is intentionally the sole exceptional launch argument. The caller
  // cannot supply it: it is derived from the dual-gated target and is only
  // returned for that exact self executable.
  if (!target || !entry || entry.kind !== "program" || typeof entry.path !== "string") return [];
  if (entry.path !== target.targetPath || !isOwnedAbsoluteDirectory(target.userDataRoot)) return [];
  return Object.freeze([`--user-data-dir=${target.userDataRoot}`]);
}

function isOwnedAbsoluteDirectory(value) {
  return typeof value === "string" && path.isAbsolute(value) && path.resolve(value) === value;
}

function recordCompanionLauncherE2EReceipt(target, { existsSync = fs.existsSync, lstatSync = fs.lstatSync, writeFileSync = fs.writeFileSync, mkdirSync = fs.mkdirSync } = {}) {
  if (!target || typeof target.userDataRoot !== "string" || typeof target.receiptPath !== "string") return false;
  const directory = path.resolve(target.userDataRoot);
  if (target.receiptPath !== path.join(directory, RECEIPT_NAME)) return false;
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  const rootStat = lstatSync(directory);
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink()) return false;
  if (existsSync(target.receiptPath)) {
    const receiptStat = lstatSync(target.receiptPath);
    if (!receiptStat.isFile() || receiptStat.isSymbolicLink()) return false;
  }
  const payload = `${JSON.stringify({ marker: "candidate-self", sequence: 1 })}\n`;
  writeFileSync(target.receiptPath, payload, { encoding: "utf8", mode: 0o600, flag: "w" });
  return true;
}

module.exports = {
  ENTRY_NAME,
  MODE,
  MODE_KEY,
  RECEIPT_NAME,
  SWITCH,
  launcherE2ELaunchArgs,
  recordCompanionLauncherE2EReceipt,
  resolveCompanionLauncherE2E,
  seedCompanionLauncherE2ETarget,
};
