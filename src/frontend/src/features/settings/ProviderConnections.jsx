import { useEffect, useRef, useState } from 'react';
import { ProviderConnectionManager } from './ProviderConnectionManager';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { settingsApi } from './settingsApi';
import { apiEndpointError } from './modelEndpoint';
import './ProviderConnections.css';

// 预设填写新连接；采用用途后仍沿原外发和启用设置执行。
export const PROVIDER_PRESETS = [
  { id: 'deepseek', name: 'DeepSeek', mark: 'DS', baseUrl: 'https://api.deepseek.com', model: 'deepseek-flash' },
  { id: 'qwen', name: '通义千问', mark: 'QW', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1', model: 'qwen-plus' },
  { id: 'kimi', name: 'Kimi', mark: 'KM', baseUrl: 'https://api.moonshot.cn/v1', model: 'kimi-k3.1-pro' },
  { id: 'zhipu', name: '智谱', mark: 'ZP', baseUrl: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-5.3' },
  { id: 'custom', name: 'OpenAI 兼容', mark: 'AI', baseUrl: '', model: '' },
];
const sameEndpoint = (left, right) => left?.replace(/\/+$/, '') === right?.replace(/\/+$/, '');

export function ModelConnectionForm({ row, mode = 'api', preset = PROVIDER_PRESETS[0], api = settingsApi, run, busy = false, onSaved }) {
  const gate = useRequestScope(preset.id), scope = gate.scope;
  const [review, setReview] = useState(null), [reviewing, setReviewing] = useState(false), [reviewError, setReviewError] = useState(''), [conflict, setConflict] = useState(false);
  const activeReview = review?.scope === scope && review.parentRevision === row.revision && review.parentMode === mode ? review : null;
  const currentRow = activeReview?.row || row, currentMode = activeReview?.mode || mode;
  const current = sameEndpoint(currentRow.base_url, preset.baseUrl);
  const [baseUrl, setBaseUrl] = useState(current || preset.id === 'custom' ? currentRow.base_url || '' : preset.baseUrl);
  const [model, setModel] = useState(current || preset.id === 'custom' ? currentRow.model || '' : preset.model);
  const [secret, setSecret] = useState(''), [saved, setSaved] = useState(false), [baseline, setBaseline] = useState(currentRow.revision);
  const edited = useRef(false);
  useEffect(() => { if (!edited.current) { setBaseline(currentRow.revision); if (sameEndpoint(currentRow.base_url, baseUrl)) setModel(currentRow.model || ''); } }, [currentRow.revision]);
  function edit() { if (!edited.current) setBaseline(currentRow.revision); edited.current = true; setSaved(false); }
  const retainKey = currentRow.has_api_key && currentRow.provider_connection?.available !== false && sameEndpoint(currentRow.base_url, baseUrl);
  const disabled = busy || reviewing || currentRow.server_managed || currentMode !== 'api';
  const revisionChanged = conflict || edited.current && baseline !== currentRow.revision;
  const endpointError = apiEndpointError(baseUrl);
  async function readCurrentConfiguration() {
    const token = gate.issue('review', scope); if (!token) return;
    setReviewing(true); setReviewError('');
    try {
      const fresh = await api.load();
      if (!Number.isInteger(fresh?.model?.generation?.revision) || !['api', 'local', 'subscription'].includes(fresh.model.generation_mode?.mode)) throw new Error('invalid_model_configuration');
      if (gate.isCurrent(token)) setReview({ scope, parentRevision: row.revision, parentMode: mode, row: fresh.model.generation, mode: fresh.model.generation_mode.mode });
    } catch { if (gate.isCurrent(token)) setReviewError('当前配置未读取 · 重新读取'); }
    finally { if (gate.isCurrent(token)) setReviewing(false); }
  }
  return <form className="settings-provider-form" onSubmit={event => {
    event.preventDefault(); if (disabled || revisionChanged || endpointError || !baseUrl.trim() || !model.trim() || !secret && !retainKey) return;
    const token = gate.issue('save', scope); if (!token) return;
    const key = secret; setSaved(false);
    run(async () => {
      let result;
      try { result = await api.saveModel({ purpose: 'generation', baseUrl: baseUrl.trim(), model: model.trim(), apiKey: key, allowRemote: currentRow.allow_remote === true, enabled: currentRow.enabled === true, expectedRevision: baseline }); }
      catch (reason) { if (reason.status === 409 && gate.isCurrent(token)) { setConflict(true); await readCurrentConfiguration(); } throw reason; }
      if (gate.isCurrent(token)) { edited.current = false; setConflict(false); setSecret(''); setSaved(true); onSaved?.(result); }
    });
  }}>
    <details open={preset.id === 'custom'}><summary>地址与模型</summary><div className="settings-fields"><label>地址<input aria-label="接入地址" aria-invalid={Boolean(endpointError)} value={baseUrl} disabled={disabled} onChange={event => { edit(); setBaseUrl(event.target.value); }}/></label><label>模型<input aria-label="接入模型" value={model} disabled={disabled} onChange={event => { edit(); setModel(event.target.value); }}/></label></div></details>
    {endpointError && <span role="status" className="settings-login-hint">{endpointError}</span>}
    {currentRow.provider_connection?.available === false && <span className="settings-login-hint" role="status">连接已变化 · <a href="#view=settings&section=model">连接设置</a></span>}
    <div className="settings-provider-key"><label>密钥<input type="password" autoComplete="new-password" aria-label="接入密钥" value={secret} placeholder={retainKey ? '已设置 · 留空保留' : '填入此服务商密钥'} disabled={disabled} onChange={event => { edit(); setSecret(event.target.value); }}/></label><button type="submit" disabled={disabled || revisionChanged || endpointError || !baseUrl.trim() || !model.trim() || !secret && !retainKey}>{saved ? '已保存' : '接入'}</button></div>
    {revisionChanged && <section className="settings-provider-revision" aria-label="接入配置变更"><p role="status">{reviewing ? '读取当前配置' : reviewError || '模型设置已变化 · 核对当前配置'}</p><dl><div><dt>当前地址</dt><dd>{currentRow.base_url || '—'}</dd></div><div><dt>当前模型</dt><dd>{currentRow.model || '—'}</dd></div></dl>{reviewError ? <button type="button" disabled={busy || reviewing} onClick={readCurrentConfiguration}>重新读取</button> : <button type="button" disabled={disabled} onClick={() => { setBaseline(currentRow.revision); setConflict(false); }}>重新核对</button>}</section>}
    {currentRow.server_managed ? <span className="settings-login-hint">服务器管理 · 由管理员配置</span> : currentMode !== 'api' && <span className="settings-login-hint">在生成用途中选择 API 后接入</span>}
  </form>;
}

export function ProviderConnections({ projectId, model, run, busy, target, onConfigure }) {
  return <ProviderConnectionManager projectId={projectId} model={model} presets={PROVIDER_PRESETS} run={run} busy={busy} target={target} onConfigure={onConfigure}/>;
}
