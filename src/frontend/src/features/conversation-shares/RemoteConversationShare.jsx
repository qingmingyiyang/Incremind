import { useEffect, useState } from 'react';
import { deviceIdentity, isDeviceIdentity, readDeviceCredential, readUserSpace, userStorageKey } from '../../shared/api/deviceTransport';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { ConversationSharePreview } from '../../shared/ui/ConversationSharePreview';
import { Row } from '../../shared/ui/Row';
import { StatusDot } from '../../shared/ui/StatusDot';
import { sharedProjectsApi } from '../shared-projects/sharedProjectsApi';
import { conversationShareApi as api } from './conversationShareApi';
import { conversationShareList, conversationSharePreview, sameConversationPreview, sameShareSource } from './conversationShareViews';
import { ConversationShareRecovery } from './ConversationShareRecovery';
import { useConversationShareCommand } from './useConversationShareCommand';
import { SharedProjectConversationDestination, SharedProjectConversationStatus, useSharedProjectConversationShare } from './SharedProjectConversationShare';

const sentLabels = { received: '已发送', filing: '对方正在归档', filed: '已归档', revoked: '已撤回' };

export function RemoteConversationShare({ source, onClose, onLocal, onNavigate }) {
  const actor = deviceIdentity();
  const gate = useRequestScope(`${userStorageKey('conversation-share-preview')}:${actor.revision}:${source.scopeKind}:${source.projectId}:${source.turnId}`);
  const scope = gate.scope, [previewState, setPreviewState] = useState(null), [sentState, setSentState] = useState(null);
  const [directoryState, setDirectoryState] = useState(null), [directoryEpoch, setDirectoryEpoch] = useState(0);
  const [previewEpoch, setPreviewEpoch] = useState(0), [sentEpoch, setSentEpoch] = useState(0), [recipient, setRecipient] = useState(null);
  const preview = previewState?.scope === scope ? previewState : null;
  const sent = sentState?.scope === scope ? sentState : null;
  const directory = directoryState?.scope === scope ? directoryState : null;
  const target = readUserSpace()?.user_id || readDeviceCredential()?.device?.user_id;
  const selected = recipient?.scope === scope ? recipient.id : '';
  const canSend = directory?.users?.some(row => row.user_id === selected);
  const refreshPreview = () => setPreviewEpoch(value => value + 1);
  const refreshSent = () => setSentEpoch(value => value + 1);
  const commands = useConversationShareCommand({ enabled: true, source, onChanged: share => {
    gate.invalidate('sent'); refreshSent();
    if (!share) { refreshPreview(); return; }
    setRecipient(null);
    setSentState(current => ({ scope, items: [share, ...(current?.scope === scope ? current.items || [] : []).filter(row => row.id !== share.id)] }));
  } });
  const personalLocked = commands.working || Boolean(commands.visible?.command);
  const projectShare = useSharedProjectConversationShare({ source, blocked: personalLocked, onSourceChanged: refreshPreview });
  const locked = personalLocked || projectShare.visible?.mutating;
  const projectLocked = locked || projectShare.visible?.working;
  useEffect(() => {
    const token = gate.issue('preview', scope), controller = new AbortController();
    const current = () => gate.isCurrent(token) && isDeviceIdentity(actor);
    if (!token) return;
    const previous = preview?.value;
    setPreviewState({ scope, value: previous, loading: true });
    api.preview(source, controller.signal).then(result => {
      if (!current()) return;
      const value = conversationSharePreview(result);
      const unchanged = sameConversationPreview(previous, value);
      if (!unchanged) setRecipient(null);
      setPreviewState({ scope, value: unchanged ? previous : value });
    }).catch(error => { if (current()) setPreviewState({ scope, value: previous, error: error.message }); });
    return () => { controller.abort(); gate.invalidate('preview'); };
  }, [scope, previewEpoch]);
  useEffect(() => {
    const token = gate.issue('directory', scope), controller = new AbortController();
    const current = () => gate.isCurrent(token) && isDeviceIdentity(actor);
    if (!token) return;
    setDirectoryState({ scope, loading: true });
    sharedProjectsApi.users({ signal: controller.signal }).then(users => {
      if (current()) setDirectoryState({ scope, users: users.filter(row => row.user_id !== target) });
    }).catch(error => { if (current()) setDirectoryState({ scope, error: error.message }); });
    return () => { controller.abort(); gate.invalidate('directory'); };
  }, [scope, directoryEpoch]);
  useEffect(() => {
    const token = gate.issue('sent', scope), controller = new AbortController();
    const current = () => gate.isCurrent(token) && isDeviceIdentity(actor);
    if (!token) return;
    setSentState(previous => ({ scope, loading: true, items: previous?.scope === scope ? previous.items : undefined }));
    api.list('sent', { turnId: source.turnId, signal: controller.signal }).then(value => {
      if (current()) setSentState({ scope, items: conversationShareList(value, 'sent').filter(row => sameShareSource(row.source, source)) });
    }).catch(error => { if (current()) setSentState({ scope, error: error.message }); });
    return () => { controller.abort(); gate.invalidate('sent'); };
  }, [scope, sentEpoch]);
  function send(ordinals) {
    if (locked || !preview?.value || preview.loading || preview.error || !canSend) return;
    commands.start('send', { turn_id: source.turnId, project_id: source.projectId, scope_kind: source.scopeKind,
      source_revision: preview.value.source_revision, to_user_id: selected, include_citations: ordinals });
  }
  return <ConversationSharePreview scope={scope} snapshot={preview?.value?.snapshot} loading={!preview || preview.loading}
    error={preview?.error} onRetry={refreshPreview} onClose={onClose} selectionLocked={locked}
    extra={<>
      {onLocal && <button type="button" className="ui-filter" onClick={onLocal}>本机导出</button>}
      <ConversationShareRecovery state={commands}/>
      <SharedProjectConversationStatus state={projectShare} onNavigate={onNavigate}/>
      {preview?.error && <p role="status">{preview.error}</p>}
      {directory?.error && <p role="alert">{directory.error} · <button type="button" onClick={() => setDirectoryEpoch(value => value + 1)}>重读成员</button></p>}
      <section aria-label="已发送的分享">
        {sent?.loading && <StatusDot state="processing"/>}
        {sent?.error && <p role="alert">{sent.error} · <button type="button" onClick={refreshSent}>重试</button></p>}
        {sent?.items?.map(share => <Row key={share.id} title={share.user_name || share.user_id} meta={sentLabels[share.state]} readOnly
          trailing={share.can_revoke && <button type="button" disabled={locked} onClick={() => commands.start('revoke',
            { expected_revision: share.revision }, share.id)}>撤回</button>}/>)}
      </section>
    </>}
    renderDestinations={({ ordinals, busy }) => <>
      {directory?.loading ? <StatusDot state="processing"/> : directory?.users?.length ? <>
        <label>成员<select aria-label="接收成员" value={selected} disabled={busy || locked}
          onChange={event => setRecipient({ scope, id: event.target.value })}>
          <option value="">选择成员</option>{directory.users.map(row => <option key={row.user_id} value={row.user_id}>{row.name}</option>)}
        </select></label>
        <button type="button" disabled={!canSend || busy || locked} onClick={() => send(ordinals)}>发送</button>
      </> : directory?.users && <span role="status">暂无其他成员</span>}
      <SharedProjectConversationDestination state={projectShare} preview={preview.value} ordinals={ordinals} disabled={busy || projectLocked}/>
    </>}/>
}
