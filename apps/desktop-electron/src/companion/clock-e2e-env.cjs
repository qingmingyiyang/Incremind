"use strict";

const SWITCH = "--chriptmas-e2e-companion-clock";
const MODE_KEY = "CHRIPTMAS_COMPANION_E2E_CLOCK_MODE";
const UTC_KEY = "CHRIPTMAS_COMPANION_E2E_CLOCK_UTC";
const PREFIX = "CHRIPTMAS_COMPANION_E2E_CLOCK_";
const MODE = "packaged-fixed";
const UTC = /^(?:202[0-9]|20[3-9]\d|2100)-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\dZ$/;

function resolveCompanionE2EClockEnv({ argv = process.argv, env = process.env } = {}) {
  if (!Array.isArray(argv) || !env || typeof env !== "object" || !argv.includes(SWITCH)) return Object.freeze({});
  const keys = Object.keys(env).filter((key) => key.startsWith(PREFIX)).sort();
  if (keys.join("|") !== `${MODE_KEY}|${UTC_KEY}` || env[MODE_KEY] !== MODE || typeof env[UTC_KEY] !== "string" || !UTC.test(env[UTC_KEY])) {
    return Object.freeze({});
  }
  return Object.freeze({ [MODE_KEY]: MODE, [UTC_KEY]: env[UTC_KEY] });
}

function resolveCompanionE2EClockNow({ argv = process.argv, env = process.env } = {}) {
  const configured = resolveCompanionE2EClockEnv({ argv, env });
  const raw = configured[UTC_KEY];
  if (typeof raw !== "string") return null;
  const fixed = new Date(raw);
  // The regexp deliberately keeps the IPC/environment contract compact. Do a
  // round trip here as well so impossible calendar values cannot normalize.
  if (Number.isNaN(fixed.getTime()) || fixed.toISOString().replace(".000Z", "Z") !== raw) return null;
  const epochMs = fixed.getTime();
  return () => new Date(epochMs);
}

function stripCompanionE2EClockEnv(env) {
  const result = { ...env };
  for (const key of Object.keys(result)) if (key.startsWith(PREFIX)) delete result[key];
  return result;
}

module.exports = { MODE, MODE_KEY, PREFIX, SWITCH, UTC, UTC_KEY, resolveCompanionE2EClockEnv, resolveCompanionE2EClockNow, stripCompanionE2EClockEnv };
