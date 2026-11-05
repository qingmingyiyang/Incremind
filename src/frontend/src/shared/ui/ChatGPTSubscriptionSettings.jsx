import { useCallback, useEffect, useRef, useState } from 'react';
import { Row } from './Row';
import { Switch } from './Switch';
import { subscriptionMessage, subscriptionRequest } from './subscriptionApi';
import { userStorageKey } from '../api/deviceTransport';
import { useRequestScope } from '../lib/useRequestScope';
import './subscription.css';

export function ChatGPTSubscriptionSettings({ showEgressSwitch = true, onChanged }) {
  const [space, setSpace] = useState(() => userStorageKey('chatgpt-subscription'));
  useEffect(() => {
    const changed = () => setSpace(userStorageKey('chatgpt-subscription'));
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, []);
  return <SubscriptionControls key={space} space={space} showEgressSwitch={showEgressSwitch} onChanged={onChanged}/>;
}

function SubscriptionControls({ space, showEgressSwitch, onChanged }) {
  const [status, setStatus] = useState(null), [models, setModels] = useState([]);
  const [attempt, setAttempt] = useState(null), [error, setError] = useState('');
  const [callbackUrl, setCallbackUrl] = useState('');
  const [busy, setBusy] = useState(false), [remoteWarning, setRemoteWarning] = useState(false);
  const popup = useRef(null), writer = useRef(null), writing = useRef(false), changed = useRef(onChanged);
  changed.current = onChanged;
  const gate = useRequestScope(space), scope = gate.scope;
  const reload = useCallback(async signal => {
    const token = gate.issue('load', scope);
    if (!token) return;
    const next = await subscriptionRequest('', undefined, 'GET', signal);
    if (!gate.isCurrent(token)) return;
    setStatus(next);
    changed.current?.();
    if (next.login) setAttempt(previous => previous?.attempt_id === next.login.attempt_id ? { ...previous, ...next.login } : next.login);
    if (next.sharing) {
      const catalog = await subscriptionRequest('/models', undefined, 'GET', signal);
      if (gate.isCurrent(token)) setModels(catalog.items);
    } else setModels([]);
  }, [gate, scope]);
  useEffect(() => {
    const controller = new AbortController();
    reload(controller.signal).catch(failure => { if (!controller.signal.aborted) setError(failure.message); });
    return () => { controller.abort(); writer.current?.abort(); popup.current?.close(); };
  }, [reload]);
  useEffect(() => {
    if (!attempt) return undefined;
    let stopped = false, timer;
    const controller = new AbortController();
    const token = gate.issue('poll', scope);
    const poll = async () => {
      try {
        const next = await subscriptionRequest(`/login/${attempt.attempt_id}`, undefined, 'GET', controller.signal);
        if (stopped || !gate.isCurrent(token)) return;
        if (next.state === 'pending') {
          if (next.callback_received && !attempt.callback_received) {
            setCallbackUrl(''); setAttempt(previous => previous && { ...previous, callback_received: true });
          } else timer = setTimeout(poll, 750);
        }
        else {
          setAttempt(null); setCallbackUrl(''); popup.current?.close();
          if (next.state === 'completed') await reload();
          else setError(subscriptionMessage(next.error));
        }
      } catch (failure) {
        if (!stopped && gate.isCurrent(token)) { setAttempt(null); setCallbackUrl(''); setError(failure.message); }
      }
    };
    timer = setTimeout(poll, 750);
    return () => { stopped = true; clearTimeout(timer); controller.abort(); };
  }, [attempt, reload, gate, scope]);
  async function perform(action) {
    if (writing.current) return;
    const token = gate.issue('write', scope);
    if (!token) return;
    const controller = new AbortController(); writer.current = controller; writing.current = true;
    setBusy(true); setError(''); setRemoteWarning(false);
    try { await action(controller.signal, token); }
    catch (failure) { if (gate.isCurrent(token)) setError(failure.message); }
    finally { if (gate.isCurrent(token)) { setBusy(false); writing.current = false; } }
  }
  function login() {
    if (writing.current) return;
    popup.current = window.open('about:blank', '_blank');
    if (popup.current) popup.current.opener = null;
    void perform(async (signal, token) => {
      try {
        const next = await subscriptionRequest('/login', { expected_revision: status.revision }, 'POST', signal);
        if (!gate.isCurrent(token)) return;
        setCallbackUrl('');
        setAttempt(next);
        if (popup.current) popup.current.location.href = next.authorization_url;
      } catch (failure) { popup.current?.close(); throw failure; }
    });
  }
  const select = (model, consent) => perform(async signal => {
    await subscriptionRequest('/selection', { model: model || null, expected_revision: status.selection.revision,
      ...(consent === undefined ? {} : { allow_remote: consent, expected_generation_revision: status.generation_revision }) }, 'PATCH', signal);
    await reload(signal);
  });
  return <section className="ui-subscription" aria-label="ChatGPT 订阅模型">
    <Row readOnly dot={attempt || busy ? 'processing' : error ? 'failed' : status?.sharing ? 'done' : 'unverified'}
      title="ChatGPT" sub={status?.identity?.email || (status?.connected ? '订阅未授权' : undefined)}
      trailing={<div className="ui-subscription-actions">
        {attempt ? <button disabled={busy} onClick={() => { setCallbackUrl(''); perform(async (signal, token) => {
          await subscriptionRequest(`/login/${attempt.attempt_id}`, undefined, 'DELETE', signal);
          if (gate.isCurrent(token)) { setAttempt(null); popup.current?.close(); }
        }); }}>取消</button> : status && !status.sharing ? <button disabled={busy} onClick={login}>登录</button> : null}
        {status?.connected && !attempt ? <button disabled={busy} onClick={() => perform(async (signal, token) => {
          const result = await subscriptionRequest('/logout', { expected_revision: status.revision }, 'POST', signal);
          await reload(signal); if (gate.isCurrent(token)) setRemoteWarning(result.remote_revoked === false);
        })}>退出</button> : null}
      </div>} />
    {attempt?.authorization_url && !attempt.callback_received && (!popup.current || popup.current.closed) && <a className="ui-subscription-login" href={attempt.authorization_url} target="_blank" rel="noreferrer">打开登录 ↗</a>}
    {attempt && <form className="ui-subscription-callback" onSubmit={event => {
      event.preventDefault(); if (busy || !callbackUrl.trim() || attempt.callback_received) return;
      const input = callbackUrl; setCallbackUrl('');
      perform(async (signal, token) => {
        const result = await subscriptionRequest(`/login/${attempt.attempt_id}/complete`, { callback_url: input }, 'POST', signal);
        if (!gate.isCurrent(token)) return;
        if (result.state === 'pending') {
          setAttempt(previous => previous && { ...previous, callback_received: true });
          return;
        }
        gate.invalidate('poll'); setAttempt(null); popup.current?.close();
        if (result.state === 'completed') await reload(signal);
        else setError(subscriptionMessage(result.error));
      });
    }}><p>{attempt.callback_received ? '授权已提交 · 正在验证' : '登录后若本机地址无法打开，复制地址栏完整地址，在此完成登录。'}</p>
      <label>完整回调地址<input type="password" inputMode="url" aria-label="完整回调地址" autoComplete="off" spellCheck="false" maxLength={16000} value={callbackUrl} disabled={busy || attempt.callback_received === true} onChange={event => setCallbackUrl(event.target.value)}/></label>
      <button type="submit" disabled={busy || attempt.callback_received === true || !callbackUrl.trim()}>完成登录</button>
    </form>}
    {status?.sharing && <Row readOnly title="生成模型" sub={status.selection.model && status.selection_ready === false ? '账号已变化 · 重选模型' : undefined} trailing={<select aria-label="ChatGPT 生成模型" value={status.selection_ready === false ? '' : status.selection.model || ''} disabled={busy}
      onChange={(event) => void select(event.target.value)}>
      <option value="">API / 本机</option>
      {status.selection.model && !models.some((model) => model.id === status.selection.model) && <option value={status.selection.model}>{status.selection.model}</option>}
      {models.map((model) => <option key={model.id} value={model.id}>{model.id}</option>)}
    </select>} />}
    {showEgressSwitch && (status?.sharing || status?.selection.model) && <Row readOnly title="允许发送到你的模型" trailing={<Switch label="允许发送到你的模型" checked={status.allow_remote}
      disabled={busy} onChange={(enabled) => void select(status.selection.model, enabled)} />} />}
    {remoteWarning && <div role="alert">远端退出未确认 · <a href="https://chatgpt.com/" target="_blank" rel="noreferrer">查看账号</a></div>}
    {error && <div role="alert">{error} <button disabled={busy} onClick={() => perform(signal => reload(signal))}>刷新状态</button></div>}
  </section>;
}
