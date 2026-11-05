import { useEffect, useState } from 'react';
import { Row } from '../../shared/ui';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { deviceIdentity, isDeviceIdentity } from '../../shared/api/deviceTransport';

export function DeliveredReminders({api,identity,notices,expanded=false,onOpen,onShowAll}) {
  const gate=useRequestScope(identity), scope=gate.scope;
  const [error,setError]=useState('');
  useEffect(() => setError(''),[scope]);
  const items=notices?.items || [];
  async function open(notice) {
    const token=gate.issue('open',scope), captured=deviceIdentity();
    try {
      await api.reminderFeedback({id:notice.id,projectId:notice.project_id,revision:notice.revision,action:'opened'});
      if (!gate.isCurrent(token) || !isDeviceIdentity(captured)) return;
      setError(''); onOpen(notice);
      window.dispatchEvent(new Event('chriptmas-reminders-changed'));
    } catch { if(gate.isCurrent(token)) setError('提醒未打开 · 稍后重试'); }
  }
  if (!items.length && !notices?.failed) return null;
  return <section className="companion-page-delivered" aria-label="已送达提醒">
    <h3 className="companion-page-section">到期提醒 <span>{items.length}</span></h3>
    {(expanded?items:items.slice(0,3)).map(notice => <Row key={`${notice.project_id}:${notice.id}`} dot="pending"
      title={notice.text} meta={notice.project_name || notice.project_id} onOpen={() => open(notice)}/>)}
    {!expanded && items.length>3 && <button type="button" className="companion-page-delivered-more" onClick={onShowAll}>查看全部{items.length}条</button>}
    {(error || notices?.failed) && <p className="companion-page-error" role="status">{error || '部分项目的提醒未读取 · 稍后重试'}</p>}
  </section>;
}
