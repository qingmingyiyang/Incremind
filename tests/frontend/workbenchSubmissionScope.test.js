import { expect, it } from 'vitest';
import { userStorageKey } from '../../src/frontend/src/shared/api/deviceTransport';
import { workbenchSubmission } from '../../src/frontend/src/features/workbench/workbenchApi';

it('同编号项目交替共享和个人请求后仍恢复各自原提交编号', () => {
  const sharedBody = { project_id: 'same-project', scope_kind: 'shared', text: '合成问题', intent: 'ask', executor: 'builtin' };
  const personalBody = { project_id: 'same-project', text: '合成问题', intent: 'ask' };
  const shared = workbenchSubmission(sharedBody), personal = workbenchSubmission(personalBody);
  const sharedSlot = userStorageKey('chriptmas-v2-request:shared:same-project');
  const personalSlot = userStorageKey('chriptmas-v2-request:same-project');
  try {
    expect(personal.id).not.toBe(shared.id);
    expect(workbenchSubmission(sharedBody).id).toBe(shared.id);
    expect(workbenchSubmission(personalBody).id).toBe(personal.id);
    expect(JSON.parse(sessionStorage.getItem(sharedSlot)).id).toBe(shared.id);
    expect(JSON.parse(sessionStorage.getItem(personalSlot)).id).toBe(personal.id);
    personal.clear();
    expect(sessionStorage.getItem(personalSlot)).toBeNull();
    expect(workbenchSubmission(sharedBody).id).toBe(shared.id);
    expect(JSON.parse(sessionStorage.getItem(sharedSlot)).id).toBe(shared.id);
  } finally { shared.clear(); personal.clear(); }
});
