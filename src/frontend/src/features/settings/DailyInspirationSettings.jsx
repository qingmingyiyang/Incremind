import { useEffect, useRef, useState } from 'react';
import { Icon, Row, Switch } from '../../shared/ui';
import { deviceIdentity, isDeviceIdentity } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { settingsApi } from './settingsApi';
import './SettingsSupport.css';

const validTime = value => typeof value === 'string' && /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value);
const validSettings = value => value && typeof value.enabled === 'boolean' && validTime(value.time)
  && value.timezone === 'Asia/Shanghai' && Number.isSafeInteger(value.revision) && value.revision >= 0;

export function DailyInspirationSettings({ projectId, api = settingsApi }) {
  const [epoch, setEpoch] = useState(0), [open, setOpen] = useState(false);
  const [value, setValue] = useState(null), [draft, setDraft] = useState(null);
  const [error, setError] = useState(''), [conflict, setConflict] = useState(false), [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const gate = useRequestScope(`${projectId}\0${epoch}`), scope = gate.scope;
  const pending = useRef(null), savedTimer = useRef(null);

  useEffect(() => {
    const changed = () => {
      pending.current?.controller.abort(); pending.current = null;
      gate.invalidate('read'); gate.invalidate('write'); clearTimeout(savedTimer.current);
      setValue(null); setDraft(null); setError(''); setConflict(false); setBusy(false); setSaved(false); setOpen(false);
      setEpoch(current => current + 1);
    };
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
      pending.current?.controller.abort(); clearTimeout(savedTimer.current);
    };
  }, [gate]);

  async function load(preserveDraft = false) {
    if (pending.current) return;
    const token = gate.issue('read', scope);
    if (!token) return;
    const identity = deviceIdentity(), controller = new AbortController(), operation = { controller };
    pending.current = operation; setBusy(true); setError('');
    const current = () => pending.current === operation && !controller.signal.aborted
      && gate.isCurrent(token) && isDeviceIdentity(identity);
    try {
      const result = await api.inspirations(controller.signal);
      if (!current()) return;
      if (!validSettings(result)) throw new Error('invalid_inspiration_settings');
      setValue({ scope, ...result });
      if (!preserveDraft) setDraft({ enabled: result.enabled, time: result.time });
      setConflict(false);
    } catch (failure) {
      if (current() && failure.name !== 'AbortError') setError('读取未完成 · 重试');
    } finally {
      if (pending.current === operation) pending.current = null;
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) setBusy(false);
    }
  }

  useEffect(() => {
    setValue(null); setDraft(null); setConflict(false); setError(''); setBusy(false); setSaved(false);
    load();
    return () => { pending.current?.controller.abort(); pending.current = null; };
  }, [scope, api]);

  async function save(submitted, preserveTime = false) {
    if (pending.current || conflict || !value || value.scope !== scope
      || typeof submitted.enabled !== 'boolean' || !validTime(submitted.time)) return;
    const token = gate.issue('write', scope);
    if (!token) return;
    const identity = deviceIdentity(), controller = new AbortController(), operation = { controller };
    pending.current = operation; clearTimeout(savedTimer.current); setBusy(true); setError(''); setSaved(false);
    const current = () => pending.current === operation && !controller.signal.aborted
      && gate.isCurrent(token) && isDeviceIdentity(identity);
    try {
      const result = await api.saveInspirations({ ...submitted, expectedRevision: value.revision }, controller.signal);
      if (!current()) return;
      if (!validSettings(result) || result.enabled !== submitted.enabled || result.time !== submitted.time)
        throw new Error('invalid_inspiration_settings');
      setValue({ scope, ...result }); setConflict(false);
      setDraft(previous => ({ enabled: result.enabled, time: preserveTime ? previous.time : result.time }));
      setSaved(true); savedTimer.current = setTimeout(() => setSaved(false), 1400);
    } catch (failure) {
      if (current() && failure.name !== 'AbortError') {
        setConflict(failure.status === 409);
        setError(failure.status === 409 ? '设置已变化 · 草稿保留' : '保存未完成 · 重试');
      }
    } finally {
      if (pending.current === operation) pending.current = null;
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) setBusy(false);
    }
  }

  const actual = value?.scope === scope ? value : null;
  const dirty = actual && draft && (draft.enabled !== actual.enabled || draft.time !== actual.time);
  return <section className="settings-item settings-daily-inspiration">
    <Row dot={<span className="settings-glyph"><Icon name="pattern" size={18}/></span>} title="每日灵感"
      sub={actual ? actual.enabled ? '每天 · 北京时间' : '已关闭' : '读取中'} expanded={open}
      onOpen={() => setOpen(current => !current)} trailing={<div className="settings-support-inline"><button type="button" className="settings-time-pill" aria-label="修改每日灵感时间" aria-expanded={open} disabled={busy || !actual} onClick={() => setOpen(current => !current)}>{actual?.time || '—'}</button><Switch label="每日灵感" checked={draft?.enabled ?? false}
        disabled={busy || !actual || conflict} onChange={enabled => {
          setDraft(current => ({ ...current, enabled })); setOpen(true);
          save({ enabled, time: actual.time }, true);
        }}/></div>}/>
    {open && <form className="settings-expand" onSubmit={event => { event.preventDefault(); if (draft && dirty) save(draft); }}>
      <div className="settings-fields"><label>每日时间<input type="time" aria-label="每日灵感时间" step="60"
        required disabled={busy || !actual} value={draft?.time || ''} onChange={event => {
          setDraft(current => ({ ...current, time: event.target.value })); setSaved(false);
        }}/></label><label>时区<input value="北京时间" readOnly/></label></div>
      {error && <p role="alert">{error} <button type="button" disabled={busy} onClick={() => load(Boolean(draft))}>{conflict ? '重新读取' : '重试读取'}</button></p>}
      <div className="settings-save"><button type="submit" disabled={busy || !actual || !draft || !validTime(draft.time) || conflict || !dirty}
        title={actual ? `r${actual.revision}` : undefined}>{saved ? '✓ 已保存' : '保存'}</button></div>
    </form>}
    {!open && error && <p role="alert">{error} <button type="button" disabled={busy} onClick={() => load(Boolean(draft))}>重试读取</button></p>}
  </section>;
}
