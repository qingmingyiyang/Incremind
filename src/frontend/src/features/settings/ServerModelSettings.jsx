import { useEffect, useRef, useState } from 'react';
import { Row, Switch } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { formatLocalShortDate } from '../../shared/lib/time';
import { serverModelsApi as api, modelPurposes } from './serverModelsApi';
import { validMoney, validModelPrice } from './serverModelMoney';

function useModelRequest(key, load) {
  const [value, setValue] = useState(null), [error, setError] = useState(''), [busy, setBusy] = useState(false), [refresh, setRefresh] = useState(0);
  const inFlight = useRef(false);
  const gate = useRequestScope(userStorageKey(key)), scope = gate.scope;
  useEffect(() => {
    const token = gate.issue('load', scope);
    setValue(null); setError(''); setBusy(false); inFlight.current = false;
    load().then(result => { if (gate.isCurrent(token)) setValue(result); })
      .catch(() => { if (gate.isCurrent(token)) setError('读取未完成 · 重试'); });
  }, [scope, refresh]);
  async function run(operation, accept) {
    if (busy || inFlight.current) return;
    const token = gate.issue('write', scope); if (!token) return;
    inFlight.current = true; setBusy(true); setError('');
    try {
      const result = await operation();
      if (gate.isCurrent(token)) { accept?.(result); return result; }
    } catch (reason) {
      if (gate.isCurrent(token)) setError(reason.status === 409 ? '记录已变化 · 重新读取后保存' : '操作未完成 · 重新读取');
    } finally { if (gate.isCurrent(token)) { inFlight.current = false; setBusy(false); } }
  }
  return { value, setValue, busy, run, refresh: () => { gate.invalidate('write'); setRefresh(old => old + 1); }, error };
}
function Status({ state }) {
  return <>{state.error && <p role="alert">{state.error}</p>}<button type="button" disabled={state.busy} onClick={state.refresh}>重新读取</button>{!state.value && !state.error && <span role="status">读取中</span>}</>;
}
const priceFields = { input_per_million: '输入', output_per_million: '输出', cache_read_per_million: '缓存命中' };
const blankProfile = () => ({ name: '', purpose: 'generation', provider: 'deepseek', base_url: '', model: '', allowed_models: [], enabled: true, prices: {} });
function profileDraft(profile) {
  return { ...(profile || blankProfile()), modelsText: (profile?.allowed_models || []).join('\n'), api_key: '', clear_api_key: false };
}
export function ServerModelProfiles({ scopeKey }) {
  const state = useModelRequest(`${scopeKey}:profiles`, api.profiles);
  const [open, setOpen] = useState(null);
  return <section className="settings-server-models" aria-label="服务器 API"><h2>服务器 API</h2><Status state={state}/>
    {state.value && <div className="settings-card">{state.value.items.map(profile => <div key={profile.profile_id}>
      <Row title={profile.name} sub={`${modelPurposes[profile.purpose]} · ${profile.enabled ? '启用' : '停用'} · ${profile.has_api_key ? '密钥已设置' : '密钥未设置'}`} expanded={open === profile.profile_id} onOpen={() => setOpen(open === profile.profile_id ? null : profile.profile_id)}/>
      {open === profile.profile_id && <><ProfileForm profile={profile} busy={state.busy} onSave={(body, baseline) => state.run(() => api.saveProfile(baseline, body), saved => state.setValue(old => ({ items: old.items.map(item => item.profile_id === saved.profile_id ? saved : item) })))}/>
        {meteringUnits[profile.purpose] && <MeteringPrices profile={profile} scopeKey={scopeKey}/>}</>}
    </div>)}<Row title="新增 API" expanded={open === 'new'} onOpen={() => setOpen(open === 'new' ? null : 'new')}/>
      {open === 'new' && <ProfileForm busy={state.busy} onSave={body => state.run(() => api.saveProfile(null, body), saved => { state.setValue(old => ({ items: [...old.items, saved] })); setOpen(saved.profile_id); })}/>}
    </div>}
  </section>;
}
const meteringUnits = {
  embedding: { unit: 'input_tokens', fields: { input_per_million: '输入 · ¥ / 百万 token' } },
  rerank: { unit: 'input_tokens', fields: { input_per_million: '计费输入 · ¥ / 百万 token' } },
  search: { unit: 'search_requests', fields: { request_per_thousand: '搜索服务 · ¥ / 千次请求' } },
  asr: { unit: 'audio_seconds', fields: { audio_per_second: '音频 · ¥ / 秒' } },
};
function MeteringPrices({ profile, scopeKey }) {
  const state = useModelRequest(`${scopeKey}:${profile.profile_id}:metering`, () => api.meteringPrices(profile));
  return <section className="settings-expand settings-model-admin" aria-label={`${profile.name}用途计价`}><h3>用途计价 · CNY</h3>
    {state.error && <p role="alert">{state.error}</p>}
    {!state.value ? <Status state={state}/> : <MeteringPriceEditor profile={profile} initial={state.value} busy={state.busy}
      onRecheck={() => state.run(() => api.meteringPrices(profile))}
      onSave={(baseline, models) => state.run(() => api.saveMeteringPrices(profile, baseline, models))}/>}
  </section>;
}
function MeteringPriceEditor({ profile, initial, busy, onRecheck, onSave }) {
  const [baseline, setBaseline] = useState(initial), [draft, setDraft] = useState(initial.models), [saved, setSaved] = useState(false);
  const contract = meteringUnits[profile.purpose];
  const models = baseline.configuration.allowed_models;
  const changed = baseline.profile_revision < profile.revision;
  const complete = models.filter(model => Object.keys(contract.fields).some(field => {
    const value = draft[model]?.rates?.[field]; return value !== undefined && value !== '';
  }));
  const valid = complete.every(model => Object.keys(contract.fields).every(field => validModelPrice(draft[model]?.rates?.[field] ?? '')));
  return <form onSubmit={async event => {
    event.preventDefault(); if (busy || !valid) return;
    const models = Object.fromEntries(complete.map(model => [model, { unit: contract.unit,
      rates: Object.fromEntries(Object.keys(contract.fields).map(field => [field, draft[model].rates[field]])) }]));
    const result = await onSave(baseline, models);
    if (result) { setBaseline(result); setSaved(true); }
  }}><fieldset disabled={busy}>
    {models.map(model => <fieldset key={model} className="settings-prices"><legend>{model}</legend>
      {Object.entries(contract.fields).map(([field, label]) => <label key={field}>{label}<input inputMode="decimal" aria-label={`${model}${label}用途单价`} value={draft[model]?.rates?.[field] ?? ''}
        onChange={event => { setSaved(false); const value = event.target.value; setDraft(old => ({ ...old, [model]: { unit: contract.unit, rates: { ...old[model]?.rates, [field]: value } } })); }}/></label>)}
    </fieldset>)}
    <p className="settings-login-hint">账号按本合同计费。全部留空表示未配置；保留原 token 价格与历史记录。发送资格仍取决于已核验协议和请求上限。</p>
    {profile.purpose === 'asr' && <p className="settings-login-hint">音频时长按提供商返回的毫秒数结算；提供商 token 用量单独保留。用量未知继续预留金额。</p>}
    {changed && <p role="status">API 配置已变化 · 重新核对后保存，保留当前输入。</p>}
    <div className="settings-save"><button type="button" onClick={async () => { const result = await onRecheck(); if (result) { setBaseline(result); setSaved(false); } }}>重新核对</button>
      <button type="submit" disabled={!valid || changed}>{saved ? '✓ 已保存' : '保存用途计价'}</button></div>
  </fieldset></form>;
}
function ProfileForm({ profile, busy, onSave }) {
  const [draft, setDraft] = useState(() => profileDraft(profile)), [saved, setSaved] = useState(false);
  const edits = useRef(0);
  const baseline = useRef(profile);
  const models = [...new Set(draft.modelsText.split(/\r?\n/).map(value => value.trim()).filter(Boolean))];
  const update = changes => { edits.current += 1; setSaved(false); setDraft(old => ({ ...old, ...changes })); };
  const pricesValid = models.every(model => Object.keys(priceFields).every(field => !draft.prices?.[model]?.[field] || validModelPrice(String(draft.prices[model][field]))));
  return <form className="settings-expand settings-model-admin" onSubmit={async event => {
    event.preventDefault(); if (busy || !pricesValid) return;
    const edit = edits.current;
    const { name, purpose, provider, base_url, model, enabled } = draft;
    const body = { name, purpose, provider, base_url, model, enabled, allowed_models: models,
      prices: Object.fromEntries(models.map(id => [id, Object.fromEntries(Object.keys(priceFields).map(field => [field, draft.prices?.[id]?.[field] || null]))])),
      ...(draft.api_key ? { api_key: draft.api_key } : {}), ...(profile && draft.clear_api_key ? { clear_api_key: true } : {}) };
    const result = await onSave(body, baseline.current);
    if (result) {
      baseline.current = result;
      if (edits.current === edit) { setDraft(profileDraft(result)); setSaved(true); }
      else setDraft(old => ({ ...old, ...(old.api_key === body.api_key ? { api_key: '' } : {}) }));
    }
  }}><fieldset className="settings-profile-fields" disabled={busy && !profile}><div className="settings-fields">
    <label>名称<input required aria-label="API 名称" value={draft.name} onChange={event => update({ name: event.target.value })}/></label>
    <label>用途<select aria-label="API 用途" value={draft.purpose} disabled={Boolean(profile)} onChange={event => update({ purpose: event.target.value })}>{Object.entries(modelPurposes).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></label>
    <label>提供商<input required aria-label="API 提供商" value={draft.provider} onChange={event => update({ provider: event.target.value })}/></label>
    <label>{draft.provider === 'brave' ? '搜索服务' : '主模型'}<input required aria-label="API 主模型" value={draft.model} onChange={event => update({ model: event.target.value })}/></label>
    <label className="settings-wide">地址<input required aria-label="API 地址" value={draft.base_url} onChange={event => update({ base_url: event.target.value })}/></label>
    <label className="settings-wide">允许模型 · 每行一个<textarea required aria-label="API 允许模型" rows={3} value={draft.modelsText} onChange={event => update({ modelsText: event.target.value })}/></label>
    <label className="settings-wide">密钥<input type="password" autoComplete="new-password" aria-label="API 新密钥" value={draft.api_key} placeholder={profile?.has_api_key ? '已设置 · 留空保留' : '未设置'} disabled={draft.clear_api_key} onChange={event => update({ api_key: event.target.value })}/></label>
  </div>
    {draft.purpose === 'search' && <Row readOnly title="Brave Web Search" sub="搜索服务 · 按请求计价" trailing={<button type="button" disabled={busy} onClick={() => update({ provider: 'brave', base_url: 'https://api.search.brave.com/res/v1', model: 'brave-web-search', modelsText: 'brave-web-search' })}>填入配置</button>}/>}
    {draft.purpose === 'asr' && <Row readOnly title="TokenHub Hy-ASR" sub="音频转写 · 账号按秒计价" trailing={<button type="button" disabled={busy} onClick={() => update({ provider: 'tokenhub-asr', base_url: 'https://tokenhub.tencentmaas.com/v1/wand/asrproxy/sync_transcribe', model: 'hy-asr-3.0-preview', modelsText: 'hy-asr-3.0-preview' })}>填入配置</button>}/>}
    <Row readOnly title="启用" trailing={<Switch label="启用 API" checked={draft.enabled} onChange={enabled => update({ enabled })}/>}/>
    {profile && <Row readOnly title="清除密钥" trailing={<Switch label="清除 API 密钥" checked={draft.clear_api_key} onChange={clear_api_key => update({ clear_api_key, api_key: '' })}/>}/>}
    {models.map(model => <fieldset className="settings-prices" key={model}><legend>{model} · ¥ / 百万 token</legend>{Object.entries(priceFields).map(([field, name]) => <label key={field}>{name}<input inputMode="decimal" aria-label={`${model}${name}服务器单价`} title="0 至 1000000，最多 6 位小数；留空表示未配置" aria-invalid={Boolean(draft.prices?.[model]?.[field]) && !validModelPrice(String(draft.prices[model][field]))} value={draft.prices?.[model]?.[field] ?? ''} onChange={event => update({ prices: { ...draft.prices, [model]: { ...draft.prices?.[model], [field]: event.target.value } } })}/></label>)}</fieldset>)}
    <p className="settings-login-hint">共享 API 按模型单价扣费。未配置价格或未核验请求消费上限的模型将拒绝发送。</p>
    {draft.purpose === 'search' && <p className="settings-login-hint">Brave 需设置中央密钥和用途计价、分配给账号，并由账号开启搜索外发。原生模型搜索缺少已核验的计费请求上限时保持停用。</p>}
    <div className="settings-save"><button disabled={busy || !pricesValid || !models.includes(draft.model)} type="submit">{saved ? '✓ 已保存' : '保存 API'}</button></div></fieldset>
  </form>;
}
export function UserModelAllocation({ user, scopeKey }) {
  const state = useModelRequest(`${scopeKey}:${user.user_id}:allocation`, () => Promise.all([api.allocation(user), api.profiles()]).then(([allocation, profiles]) => ({ allocation, profiles: profiles.items })));
  return <section className="settings-model-admin" aria-label={`${user.name}模型分配`}><h3>模型分配</h3><Status state={state}/>{state.value && <AllocationForm key={state.value.allocation.revision} user={user} value={state.value} busy={state.busy} onSave={body => state.run(() => api.saveAllocation(user, state.value.allocation, body), allocation => state.setValue(old => ({ ...old, allocation })))}/>}</section>;
}
function AllocationForm({ user, value, busy, onSave }) {
  const [mode, setMode] = useState(value.allocation.mode), [profiles, setProfiles] = useState(value.allocation.profiles || {});
  return <form onSubmit={event => { event.preventDefault(); if (!busy) onSave({ mode, profiles: mode === 'shared' ? Object.fromEntries(Object.entries(profiles).filter(([, id]) => id)) : {} }); }}>
    <label>使用方式<select aria-label={`${user.name}模型使用方式`} value={mode} disabled={busy} onChange={event => setMode(event.target.value)}><option value="independent">账号独立 API</option><option value="shared">服务器共享 API</option></select></label>
    {mode === 'shared' && <div className="settings-fields">{Object.entries(modelPurposes).map(([purpose, name]) => <label key={purpose}>{name}<select aria-label={`${user.name}${name}API`} disabled={busy} value={profiles[purpose] || ''} onChange={event => setProfiles(old => ({ ...old, [purpose]: event.target.value }))}><option value="">未分配</option>{value.profiles.filter(item => item.purpose === purpose).map(item => <option key={item.profile_id} value={item.profile_id} disabled={!item.enabled || !item.configured}>{item.name}{!item.enabled ? ' · 停用' : !item.configured ? ' · 未配置' : ''}</option>)}</select></label>)}</div>}
    <div className="settings-save"><button type="submit" disabled={busy || user.deletion_pending}>保存分配</button></div>
  </form>;
}
export function UserModelBalance({ user, admin, scopeKey }) {
  const state = useModelRequest(`${scopeKey}:${user.user_id}:balance`, () => api.balance(user).then(balance => ({ balance })));
  const [amount, setAmount] = useState(''), [reason, setReason] = useState('');
  async function reload() { return { balance: await api.balance(user) }; }
  return <section className="settings-model-admin" aria-label={`${user.name}模型金额`}><h3>模型金额 · CNY</h3><Status state={state}/>{state.value && <>
    <div className="settings-balance-facts">{[['余额', 'balance'], ['预留', 'reserved'], ['可用', 'available'], ['已消费', 'spent']].map(([name, field]) => <div key={field}><span>{name}</span><strong>{state.value.balance[field]}</strong></div>)}</div>
    {state.value.balance.blocked_reason && <p role="status" title={state.value.balance.blocked_reason}>模型消费已暂停 · 检查待核算记录</p>}
    {admin && <form onSubmit={event => { event.preventDefault(); const submitted = { amount, reason }; state.run(async () => { await api.adjust(user, state.value.balance, submitted.amount, submitted.reason); return reload(); }, result => { state.setValue(result); setAmount(''); setReason(''); }); }}><div className="settings-fields">
      <label>增减金额<input aria-label={`${user.name}增减金额`} inputMode="decimal" value={amount} disabled={state.busy} onChange={event => setAmount(event.target.value)}/></label><label>原因<input required maxLength={500} aria-label={`${user.name}金额调整原因`} value={reason} disabled={state.busy} onChange={event => setReason(event.target.value)}/></label></div><div className="settings-save"><button type="submit" disabled={state.busy || !validMoney(amount, true) || !reason.trim()}>调整金额</button></div></form>}
    <h3>消费记录 · 待核算 {state.value.balance.pending_count}</h3><ConsumptionList user={user} admin={admin} scopeKey={scopeKey} balanceRevision={state.value.balance.revision} disabled={state.busy} onReconcile={(attempt, amount, reason) => state.run(async () => { await api.reconcile(user, attempt, amount, reason); return reload(); }, state.setValue)}/>
  </>}</section>;
}
function ConsumptionList({ user, admin, scopeKey, balanceRevision, disabled, onReconcile }) {
  const [filter, setFilter] = useState(admin ? 'pending' : 'all');
  return <><div className="settings-modes" role="group" aria-label="消费状态">{[['pending', '待核算'], ['settled', '已结算'], ['all', '全部']].map(([value, label]) => <button type="button" key={value} aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}</button>)}</div>
    <ConsumptionPage key={`${filter}:${balanceRevision}`} user={user} admin={admin} scopeKey={scopeKey} filter={filter} disabled={disabled} onReconcile={onReconcile}/></>;
}
function ConsumptionPage({ user, admin, scopeKey, filter, disabled, onReconcile }) {
  const state = useModelRequest(`${scopeKey}:${user.user_id}:attempts:${filter}`, () => api.attempts(user, filter));
  return <><Status state={state}/>{state.value && <div className="settings-model-attempts">{state.value.items.map(attempt => <AttemptRow key={`${attempt.attempt_id}:${attempt.revision}`} attempt={attempt} admin={admin} busy={disabled || state.busy} onSave={(amount, reason) => onReconcile(attempt, amount, reason)}/>)}{!state.value.items.length && <span>暂无记录</span>}
    {state.value.has_more && <button type="button" disabled={disabled || state.busy} onClick={() => state.run(() => api.attempts(user, filter, state.value.next_cursor), page => state.setValue(old => ({ ...page, items: [...old.items, ...page.items] })))}>更多记录</button>}</div>}</>;
}
function AttemptRow({ attempt, admin, busy, onSave }) {
  const [amount, setAmount] = useState(''), [reason, setReason] = useState('');
  const unsent = attempt.transmission?.status === 'confirmed_none';
  return <div><Row readOnly title={attempt.model || attempt.attempt_id} sub={unsent ? '未发送 · 已释放预留' : ({ reserved: '发送中', pending_review: '待核算', settled: '已结算' })[attempt.status] || attempt.status} meta={attempt.charged_amount ?? attempt.reserved_amount ?? '—'}/>
    <details><summary>消费明细</summary><Row readOnly title="提供商" meta={attempt.provider}/><Row readOnly title="时间" meta={formatLocalShortDate(attempt.created_at)}/><Row readOnly title="预留金额" meta={attempt.reserved_amount}/><Row readOnly title="已扣金额" meta={attempt.charged_amount ?? '待核算'}/>
      {attempt.metering && <><Row readOnly title="计价单位" meta={({ input_tokens: '输入 token', search_tokens_calls: 'token 与搜索次数', search_requests: '搜索请求', audio_seconds: '音频秒数' })[attempt.metering.price.unit]}/>
        {Object.entries(attempt.metering.observation || {}).map(([field, value]) => <Row readOnly key={field} title={({ input_tokens: '计费输入 token', output_tokens: '输出 token', cache_read_tokens: '缓存命中 token', search_calls: '搜索次数', requests: '已完成请求', duration_ms: '音频毫秒数', usage_total_token: '提供商 token', sentence_end_ms: '末句结束毫秒数' })[field] || '实际计量'} meta={value == null ? '未报告' : String(value)}/>)}
        {!attempt.metering.observation && <p>{unsent ? '已核验 HTTP 尚未开始 · 保留原拒绝记录' : '实际用量待核算'}</p>}</>}
    </details>
    {admin && attempt.status === 'pending_review' && <form onSubmit={event => { event.preventDefault(); if (!busy) onSave(amount, reason); }}><div className="settings-fields"><label>实际金额<input inputMode="decimal" aria-label={`${attempt.attempt_id}实际金额`} disabled={busy} value={amount} onChange={event => setAmount(event.target.value)}/></label><label>核算依据<input required aria-label={`${attempt.attempt_id}核算依据`} disabled={busy} value={reason} onChange={event => setReason(event.target.value)}/></label></div><div className="settings-save"><button disabled={busy || !validMoney(amount) || !reason.trim()} type="submit">确认核算</button></div></form>}
  </div>;
}
