import { useEffect, useState } from 'react';
import { Row } from '../../shared/ui/Row';
import { captureOwner, captureStore, sendCapture, syncCaptureOwner } from './captureQueue';
import './capture.css';

const errors = {
  capture_missing: '分享已清除 · 重新分享',
  capture_owner_changed: '账号已变化 · 返回原账号',
  share_upload_interrupted: '上传状态待核对 · 查看工作台',
  share_turn_interrupted: '提交已中断 · 查看工作台',
  share_credentials_rejected: '含登录凭据 · 改用设置',
  unsupported_share_file: '文件不支持 · 更换文件',
  idempotency_key_conflict: '提交编号冲突 · 查看工作台',
};
export function CaptureEntry() {
  const [rows, setRows] = useState([]), [error, setError] = useState(''), [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(0);
  const [retryable, setRetryable] = useState(true);
  const [failures, setFailures] = useState({});
  useEffect(() => {
    let active = true;
    const load = async () => {
      const issue = new URLSearchParams(location.hash.slice(1)).get('capture-error');
      if (issue) { setError(issue === 'credentials' ? '含登录凭据 · 改用设置' : '接收未完成 · 重新分享'); return; }
      setBusy(true); setError(''); setRetryable(true); setFailures({});
      try {
        const owner = captureOwner();
        await syncCaptureOwner();
        const parameter = new URLSearchParams(location.hash.slice(1)).get('capture');
        const selected = parameter === 'pending' ? null : parameter;
        const captures = (await captureStore.list()).filter(value => selected ? value.id === selected : !value.owner || value.owner === owner)
          .sort((a, b) => a.createdAt - b.createdAt);
        if (captureOwner() !== owner) throw new Error('capture_owner_changed');
        if (!captures.length) { if (selected) throw new Error('capture_missing'); return; }
        if (captures.some(value => value.owner && value.owner !== owner)) throw new Error('capture_owner_changed');
        if (active) setRows(captures);
        for (const capture of captures) {
          if (!active) break;
          if (captureOwner() !== owner) throw new Error('capture_owner_changed');
          try {
            const result = await sendCapture(capture.id);
            if (captureOwner() !== owner) throw new Error('capture_owner_changed');
            if (active) setRows(values => values.map(value => value.id === capture.id ? result : value));
          } catch (reason) {
            if (captureOwner() !== owner || reason.message === 'capture_owner_changed') throw new Error('capture_owner_changed');
            if (active) setFailures(values => ({ ...values, [capture.id]: {
              message: errors[reason.message] || '发送未完成 · 重试', retryable: !Object.hasOwn(errors, reason.message),
            } }));
          }
        }
      } catch (reason) {
        if (active) { setError(errors[reason.message] || '发送未完成 · 重试'); setRetryable(!Object.hasOwn(errors, reason.message)); }
      } finally { if (active) setBusy(false); }
    };
    load();
    return () => { active = false; };
  }, [retry]);
  async function clearCapture(id) {
    if (busy) return;
    const owner = captureOwner();
    setBusy(true); setError('');
    try {
      const capture = await captureStore.get(id);
      if (captureOwner() !== owner || capture?.owner && capture.owner !== owner) throw new Error('capture_owner_changed');
      await captureStore.remove(id);
      if (captureOwner() !== owner) return;
      setRows(values => values.filter(value => value.id !== id));
      setFailures(values => { const next = { ...values }; delete next[id]; return next; });
    } catch (reason) {
      if (reason.message === 'capture_owner_changed') { setError(errors.capture_owner_changed); setRetryable(false); }
      else setFailures(values => ({ ...values, [id]: { message: '清除未完成 · 再试', retryable: false } }));
    } finally { setBusy(false); }
  }
  return <main className="capture-page">
    <h1>记住</h1>
    {rows.map(row => <section key={row.id}>
      {row.state === 'done' ? row.results.map(result => <Row key={result.turn.id}
        dot={result.turn.receipt?.remember?.state === 'processing' ? 'processing' : result.turn.receipt?.remember?.state === 'failed' ? 'failed' : 'unverified'}
        title={result.turn.receipt?.remember?.title || '原件'} onOpen={() => {
          location.assign('/#' + new URLSearchParams({ view: 'workbench', project_id: result.project_id || 'default', thread_id: result.thread_id, turn_id: result.turn.id }));
        }} />) : <Row dot={failures[row.id] || error ? 'failed' : 'processing'} title={row.files?.length ? `原件 ${row.files.length}` : '原件'} readOnly />}
      {failures[row.id] && <p role="alert">{failures[row.id].message}</p>}
      {!busy && <button type="button" onClick={() => clearCapture(row.id)}>清除</button>}
    </section>)}
    {error && <p role="alert">{error}</p>}
    {!busy && (error ? retryable : Object.values(failures).some(value => value.retryable)) && <button type="button" onClick={() => setRetry(value => value + 1)}>重试</button>}
    <a href="/#view=workbench">工作台</a>
  </main>;
}
