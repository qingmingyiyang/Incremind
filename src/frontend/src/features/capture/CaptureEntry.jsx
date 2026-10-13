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
  useEffect(() => {
    let active = true;
    const load = async () => {
      const issue = new URLSearchParams(location.hash.slice(1)).get('capture-error');
      if (issue) { setError(issue === 'credentials' ? '含登录凭据 · 改用设置' : '接收未完成 · 重新分享'); return; }
      setBusy(true); setError(''); setRetryable(true);
      try {
        await syncCaptureOwner();
        const parameter = new URLSearchParams(location.hash.slice(1)).get('capture');
        const selected = parameter === 'pending' ? null : parameter;
        const captures = (await captureStore.list()).filter(value => selected ? value.id === selected : !value.owner || value.owner === captureOwner())
          .sort((a, b) => a.createdAt - b.createdAt);
        if (!captures.length) { if (selected) throw new Error('capture_missing'); return; }
        if (captures.some(value => value.owner && value.owner !== captureOwner())) throw new Error('capture_owner_changed');
        if (active) setRows(captures);
        for (const capture of captures) {
          if (!active) break;
          const result = await sendCapture(capture.id);
          if (active) setRows(values => values.map(value => value.id === capture.id ? result : value));
        }
      } catch (reason) {
        if (active) { setError(errors[reason.message] || '发送未完成 · 重试'); setRetryable(!Object.hasOwn(errors, reason.message)); }
      } finally { if (active) setBusy(false); }
    };
    load();
    return () => { active = false; };
  }, [retry]);
  return <main className="capture-page">
    <h1>记住</h1>
    {rows.map(row => <section key={row.id}>
      {row.state === 'done' ? row.results.map(result => <Row key={result.turn.id}
        dot={result.turn.receipt?.remember?.state === 'processing' ? 'processing' : result.turn.receipt?.remember?.state === 'failed' ? 'failed' : 'unverified'}
        title={result.turn.receipt?.remember?.title || '原件'} onOpen={() => {
          location.assign('/#' + new URLSearchParams({ view: 'workbench', project_id: result.project_id || 'default', thread_id: result.thread_id, turn_id: result.turn.id }));
        }} />) : <Row dot={error ? 'failed' : 'processing'} title={row.files?.length ? `原件 ${row.files.length}` : '原件'} readOnly />}
    </section>)}
    {error && <p role="alert">{error}</p>}
    {!busy && error && retryable && <button type="button" onClick={() => setRetry(value => value + 1)}>重试</button>}
    <a href="/#view=workbench">工作台</a>
    {!busy && rows.length > 0 && <button type="button" onClick={async () => {
      await Promise.all(rows.map(row => captureStore.remove(row.id))); location.assign('/#view=workbench');
    }}>清除</button>}
  </main>;
}
