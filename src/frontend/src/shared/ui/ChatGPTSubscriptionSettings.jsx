import { useCallback, useEffect, useRef, useState } from 'react';
import { Row } from './Row';
import { Switch } from './Switch';
import { subscriptionRequest } from './subscriptionApi';
import './subscription.css';

export function ChatGPTSubscriptionSettings({ showEgressSwitch = true, onChanged }) {
  const [status, setStatus] = useState(null), [models, setModels] = useState([]);
  const [attempt, setAttempt] = useState(null), [error, setError] = useState('');
  const [busy, setBusy] = useState(false), [remoteWarning, setRemoteWarning] = useState(false);
  const popup = useRef(null), mounted = useRef(false);
  const reload = useCallback(async () => {
    const next = await subscriptionRequest();
    if (!mounted.current) return;
    setStatus(next);
    onChanged?.();
    if (next.login) setAttempt((previous) => previous || next.login);
    if (next.sharing) {
      const catalog = await subscriptionRequest('/models');
      if (mounted.current) setModels(catalog.items);
    } else setModels([]);
  }, []);
  useEffect(() => {
    mounted.current = true;
    reload().catch((failure) => { if (mounted.current) setError(failure.message); });
    return () => { mounted.current = false; };
  }, [reload]);
  useEffect(() => {
    if (!attempt) return undefined;
    let stopped = false, timer;
    const controller = new AbortController();
    const poll = async () => {
      try {
        const next = await subscriptionRequest(`/login/${attempt.attempt_id}`, undefined, 'GET', controller.signal);
        if (stopped) return;
        if (next.state === 'pending') timer = setTimeout(poll, 750);
        else {
          setAttempt(null); popup.current?.close();
          if (next.state === 'completed') await reload();
          else setError('登录未完成 · 重试');
        }
      } catch (failure) {
        if (!stopped) { setAttempt(null); setError(failure.message); }
      }
    };
    timer = setTimeout(poll, 750);
    return () => { stopped = true; clearTimeout(timer); controller.abort(); };
  }, [attempt, reload]);
  async function perform(action) {
    setBusy(true); setError(''); setRemoteWarning(false);
    try { await action(); }
    catch (failure) { if (mounted.current) setError(failure.message); }
    finally { if (mounted.current) setBusy(false); }
  }
  function login() {
    popup.current = window.open('about:blank', '_blank');
    if (popup.current) popup.current.opener = null;
    void perform(async () => {
      try {
        const next = await subscriptionRequest('/login', { expected_revision: status.revision }, 'POST');
        if (!mounted.current) return;
        setAttempt(next);
        if (popup.current) popup.current.location.href = next.authorization_url;
      } catch (failure) { popup.current?.close(); throw failure; }
    });
  }
  const select = (model, consent) => perform(async () => {
    await subscriptionRequest('/selection', { model: model || null, expected_revision: status.selection.revision,
      ...(consent === undefined ? {} : { allow_remote: consent, expected_generation_revision: status.generation_revision }) }, 'PATCH');
    await reload();
  });
  return <section className="ui-subscription" aria-label="ChatGPT 订阅模型">
    <Row readOnly dot={attempt || busy ? 'processing' : error ? 'failed' : status?.sharing ? 'done' : 'unverified'}
      title="ChatGPT" sub={status?.identity?.email || (status?.connected ? '订阅未授权' : undefined)}
      trailing={<div className="ui-subscription-actions">
        {attempt ? <button disabled={busy} onClick={() => perform(async () => {
          await subscriptionRequest(`/login/${attempt.attempt_id}`, undefined, 'DELETE');
          setAttempt(null); popup.current?.close();
        })}>取消</button> : status && !status.sharing ? <button disabled={busy} onClick={login}>登录</button> : null}
        {status?.connected && !attempt ? <button disabled={busy} onClick={() => perform(async () => {
          const result = await subscriptionRequest('/logout', { expected_revision: status.revision }, 'POST');
          await reload(); setRemoteWarning(result.remote_revoked === false);
        })}>退出</button> : null}
      </div>} />
    {attempt?.authorization_url && !popup.current && <a className="ui-subscription-login" href={attempt.authorization_url} target="_blank" rel="noreferrer">打开登录 ↗</a>}
    {status?.sharing && <Row readOnly title="生成模型" sub={status.selection.model && status.selection_ready === false ? '账号已变化 · 重选模型' : undefined} trailing={<select aria-label="ChatGPT 生成模型" value={status.selection_ready === false ? '' : status.selection.model || ''} disabled={busy}
      onChange={(event) => void select(event.target.value)}>
      <option value="">API / 本机</option>
      {status.selection.model && !models.some((model) => model.id === status.selection.model) && <option value={status.selection.model}>{status.selection.model}</option>}
      {models.map((model) => <option key={model.id} value={model.id}>{model.id}</option>)}
    </select>} />}
    {showEgressSwitch && (status?.sharing || status?.selection.model) && <Row readOnly title="允许发送到你的模型" trailing={<Switch label="允许发送到你的模型" checked={status.allow_remote}
      disabled={busy} onChange={(enabled) => void select(status.selection.model, enabled)} />} />}
    {remoteWarning && <div role="alert">远端退出未确认 · <a href="https://chatgpt.com/" target="_blank" rel="noreferrer">查看账号</a></div>}
    {error && <div role="alert">{error} <button disabled={busy} onClick={() => perform(reload)}>重试</button></div>}
  </section>;
}
