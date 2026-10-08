const ROUTINE_KEYS = new Set(["enabled", "sleep_start", "wake_time"]);
const TIME = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
const WAKE_DURATION_MS = 30 * 60 * 1000;

function minuteOfDay(value) {
  const [hours, minutes] = value.split(":").map(Number);
  return hours * 60 + minutes;
}

function localDay(date) {
  const year = String(date.getFullYear()).padStart(4, "0");
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function sanitiseRoutineSettings(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  if (Object.keys(value).some((key) => !ROUTINE_KEYS.has(key)) || Object.keys(value).length !== 3) return null;
  if (typeof value.enabled !== "boolean" || typeof value.sleep_start !== "string" || typeof value.wake_time !== "string") return null;
  if (!TIME.test(value.sleep_start) || !TIME.test(value.wake_time) || value.sleep_start === value.wake_time) return null;
  return Object.freeze({ enabled: value.enabled, sleep_start: value.sleep_start, wake_time: value.wake_time });
}

function isRoutineSleep(date, settings) {
  const safe = sanitiseRoutineSettings(settings);
  if (!(date instanceof Date) || Number.isNaN(date.getTime()) || !safe?.enabled) return false;
  const current = date.getHours() * 60 + date.getMinutes();
  const start = minuteOfDay(safe.sleep_start);
  const end = minuteOfDay(safe.wake_time);
  return start < end ? current >= start && current < end : current >= start || current < end;
}

class CompanionRoutineController {
  constructor({ publish, clear, onMorning = async () => {}, now = () => new Date(), monotonicNow = () => performance.now() }) {
    this.publish = publish;
    this.clear = clear;
    this.onMorning = onMorning;
    this.now = now;
    this.monotonicNow = monotonicNow;
    this.settings = Object.freeze({ enabled: true, sleep_start: "23:00", wake_time: "07:00" });
    this.settingsRevision = 0;
    this.sleeping = null;
    this.manualWakeUntil = null;
    this.lastMonotonic = null;
    this.morningAttemptedDay = null;
  }

  applySnapshot(snapshot) {
    const settings = sanitiseRoutineSettings(snapshot?.settings);
    if (!settings || !Number.isSafeInteger(snapshot?.revision) || snapshot.revision < 0) return false;
    if (snapshot.revision < this.settingsRevision) return false;
    if (snapshot.revision === this.settingsRevision && (
      settings.enabled !== this.settings.enabled
      || settings.sleep_start !== this.settings.sleep_start
      || settings.wake_time !== this.settings.wake_time
    )) return false;
    this.settings = settings;
    this.settingsRevision = snapshot.revision;
    this.evaluate();
    return true;
  }

  evaluate() {
    const wall = this.now();
    const monotonic = this.monotonicNow();
    if (!(wall instanceof Date) || Number.isNaN(wall.getTime()) || !Number.isFinite(monotonic) || monotonic < 0) return this.status();
    if (this.lastMonotonic !== null && monotonic < this.lastMonotonic) this.manualWakeUntil = null;
    this.lastMonotonic = monotonic;
    if (this.manualWakeUntil !== null && monotonic >= this.manualWakeUntil) this.manualWakeUntil = null;
    const naturalSleep = isRoutineSleep(wall, this.settings);
    const sleeping = naturalSleep && this.manualWakeUntil === null;
    if (sleeping !== this.sleeping) {
      this.sleeping = sleeping;
      if (sleeping) this.publish({ state: "sleeping", mood: "idle", animation_key: "sleep" });
      else this.clear();
    }
    const wakeMinute = minuteOfDay(this.settings.wake_time);
    const currentMinute = wall.getHours() * 60 + wall.getMinutes();
    const sinceWake = (currentMinute - wakeMinute + 1440) % 1440;
    const day = localDay(wall);
    if (this.settings.enabled && !naturalSleep && sinceWake < 300 && this.morningAttemptedDay !== day) {
      this.morningAttemptedDay = day;
      void Promise.resolve(this.onMorning(day)).catch(() => {});
    }
    return this.status();
  }

  wakeForThirtyMinutes() {
    this.evaluate();
    if (!isRoutineSleep(this.now(), this.settings)) return Object.freeze({ ...this.status(), status: "already_awake" });
    const monotonic = this.monotonicNow();
    if (!Number.isFinite(monotonic) || monotonic < 0) return Object.freeze({ ...this.status(), status: "unavailable" });
    this.lastMonotonic = monotonic;
    this.manualWakeUntil = monotonic + WAKE_DURATION_MS;
    this.evaluate();
    return Object.freeze({ ...this.status(), status: "awakened" });
  }

  status() {
    const monotonic = this.monotonicNow();
    const remaining = this.manualWakeUntil === null || !Number.isFinite(monotonic)
      ? 0 : Math.max(0, Math.ceil((this.manualWakeUntil - monotonic) / 1000));
    return Object.freeze({
      enabled: this.settings.enabled,
      sleep_start: this.settings.sleep_start,
      wake_time: this.settings.wake_time,
      settings_revision: this.settingsRevision,
      sleeping: this.sleeping === true,
      manual_wake_remaining_seconds: remaining,
    });
  }
}

module.exports = { CompanionRoutineController, WAKE_DURATION_MS, isRoutineSleep, sanitiseRoutineSettings };
