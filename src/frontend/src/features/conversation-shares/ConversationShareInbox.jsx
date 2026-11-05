import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { deviceIdentity, isDeviceIdentity, userStorageKey } from '../../shared/api/deviceTransport';
import { projectScopeKind, projectScopeLabel } from '../../shared/lib/projectScope';
import { useRequestScope } from '../../shared/lib/useRequestScope';
import { FocusPanel } from '../../shared/ui/FocusPanel';
import { Icon } from '../../shared/ui/Icon';
import { MarkdownBody } from '../../shared/ui/MarkdownBody';
import { Row } from '../../shared/ui/Row';
import { StatusDot } from '../../shared/ui/StatusDot';
import { conversationShareApi as api } from './conversationShareApi';
import { conversationCitation, conversationCitationAllowed, conversationShareList, conversationShareRow, restrictCommandCitations } from './conversationShareViews';
import { ConversationShareRecovery } from './ConversationShareRecovery';
import { useConversationShareCommand } from './useConversationShareCommand';

export function ConversationShareInbox({ projectId, projects, requestOpen, onNavigate, onFiled, panelHost,
  panelAllowed, openingDisabled, onDetailChange }) {
  const actor = deviceIdentity();
  const gate = useRequestScope(`${userStorageKey('conversation-inbox')}:${actor.revision}:${projectId}`), scope = gate.scope;
  const [epoch, setEpoch] = useState(0), [listState, setListState] = useState(null), [detailState, setDetailState] = useState(null);
  const [citationState, setCitationState] = useState(null), [destination, setDestination] = useState(null);
  const [archiveState, setArchiveState] = useState(null);
  const selected = useRef(null), detailRead = useRef(null), citationRead = useRef(null), citationAuthority = useRef(null);
  const list = listState?.scope === scope ? listState : null;
  const detail = detailState?.scope === scope ? detailState : null;
  const citation = citationState?.scope === scope && citationState.shareId === detail?.id
    && conversationCitationAllowed(detail.share, citationState.ordinal) ? citationState : null;
  const archived = archiveState?.scope === scope ? archiveState.share : null;
  const targets = projects.filter(row => projectScopeKind(row) === 'personal');
  const chosen = destination?.scope === scope && destination.id === detail?.id ? destination.project : '';
  const current = token => gate.isCurrent(token) && isDeviceIdentity(actor);
  const refresh = () => setEpoch(value => value + 1);
  const commands = useConversationShareCommand({ enabled: true, onChanged: share => {
    gate.invalidate('list'); refresh();
    if (!share) {
      if (selected.current) void open(selected.current);
      return;
    }
    if (selected.current === share.id) {
      const currentDetail = citationAuthority.current;
      if (currentDetail?.id !== share.id || currentDetail.revision <= share.revision) {
        detailRead.current?.abort(); gate.invalidate('detail');
        acceptDetail(restrictCommandCitations(share, currentDetail));
      }
    }
    if (share.state === 'filed') { setArchiveState({ scope, share }); onFiled?.(); }
    setListState(previous => ({ scope, items: (previous?.scope === scope ? previous.items || [] : [])
      .flatMap(row => row.id !== share.id ? [row] : share.state === 'filing' ? [share] : []) }));
  } });
  const locked = commands.working || Boolean(commands.visible?.command);
  useEffect(() => {
    const token = gate.issue('list', scope), controller = new AbortController();
    if (!token) return;
    setListState(previous => ({ scope, loading: true, items: previous?.scope === scope ? previous.items : undefined }));
    api.list('inbox', { signal: controller.signal }).then(result => {
      if (current(token)) setListState({ scope, items: conversationShareList(result, 'inbox') });
    }).catch(error => { if (current(token)) setListState({ scope, error: error.message }); });
    return () => { controller.abort(); gate.invalidate('list'); };
  }, [scope, epoch]);
  useEffect(() => () => {
    detailRead.current?.abort(); citationRead.current?.controller.abort(); selected.current = null;
    citationAuthority.current = null; onDetailChange(false);
  }, [scope, onDetailChange]);
  function closeCitation() {
    citationRead.current?.controller.abort(); citationRead.current = null;
    gate.invalidate('citation'); setCitationState(null);
  }
  function canReadCitation(id, ordinal) {
    return citationAuthority.current?.id === id && conversationCitationAllowed(citationAuthority.current, ordinal);
  }
  function acceptDetail(share) {
    citationAuthority.current = share;
    const prior = citationRead.current;
    if (prior && !canReadCitation(prior.shareId, prior.ordinal)) closeCitation();
    setDetailState(share.state === 'revoked' ? { scope, id: share.id, error: '对话分享已撤回' } : { scope, id: share.id, share });
  }
  function close() {
    selected.current = null; citationAuthority.current = null;
    detailRead.current?.abort(); gate.invalidate('detail'); closeCitation();
    setDetailState(null); setDestination(null); onDetailChange(false);
  }
  useEffect(() => { if (!panelAllowed && selected.current) close(); }, [panelAllowed]);
  async function open(id) {
    if (!isDeviceIdentity(actor)) return;
    const token = gate.issue('detail', scope);
    if (!token) return;
    citationAuthority.current = null; closeCitation(); detailRead.current?.abort();
    const controller = new AbortController();
    detailRead.current = controller; selected.current = id;
    setDetailState({ scope, id, loading: true }); onDetailChange(true);
    try {
      const value = await api.detail(id, controller.signal);
      if (current(token) && selected.current === id) acceptDetail(conversationShareRow(value, 'inbox', { id, detail: true }));
    } catch (error) {
      if (!current(token) || selected.current !== id) return;
      setDetailState({ scope, id, error: error.message });
      if (error.status === 404) refresh();
    }
  }
  async function openCitation(ordinal) {
    const id = detail?.id;
    if (!id || selected.current !== id || !canReadCitation(id, ordinal) || !isDeviceIdentity(actor)) return;
    const token = gate.issue('citation', scope);
    if (!token) return;
    citationRead.current?.controller.abort();
    const controller = new AbortController(); citationRead.current = { controller, shareId: id, ordinal };
    setCitationState({ scope, shareId: id, ordinal, loading: true });
    try {
      const value = await api.citation(id, ordinal, controller.signal);
      if (current(token) && selected.current === id && canReadCitation(id, ordinal))
        setCitationState({ scope, shareId: id, ordinal, value: conversationCitation(value) });
    } catch (error) {
      if (current(token) && selected.current === id && canReadCitation(id, ordinal))
        setCitationState({ scope, shareId: id, ordinal, error: error.message });
    }
  }
  function openOriginal(share) {
    if (!gate.isScopeCurrent(scope) || !isDeviceIdentity(actor) || share.state !== 'filed') return;
    onNavigate?.('library', { scope_kind: 'personal', layer: 'source', project_id: share.archive.project_id, source_id: share.archive.item_id });
  }
  function openSharedSource(value) {
    if (!gate.isScopeCurrent(scope) || !isDeviceIdentity(actor) || citation?.value !== value || selected.current !== detail?.id
        || !canReadCitation(detail.id, citation.ordinal)) return;
    const field = { source: 'source_id', insight: 'insight_id', note: 'document_id', summary: 'document_id' }[value.layer];
    onNavigate?.('library', { scope_kind: 'shared', project_id: value.project_id, layer: value.layer, [field]: value.id });
  }
  const share = detail?.share;
  return <section aria-label="收到的对话分享">
    <h3>对话分享</h3>
    {!detail && <ConversationShareRecovery state={commands}/>}
    {(!list || list.loading) && <StatusDot state="processing"/>}
    {list?.error && <p role="alert">{list.error} · <button type="button" onClick={refresh}>重试</button></p>}
    {list?.items?.length === 0 && !list.loading && !archived && <p className="library-meta">暂无收到的分享</p>}
    {archived && <p role="status">已归档 {onNavigate && <button type="button" onClick={() => openOriginal(archived)}>打开原件</button>}</p>}
    {list?.items?.map(row => <Row key={row.id} dot={<Icon name="person" size={16}/>} title={row.title}
      sub={`来自 ${row.user_name || row.user_id}`} meta={row.state === 'filing' ? '归档待完成' : undefined}
      selected={detail?.id === row.id} readOnly={openingDisabled} onOpen={() => requestOpen(() => { setDestination(null); void open(row.id); })}/>)}
    {detail && panelAllowed && panelHost && createPortal(<FocusPanel className="library-focus" title="对话分享" onClose={close}>
      {detail.loading ? <StatusDot state="processing"/> : detail.error ? <p role="alert">{detail.error} · <button type="button" onClick={() => open(detail.id)}>重读</button></p> : <>
        <p>来自 {share.user_name || share.user_id}</p>
        <h3>问题</h3><p>{share.snapshot.question}</p>
        <h3>回答</h3><MarkdownBody>{share.snapshot.answer}</MarkdownBody>
        {share.snapshot.citations.map((item, ordinal) => <section key={ordinal} aria-label={item.title}>
          <Row title={item.title} readOnly trailing={conversationCitationAllowed(share, ordinal)
            && <button type="button" onClick={() => openCitation(ordinal)}>读取依据</button>}/>
          <blockquote>{item.quote}</blockquote>
          {citation?.ordinal === ordinal && (citation.loading ? <StatusDot state="processing"/> : citation.error
            ? <p role="status">{citation.error}</p> : <><MarkdownBody>{citation.value.text}</MarkdownBody>
              {onNavigate && <button type="button" onClick={() => openSharedSource(citation.value)}>在共享资料库打开</button>}
              <button type="button" onClick={closeCitation}>收起</button></>)}
        </section>)}
        {share.can_file && <div className="library-inbox-filing">
          <label>归档项目<select aria-label="归档目标个人项目" value={chosen} disabled={locked}
            onChange={event => setDestination({ scope, id: share.id, project: event.target.value })}>
            <option value="">选择个人项目</option>{targets.map(row => <option key={row.id} value={row.id}>{projectScopeLabel(row, targets)}</option>)}
          </select></label>
          <button type="button" disabled={locked || !targets.some(row => row.id === chosen)}
            onClick={() => commands.start('file', { project_id: chosen, expected_revision: share.revision }, share.id)}>归档</button>
        </div>}
        {share.state === 'filing' && !locked && <button type="button" onClick={() => commands.start('file',
          { project_id: share.filing.project_id, expected_revision: share.filing.expected_revision }, share.id, share.filing.command_key)}>核对归档</button>}
        {share.state === 'filed' && <p role="status">已归档 {onNavigate && <button type="button" onClick={() => openOriginal(share)}>打开原件</button>}</p>}
      </>}
      <ConversationShareRecovery state={commands}/>
    </FocusPanel>, panelHost)}
  </section>;
}
