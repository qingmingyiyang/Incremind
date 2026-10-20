import { useEffect, useRef, useState } from 'react';
import { Row } from '../../shared/ui';
import { userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { settingsApi } from './settingsApi';

const MAX_IMPORT_BYTES = 262144;
const spaceKey = () => userStorageKey('asr-lexicon');

export function AsrLexicon({ projectId, api = settingsApi }) {
  const [userSpace, setUserSpace] = useState(spaceKey);
  const gate = useRequestScope(`${userSpace}:${projectId}`), scope = gate.scope;
  const [loaded, setLoaded] = useState(null), [preview, setPreview] = useState(null);
  const [text, setText] = useState(''), [format, setFormat] = useState('txt');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [refresh, setRefresh] = useState(0);
  const controller = useRef(null), fileInput = useRef(null);
  const current = token => spaceKey() === userSpace && gate.isCurrent(token);
  useEffect(() => {
    const changed = () => setUserSpace(spaceKey());
    window.addEventListener('chriptmas-user-space', changed);
    window.addEventListener('chriptmas-device-required', changed);
    return () => {
      window.removeEventListener('chriptmas-user-space', changed);
      window.removeEventListener('chriptmas-device-required', changed);
    };
  }, []);
  useEffect(() => {
    setText(''); setFormat('txt');
  }, [scope]);
  useEffect(() => {
    setLoaded(null); setPreview(null); setError(''); setBusy(false);
    const token = gate.issue('load', scope), request = new AbortController();
    api.loadAsrLexicon(request.signal).then(value => {
      if (current(token)) setLoaded({ scope, value });
    }).catch(() => { if (current(token)) setError('读取未完成 · 重试'); });
    return () => {
      request.abort(); controller.current?.abort(); gate.invalidate('load'); gate.invalidate('operation');
    };
  }, [api, scope, refresh]);
  const value = loaded?.scope === scope ? loaded.value : null;
  function edit(nextText, nextFormat = format) {
    gate.invalidate('operation'); setText(nextText); setFormat(nextFormat); setPreview(null); setError('');
  }
  async function readFile(file) {
    if (!file || busy || !value) return;
    const token = gate.issue('operation', scope); if (!token || spaceKey() !== userSpace) return;
    setPreview(null); setError('');
    const extension = file.name.split('.').pop().toLowerCase();
    if (!['txt', 'csv', 'json'].includes(extension)) { setError('文件格式不支持 · 选择 TXT、CSV 或 JSON'); return; }
    if (file.size > MAX_IMPORT_BYTES) { setError('文件超过 256 KB · 拆分后导入'); return; }
    setBusy(true);
    try {
      // 文件只在本机读取；预览和写入分别需要用户点击。
      const bytes = await new Promise((resolve, reject) => {
        const reader = new FileReader(); reader.onload = () => resolve(reader.result);
        reader.onerror = reject; reader.readAsArrayBuffer(file);
      });
      const content = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
      if (current(token)) { setText(content); setFormat(extension); }
    } catch { if (current(token)) setError('文件读取失败 · 使用 UTF-8 文件'); }
    finally { if (current(token)) setBusy(false); }
  }
  async function importTerms(mode) {
    if (busy || !value || !text.trim() || spaceKey() !== userSpace || mode === 'commit' && !preview) return;
    if (new TextEncoder().encode(text).length > MAX_IMPORT_BYTES) { setError('内容超过 256 KB · 拆分后导入'); return; }
    const token = gate.issue('operation', scope); if (!token) return;
    const request = new AbortController(); controller.current = request;
    setBusy(true); setError('');
    try {
      const result = await api.importAsrLexicon({ expected_revision: value.revision, text, format, preview: mode === 'preview' }, request.signal);
      if (!current(token)) return;
      if (mode === 'preview') setPreview(result);
      else {
        setText(''); setPreview(null);
        const updated = await api.loadAsrLexicon(request.signal);
        if (current(token)) setLoaded({ scope, value: updated });
      }
    } catch (reason) {
      if (current(token)) {
        setPreview(null);
        setError(reason.status === 409 ? '词表已变化 · 刷新' : reason.status === 413 ? '内容超过 256 KB · 拆分后导入'
          : reason.detail === 'asr_lexicon_capacity_exceeded' ? '词表超过上限 · 减少词语或重点词'
            : reason.status === 400 || reason.status === 422 ? '词表格式有误 · 检查词语和权重' : '操作未完成 · 重试');
      }
    } finally { if (current(token)) { setBusy(false); controller.current = null; } }
  }
  return <section className="settings-asr-lexicon" aria-label="热词表">
    <Row readOnly title="热词表" meta={value ? `${value.terms.filter(term => term.status !== 'retired').length} 词` : '—'}/>
    {value && <>
      <Row readOnly title="适用" meta="本机 · 实时语音" sub="Hy-ASR 暂不支持"/>
      <div className="settings-asr-terms" aria-label="当前热词">{value.terms.filter(term => term.status !== 'retired').map(term => <span key={term.id || term.term}>{term.term} <small title="本机转写仅按权重优先排列">{term.weight}</small></span>)}</div>
      <div className="settings-asr-import">
        <label>格式<select aria-label="热词格式" value={format} disabled={busy} onChange={event => edit(text, event.target.value)}>
          <option value="txt">TXT</option><option value="csv">CSV</option><option value="json">JSON</option>
        </select></label>
        <button type="button" disabled={busy} onClick={() => fileInput.current?.click()}>选文件</button>
        <input hidden ref={fileInput} type="file" aria-label="导入热词文件" accept=".txt,.csv,.json" disabled={busy} onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; readFile(file); }}/>
      </div>
      <textarea aria-label="批量热词" value={text} maxLength={MAX_IMPORT_BYTES} disabled={busy} placeholder={format === 'json' ? '[{"term":"词语","weight":4}]' : format === 'csv' ? 'term,weight\n词语,4' : '每行一个词语'} title="UTF-8，最多256 KB；TXT每行词语或词语加Tab和权重；CSV含term,weight表头；JSON为词语对象数组；权重1至5，重点词is_super为true；已有词语保留" onChange={event => edit(event.target.value)}/>
      <div className="settings-asr-actions"><button type="button" disabled={busy || !text.trim()} onClick={() => importTerms('preview')}>预览</button>
        {preview && <><span role="status">新增 {preview.added_count} · 更新 {preview.updated_count} · 保留 {preview.unchanged_count}</span><button type="button" disabled={busy} onClick={() => importTerms('commit')}>导入</button></>}
        {busy && <span role="status">处理中</span>}
      </div>
      {preview && <div className="settings-asr-terms" aria-label="导入预览">{preview.terms.map(term => <span key={term.term}>{term.term} <small>{term.weight}</small></span>)}</div>}
    </>}
    {error && <p role="alert">{error} <button type="button" disabled={busy} onClick={() => setRefresh(old => old + 1)}>刷新</button></p>}
  </section>;
}
