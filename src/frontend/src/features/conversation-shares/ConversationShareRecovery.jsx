export function ConversationShareRecovery({ state }) {
  const { visible, working } = state;
  if (!visible?.command && !visible?.error) return null;
  const retryable = ['not_accepted', 'filing'].includes(visible.phase);
  return <div className="ui-conversation-error" role={visible.error ? 'alert' : 'status'}>
    {visible.command && <span>{working ? '正在核对' : visible.phase === 'filing' ? '归档待完成'
      : visible.phase === 'not_accepted' ? '原请求未受理' : '分享结果待核对'}</span>}
    {visible.error && <span>{visible.error}</span>}
    {(visible.command || !visible.phase) && <button type="button" disabled={working} onClick={state.check}>核对</button>}
    {retryable && <button type="button" disabled={working} onClick={state.retry}>继续原请求</button>}
  </div>;
}
