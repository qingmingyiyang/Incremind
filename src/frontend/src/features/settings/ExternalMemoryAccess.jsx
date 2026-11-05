import { useEffect, useState } from 'react';
import { Row, Switch, StatusDot } from '../../shared/ui';
import { deviceIdentity, isDeviceIdentity, readUserSpace } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { nativeMcpApi, nativeClientConfiguration } from '../externalAgent/nativeMcpApi';

const scopes = [['memory:read', '召回与查看'], ['memory:remember', '记住原件'], ['memory:propose', '提出待确认认识'], ['memory:use', '登记使用编号']];

export function ExternalMemoryAccess({ projectId }) {
  const identity = deviceIdentity(), target = readUserSpace()?.user_id || '';
  const gate = useRequestScope(`${projectId}\0${identity.key}\0${identity.revision}\0${target}`), scope = gate.scope;
  const [response, setValue] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0);
  const [origin, setOrigin] = useState(''), [callback, setCallback] = useState(''), [client, setClient] = useState('claude');
  const [selected, setSelected] = useState(['memory:read']), [secretState, setSecret] = useState(null), [copied, setCopied] = useState('');
  const value = response?.scope === scope ? response.value : null, secret = secretState?.scope === scope ? secretState : null;
  useEffect(() => {
    const token = gate.issue('read', scope), controller = new AbortController();
    setValue(null); setError(''); setBusy(false); setSecret(null); setCopied('');
    nativeMcpApi.read(controller.signal).then(result => {
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) { setValue({ scope, value: result }); setOrigin(result.origin || ''); }
    }).catch(reason => { if (gate.isCurrent(token) && reason.name !== 'AbortError') setError(reason.code === 'native_mcp_server_required' ? '原生远程 MCP 需要服务器模式' : reason.message); });
    return () => { controller.abort(); gate.invalidate('read'); gate.invalidate('write'); gate.invalidate('copy'); };
  }, [gate, scope, refresh]);
  async function run(operation) {
    if (busy || !isDeviceIdentity(identity)) return;
    const token = gate.issue('write', scope); if (!token) return;
    gate.invalidate('read'); gate.invalidate('copy'); setBusy(true); setError(''); setCopied('');
    try { const result = await operation(); if (gate.isCurrent(token) && isDeviceIdentity(identity)) return result; }
    catch (reason) { if (gate.isCurrent(token)) setError(reason.message); }
    finally {
      if (gate.isCurrent(token) && isDeviceIdentity(identity)) {
        setBusy(false);
        try { const result = await nativeMcpApi.read(); if (gate.isCurrent(token)) setValue({ scope, value: result }); }
        catch (reason) { if (gate.isCurrent(token)) setError(old => old || reason.message); }
      }
    }
  }
  async function copy(text, label) {
    const token = gate.issue('copy', scope); if (!token || !isDeviceIdentity(identity)) return;
    try { await navigator.clipboard.writeText(text); if (gate.isCurrent(token) && isDeviceIdentity(identity)) setCopied(label); }
    catch { if (gate.isCurrent(token)) setError('复制未完成 · 可选择文字复制'); }
  }
  let configuration = null;
  if (value?.origin) { try { configuration = nativeClientConfiguration(value.origin, client); } catch { configuration = null; } }
  return <section className="external-agent-guide" aria-label="原生远程记忆接入">
    <h3>原生 MCP · 远程记忆</h3>
    {error && <p role="alert">{error} <button type="button" disabled={busy} onClick={() => setRefresh(old => old + 1)}>重新读取</button></p>}
    {!value && !error && <p role="status">读取接入状态</p>}
    {value?.available === false && <p role="status">原生远程 MCP 与 ChatGPT 接入需要服务器模式。</p>}
    {value?.available === true && <>
      <Row readOnly title="服务器地址" sub={value.origin ? `${value.origin}/mcp` : '尚未配置 HTTPS 地址'} dot={<StatusDot state={value.origin ? 'done' : 'unverified'}/>}/>
      {value.can_configure && <form className="external-agent-local-paths" onSubmit={event => { event.preventDefault(); run(() => nativeMcpApi.configure({ origin: origin.trim(), expected_revision: value.config_revision })); }}>
        <label>HTTPS 地址<input type="url" required autoComplete="off" value={origin} onChange={event => setOrigin(event.target.value)} placeholder="https://memory.example.com" disabled={busy}/></label>
        <button disabled={busy || !origin.trim()}>保存地址</button>
      </form>}
      {value.origin && <>
        <p className="external-agent-note">为外部客户端创建独立钥匙，可随时作废。私密资料不交出，每次交付写回执。</p>
        <div className="external-agent-local-paths"><label>客户端<select value={client} disabled={busy} onChange={event => { setClient(event.target.value); setCopied(''); }}><option value="claude">Claude Code</option><option value="codex">Codex</option></select></label>
          <fieldset disabled={busy}><legend>钥匙权限</legend>{scopes.map(([id, title]) => <label key={id}><input type="checkbox" checked={selected.includes(id)} onChange={event => setSelected(old => event.target.checked ? [...old, id] : old.filter(item => item !== id))}/>{title}</label>)}</fieldset>
          <button type="button" disabled={busy || !selected.length} onClick={async () => {
            const result = await run(() => nativeMcpApi.issue({ client, scopes: selected, name: `${client} · 外部记忆`, expires_at: Math.floor(Date.now() / 1000) + 30 * 86400 }));
            if (result && gate.isScopeCurrent(scope) && isDeviceIdentity(identity)) setSecret({ scope, key: result.key, device: result.device });
          }}>创建 30 天钥匙</button>
        </div>
        {secret && <div className="external-agent-configuration"><p>独立钥匙 · 仅本次显示</p><pre>{secret.key}</pre><button type="button" onClick={() => copy(secret.key, '钥匙')}>复制钥匙</button><button type="button" onClick={() => setSecret(null)}>隐藏</button><p>在客户端本机设置 CHRIPTMAS_MCP_DEVICE_KEY 环境变量。</p></div>}
        {configuration && <div className="external-agent-configuration"><pre>{configuration}</pre><button type="button" disabled={busy} onClick={() => copy(configuration, '配置')}>复制{client === 'codex' ? ' PowerShell 命令' : '配置'}</button><p className="external-agent-note">保存后在客户端检查七个记忆工具 · 连接待验证</p></div>}
        {value.keys.map(key => <Row readOnly key={key.device_id} title={key.client} sub={`${key.scopes.length} 项权限 · ${key.revoked ? '已作废' : '到期 ' + new Date(key.expires_at * 1000).toLocaleDateString()}`} trailing={!key.revoked && <button type="button" disabled={busy} onClick={() => run(() => nativeMcpApi.revoke(key.device_id, { expected_revision: key.device_revision }))}>作废</button>}/>)}
        <h3>ChatGPT · OAuth</h3>
        <Row readOnly title="允许 ChatGPT 使用记忆" sub="独立于执行任务的客户端" trailing={<Switch label="允许ChatGPT使用记忆" checked={value.chatgpt.enabled} disabled={busy} onChange={enabled => run(() => nativeMcpApi.preference({ enabled, expected_revision: value.chatgpt.revision }))}/>}/>
        <form className="external-agent-local-paths" onSubmit={event => { event.preventDefault(); run(() => nativeMcpApi.register({ redirect_uris: [callback.trim()], token_endpoint_auth_method: 'none', grant_types: ['authorization_code', 'refresh_token'], response_types: ['code'], scope: scopes.map(([id]) => id).join(' ') })); }}>
          <label>ChatGPT 回调地址<input type="url" required autoComplete="off" value={callback} disabled={busy} onChange={event => setCallback(event.target.value)} placeholder="粘贴 ChatGPT Plugins 配置页显示的完整回调地址"/></label><button disabled={busy || !callback.trim()}>创建 OAuth 客户端</button>
        </form>
        {value.oauth_clients.map(item => <div key={item.client_id} className="external-agent-configuration"><p>Client ID</p><pre>{item.client_id}</pre><button type="button" onClick={() => copy(item.client_id, 'Client ID')}>复制 Client ID</button><p>在 ChatGPT Plugins 中添加 {value.origin}/mcp，选择 OAuth，使用此 Client ID；登录本应用后选择范围并授权。</p></div>)}
      </>}
      {copied && <p role="status">已复制{copied}</p>}
    </>}
  </section>;
}
