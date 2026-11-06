import { useEffect, useRef, useState } from 'react';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { readStoredJson, writeStoredJson, removeStoredItem } from '../../shared/lib/browserStorage';
import { workbenchApi } from './workbenchApi';
import { acceptedIntervention, interventionTextLength, mergeInterventions } from './interventionState';

export function useInterventions({ projectId, turn, active }) {
  const key = userStorageKey(`steer:${projectId}:${turn.id}`);
  const initial = useRef(readStoredJson(key).value);
  const restored = initial.current;
  const [text, setText] = useState(typeof restored?.text === 'string' ? restored.text : '');
  const [pending, setPending] = useState(typeof restored?.pending?.key === 'string' && typeof restored.pending.text === 'string' ? restored.pending : null);
  const [data, setData] = useState(turn), [error, setError] = useState(''), [sending, setSending] = useState(false);
  const [missing, setMissing] = useState(false);
  const currentText = useRef(text), currentPending = useRef(pending), alive = useRef(true);
  currentText.current = text; currentPending.current = pending;
  const valid = () => alive.current && userStorageKey(`steer:${projectId}:${turn.id}`) === key;
  function save(nextText, nextPending) {
    if (!valid()) return false;
    if (!nextText && !nextPending) return removeStoredItem(key);
    return writeStoredJson(key, { text: nextText, pending: nextPending });
  }
  function draft(value) { setText(value); save(value, currentPending.current); }
  function settle(value, submitted) {
    if (!valid() || currentPending.current?.key !== submitted.key) return;
    const entry = acceptedIntervention(value, submitted);
    const next = currentText.current === submitted.text ? '' : currentText.current;
    save(next, null); currentPending.current = null; setPending(null); setText(next); setMissing(false); setError('');
    setData(old => ({ ...old, steers: mergeInterventions(old.steers, [entry]),
      steer_remaining: old.steers?.some(item => item.id === entry.id) ? old.steer_remaining
        : Number.isInteger(old.steer_remaining) ? Math.max(0, old.steer_remaining - 1) : old.steer_remaining }));
  }
  async function recover(signal) {
    const submitted = currentPending.current;
    if (!submitted || !valid()) return;
    try { settle(await workbenchApi.readSteer(projectId, turn.id, submitted.key, { signal }), submitted); }
    catch (failure) { if (valid() && currentPending.current?.key === submitted.key && !signal?.aborted) {
      setMissing(failure.status === 404 && failure.code === 'steer_not_found');
      setError(failure.status === 404 ? '尚未受理，可重试同一次插话' : '插话结果待核对');
    } }
  }
  useEffect(() => {
    if (!active) setData(turn);
    else if (turn.steers?.length) setData(old => ({ ...old, steers: mergeInterventions(old.steers, turn.steers) }));
  }, [turn, active]);
  useEffect(() => {
    alive.current = true;
    const controller = new AbortController(); let timer;
    async function read() {
      if (currentPending.current) await recover(controller.signal);
      if (!controller.signal.aborted && (active || currentPending.current)) {
        try {
          const state = await workbenchApi.steerState(projectId, turn.id, { signal: controller.signal });
          if (!Array.isArray(state.steers) || !Number.isInteger(state.steer_remaining)
            || state.steer_remaining < 0 || state.steer_remaining > 5
            || state.steer_target !== null && !['ask', 'do'].includes(state.steer_target?.intent)) throw new Error('插话状态未读取');
          if (valid()) { setData(old => ({ ...old, ...state })); setError(old => currentPending.current ? old : ''); }
        } catch { if (valid() && !controller.signal.aborted) setError('插话状态未读取'); }
      }
      if (!controller.signal.aborted && (active || currentPending.current)) timer = setTimeout(read, 1500);
    }
    read();
    return () => { alive.current = false; controller.abort(); clearTimeout(timer); };
  }, [key, active]);
  async function submit() {
    if (!valid() || sending) return;
    const submitted = currentPending.current || { key: `steer-${crypto.randomUUID()}`, text };
    if (currentPending.current && !missing || !currentPending.current && (!data.steer_target || data.steer_remaining === 0
      || !text.trim() || interventionTextLength(text) > 2000)) return;
    if (!save(text, submitted)) { setError('无法保存插话编号，请检查浏览器存储'); return; }
    currentPending.current = submitted; setPending(submitted); setSending(true); setMissing(false); setError('');
    try { settle(await workbenchApi.steer(projectId, turn.id, submitted.text, submitted.key), submitted); }
    catch (failure) { if (valid()) {
      if (failure.status >= 400 && failure.status < 500 && failure.code !== 'steer_submission_conflict') {
        currentPending.current = null; setPending(null); save(currentText.current, null);
        if (['turn_finished', 'steer_limit', 'steer_unsupported'].includes(failure.code)) setData(old => ({ ...old, steer_target: null }));
        setError({ turn_finished: '本轮已结束，草稿已保留', steer_limit: '本轮已用完五次插话',
          steer_unsupported: '当前执行者不支持插话，草稿已保留' }[failure.code] || '插话未受理');
      } else { setError('插话结果待核对'); await recover(); }
    } }
    finally { if (valid()) setSending(false); }
  }
  return { data, text, draft, pending, missing, sending, error, submit, recover };
}
