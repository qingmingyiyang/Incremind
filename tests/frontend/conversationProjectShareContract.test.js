import { expect, it } from 'vitest';
import { conversationProjectCommand, conversationProjectNotAccepted, conversationProjectPointer, conversationProjectReceipt,
  conversationProjectTarget, conversationSourceRevisionAdvanced, pendingConversationProjectCommand } from '../../src/frontend/src/features/conversation-shares/conversationShareViews';

it('共享项目分享严格区分原轮和写入目标，并按原编号与目标资料位置恢复', () => {
  const source = { turnId: 'turn-one', projectId: 'personal-one', scopeKind: 'personal' };
  const target = { project_id: 'shared-two', scope_kind: 'shared', namespace: 'shared/shared-two/data-one',
    authority: { project_revision: 1, member_revision: 0, account_revision: 1 } };
  expect(conversationProjectTarget(target, 'shared-two')).toBe(target);
  expect(() => conversationProjectTarget({ ...target, namespace: 'shared/shared-other/data-one' }, 'shared-two')).toThrow();
  const body = { turn_id: source.turnId, project_id: source.projectId, scope_kind: source.scopeKind,
    source_revision: { turn_revision: 4, content: { id: 'answer-one', revision: 2 } },
    include_citations: [0, 2], namespace: target.namespace, authority: target.authority };
  const command = { id: 'operation-one', path: '/projects/shared-two', expected: { action: 'share-project', projectId: 'shared-two' }, body };
  expect(conversationProjectCommand(command, source)).toBe(command);
  expect(pendingConversationProjectCommand([command], 'shared-two', target.namespace)).toBe(command);
  expect(pendingConversationProjectCommand([command], 'shared-two', 'shared/shared-two/data-replaced')).toBe(null);
  expect(pendingConversationProjectCommand([command], 'shared-other', target.namespace)).toBe(null);
  expect(conversationSourceRevisionAdvanced(body.source_revision, { ...body.source_revision, turn_revision: 5 })).toBe(true);
  expect(conversationSourceRevisionAdvanced(body.source_revision, { ...body.source_revision, content: { id: 'other-answer', revision: 3 } })).toBe(false);
  expect(() => conversationProjectCommand({ ...command, body: { ...body, snapshot: { question: '保留在服务器的正文' } } }, source)).toThrow();
  expect(conversationProjectNotAccepted({ state: 'not_accepted' })).toBe(true);
  expect(conversationProjectNotAccepted({ state: 'not_accepted', command_id: command.id })).toBe(false);
  const pointer = conversationProjectPointer({ id: command.id, projectId: target.project_id, namespace: target.namespace });
  const accepted = { command_id: command.id, state: 'accepted', project_id: target.project_id,
    scope_kind: 'shared', namespace: target.namespace, kind: 'conversation', item: { id: 'source-one', input_kind: 'conversation' } };
  expect(conversationProjectReceipt(accepted, pointer)).toEqual({ projectId: 'shared-two', namespace: target.namespace, itemId: 'source-one' });
  expect(() => conversationProjectReceipt({ ...accepted, namespace: 'shared/shared-two/data-replaced' }, pointer)).toThrow();
});
