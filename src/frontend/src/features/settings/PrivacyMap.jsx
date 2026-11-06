import { useId } from 'react';
import { REMOTE_PROCESSING_HINT, PRIVATE_MATERIAL_HINT } from '../../shared/lib/privacyMessages';
import { EgressSwitch } from './EgressSwitch';

export function PrivacyMap({ model, externalAgent, privacy, projects, api, run, busy }) {
  const hintId = useId();
  const purposes = [['generation', '生成'], ['vision', '识图'], ['asr', '转写'], ['embedding', '向量'], ['rerank', '重排'], ['search', '搜索']];
  if (externalAgent) purposes.push(['agent', '外部 agent']);
  const uses = purposes.map(([purpose, name]) => {
    const row = purpose === 'agent' ? externalAgent : model[purpose] || {};
    const local = purpose === 'generation' && model.generation_mode.mode === 'local' || ['vision', 'embedding'].includes(purpose) && row.mode === 'local';
    const enabled = purpose === 'asr' ? row.enabled === true && row.egress_manifest?.consented === true : row.allow_remote === true;
    const target = local ? '本机' : purpose === 'agent' ? Object.entries(row.clients || {}).filter(([, enabled]) => enabled).map(([id]) => id).join(' · ') || '未启用' : purpose === 'generation' && model.generation_mode.mode === 'subscription' ? 'ChatGPT 订阅' : row.model || row.provider || (row.configured ? '已配置' : '未接入');
    async function toggle(next) {
      if (purpose === 'agent') return run(() => api.saveExternalAgent({ allow_remote: next, include_profile: row.include_profile, daily_limit: row.daily_limit, clients: { ...row.clients }, expected_revision: row.revision }));
      if (purpose === 'asr') return run(() => next ? api.enableAsr() : api.disableAsr());
      if (purpose === 'generation' && model.generation_mode.mode === 'subscription') return run(() => api.toggleSubscription(next, row.revision));
      return run(() => api.saveModel({ purpose, serverManaged: row.server_managed === true, baseUrl: row.base_url, model: row.model, apiKey: '', enabled: next || row.enabled === true, allowRemote: next, expectedRevision: row.revision }));
    }
    return { purpose, name, row, local, enabled, target, toggle };
  });
  return <section aria-label="外发地图"><h2 className="settings-caption">外发地图</h2><p className="settings-egress-note" id={hintId}>普通资料按用途外发。私密项目和单独设为私密的资料继续受保护。</p><p className="settings-egress-note">● 会外发 · ○ 本机 · × 私密 · — 关闭</p><div className="settings-card settings-map-scroll"><table className="settings-egress-map"><thead><tr><th scope="col">用途</th><th scope="col">去处</th>{projects.map(project => {
    const privateProject = privacy.private_projects.includes(project.id) || project.private;
    const action = privateProject ? '取消私密' : '设为私密';
    return <th key={project.id} scope="col"><span className="settings-map-project">{project.name} · {privateProject ? '私密' : '普通'}</span>{!['me', 'inbox'].includes(project.id) && <button type="button" disabled={busy} aria-label={`${action} · ${project.name} · 外发地图`} aria-describedby={hintId} title={PRIVATE_MATERIAL_HINT} onClick={() => run(() => api.savePrivacy(privateProject ? privacy.private_projects.filter(id => id !== project.id) : [...privacy.private_projects, project.id], privacy.revision))}>{action}</button>}</th>;
  })}</tr></thead><tbody>{uses.map(use => <tr key={use.purpose}><th scope="row"><span className="settings-map-purpose">{use.name}</span><EgressSwitch label={`${use.name}地图外发`} aria-describedby={hintId} title={use.local ? '本机处理' : REMOTE_PROCESSING_HINT} checked={!use.local && use.enabled} disabled={busy || use.local || use.purpose === 'agent' && !Number.isInteger(use.row.revision) || use.purpose === 'asr' && use.row.server_managed && !use.enabled} onChange={use.toggle}/></th><td>{use.target}</td>{projects.map(project => {
    const privateProject = privacy.private_projects.includes(project.id) || project.private;
    const state = use.local ? '本机' : !use.enabled ? '关闭' : privateProject ? '私密拦下' : '会外发';
    return <td key={project.id}><span className={`settings-map-cell ${state === '会外发' ? 'is-remote' : ''}`} title={`${project.name} · ${use.name} · ${state}`} aria-label={`${project.name}${use.name}${state}`}>{use.local ? '○' : !use.enabled ? '—' : privateProject ? '×' : '●'}</span></td>;
  })}</tr>)}</tbody></table></div></section>;
}
