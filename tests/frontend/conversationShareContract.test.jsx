import { fireEvent, render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { ConversationSharePreview } from '../../src/frontend/src/shared/ui/ConversationSharePreview';
import { conversationCitation, conversationCitationAllowed, conversationCommandShare, conversationSharePreview, restrictCommandCitations, sameConversationPreview, validateConversationCommand } from '../../src/frontend/src/features/conversation-shares/conversationShareViews';

it('保留快照原引用序号并只核对原归档命令的目标', () => {
  const preview = conversationSharePreview({ snapshot: { question: '问题', answer: '回答',
    citations: ['甲', '乙', '丙'].map(title => ({ title, quote: `${title}的片段` })) },
    source_revision: { turn_revision: 2, content: { id: 'turn-one', revision: 2 } } });
  render(<ConversationSharePreview scope="one" snapshot={preview.snapshot}
    renderDestinations={({ ordinals }) => <output aria-label="引用原序号">{ordinals.join(',')}</output>}/>);
  fireEvent.click(screen.getByRole('button', { name: '移除引用 乙' }));
  expect(screen.getByLabelText('引用原序号').textContent).toBe('0,2');
  expect(sameConversationPreview(preview, structuredClone(preview))).toBe(true);
  expect(sameConversationPreview(preview, { ...preview, source_revision: { ...preview.source_revision, turn_revision: 3 } })).toBe(false);
  const source = { turnId: 'turn-one', projectId: 'personal-one', scopeKind: 'personal' };
  const command = { id: 'operation-one', path: '', expected: { action: 'send' },
    body: { turn_id: source.turnId, project_id: source.projectId, scope_kind: source.scopeKind,
      to_user_id: 'recipient-one', source_revision: preview.source_revision, include_citations: [0, 2] } };
  expect(validateConversationCommand(command, source)).toBe(command);
  expect(() => validateConversationCommand({ ...command, body: { ...command.body, snapshot: preview.snapshot } }, source)).toThrow();
  expect(() => validateConversationCommand({ ...command, body: { ...command.body,
    source_revision: { ...preview.source_revision, content: { ...preview.source_revision.content, markdown: '正文' } } } }, source)).toThrow();
  const file = { id: 'file-one', path: '/share-one/file', expected: { action: 'file', shareId: 'share-one' },
    body: { project_id: 'personal-one', expected_revision: 1 } };
  const filing = { id: 'share-one', revision: 2, state: 'filing', direction: 'inbox', title: '问题',
    user_id: 'sender-one', user_name: '发送者', created_at: '2026-11-05T00:00:00Z', can_file: false, can_revoke: false,
    snapshot: preview.snapshot, citation_details: [], filing: { command_key: file.id, project_id: 'personal-one', expected_revision: 1 } };
  expect(conversationCommandShare(filing, file)).toBe(filing);
  expect(() => conversationCommandShare({ ...filing, filing: { ...filing.filing, project_id: 'other-project' } }, file)).toThrow();
  const citation = { scope_kind: 'shared', project_id: 'shared-project-one',
    namespace: 'shared/shared-project-one/shared-data-one', layer: 'source', id: 'item-one', title: '原件', text: '原文' };
  expect(conversationCitation(citation)).toBe(citation);
  expect(() => conversationCitation({ ...citation, namespace: 'shared/shared-project-two/shared-data-one' })).toThrow();
  expect(() => conversationCitation({ ...citation, namespace: 'shared/shared-project-one/shared-data-one/extra' })).toThrow();
  expect(conversationCitationAllowed({ ...filing, citation_details: [1] }, 1)).toBe(true);
  expect(conversationCitationAllowed(filing, 1)).toBe(false);
  const olderCommand = { ...filing, citation_details: [0, 1] };
  const newerDetail = { ...filing, citation_details: [0] };
  expect(restrictCommandCitations(olderCommand, newerDetail).citation_details).toEqual([0]);
  expect(restrictCommandCitations(olderCommand, null).citation_details).toEqual([]);
});
