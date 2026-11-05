import { useEffect, useState } from 'react';
import { FocusPanel } from '../../shared/ui';
import { deviceIdentity, isDeviceIdentity } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { nativeMcpApi } from './nativeMcpApi';
import '../settings/ExternalAgentSettings.css';

const labels = { 'memory:read': '召回与查看', 'memory:remember': '记住原件', 'memory:propose': '提出待确认认识', 'memory:use': '登记使用编号' };

export function HumanAuthorization({ routeHash, onClose }) {
  const params = new URLSearchParams(routeHash.slice(1)), consent = params.get('native-mcp-consent'), candidate = params.get('native-mcp-review');
  const project = params.get('project_id') || 'default', target = params.get('native-mcp-target');
  const identity = deviceIdentity(), gate = useRequestScope(`${routeHash}\0${identity.key}\0${identity.revision}`), scope = gate.scope;
  const [response, setResponse] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0);
  const value = response?.scope === scope ? response.value : null;
  useEffect(() => {
    if (!consent && !candidate) return undefined;
    const token = gate.issue('read', scope), controller = new AbortController();
    setError(''); setResponse(null); setBusy(false);
    const operation = consent ? nativeMcpApi.consent(consent, controller.signal) : nativeMcpApi.candidate(candidate, project, controller.signal);
    operation.then(result => {
      if (!gate.isCurrent(token) || !isDeviceIdentity(identity)) return;
      if (target && result.target_user_id !== target) { setError('请在主应用选择此认识所属的用户空间后重新读取'); return; }
      setResponse({ scope, value: result });
    }).catch(reason => { if (gate.isCurrent(token) && reason.name !== 'AbortError') setError(reason.message); });
    return () => { controller.abort(); gate.invalidate('read'); gate.invalidate('write'); };
  }, [gate, scope, consent, candidate, project, target, refresh]);
  if (!consent && !candidate) return null;
  async function act(action) {
    if (busy || !value || !isDeviceIdentity(identity)) return;
    const token = gate.issue('write', scope); if (!token) return;
    setBusy(true); setError('');
    try {
      let result;
      if (consent) result = await nativeMcpApi.decide(consent, { expected_revision: value.revision, approve: action === 'approve' });
      else {
        const body = { project, revision: value.revision, action };
        const prepared = await nativeMcpApi.prepare(candidate, body);
        if (!gate.isCurrent(token) || !isDeviceIdentity(identity)) return;
        result = await nativeMcpApi.commit(candidate, { ...body, nonce: prepared.nonce });
      }
      if (!gate.isCurrent(token) || !isDeviceIdentity(identity)) return;
      if (consent) {
        const redirect = new URL(result.redirect_uri);
        if (redirect.origin !== 'https://chatgpt.com' || !redirect.pathname.startsWith('/connector/oauth/')) throw new Error('回调地址未通过核验');
        globalThis.location.assign(redirect.href);
      } else setResponse({ scope, value: { ...value, result } });
    } catch (reason) { if (gate.isCurrent(token)) setError(reason.message); }
    finally { if (gate.isCurrent(token)) setBusy(false); }
  }
  return <FocusPanel className="native-mcp-human" title={consent ? '授权 ChatGPT 使用记忆' : '审核待确认认识'} onClose={onClose} status={value?.result ? 'done' : 'pending'}>
    {!value && !error && <p role="status">读取当前资格</p>}
    {error && <p role="alert">{error} <button type="button" disabled={busy} onClick={() => setRefresh(old => old + 1)}>重新读取</button></p>}
    {value && (consent ? <>
      <p>允许以下操作</p><ul>{value.scopes.map(item => <li key={item}>{labels[item] || item}</li>)}</ul>
      <p>当前用户空间 {value.target_user_id} · 外发开关、私密限制和使用回执继续生效。</p>
      <button type="button" disabled={busy} onClick={() => act('approve')}>授权并返回 ChatGPT</button><button type="button" disabled={busy} onClick={() => act('deny')}>拒绝授权</button>
    </> : <>
      <p>{typeof value.content === 'string' ? value.content : JSON.stringify(value.content)}</p>
      {value.conditions && <p>{typeof value.conditions === 'string' ? value.conditions : JSON.stringify(value.conditions)}</p>}
      {value.result ? <p role="status">{value.result.action === 'confirm' ? '认识已确认' : '认识已丢弃'} · 修订 {value.result.revision}</p> : <div className="external-agent-card-actions"><button type="button" disabled={busy} onClick={() => act('confirm')}>确认认识</button><button type="button" disabled={busy} onClick={() => act('discard')}>丢弃认识</button></div>}
    </>)}
  </FocusPanel>;
}
