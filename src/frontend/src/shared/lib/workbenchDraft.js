import { useCallback, useMemo, useRef, useState } from 'react';
import { userStorageKey } from '../api/deviceTransport';

const DRAFT_KEY = 'chriptmas-workbench-draft';
const intents = [null, 'remember', 'ask', 'do', 'inspiration'];

function textDraft(value, projectId) {
  if (!value || typeof value.text !== 'string') return undefined;
  return {
    projectId, text: value.text,
    selection: intents.includes(value.selection) ? value.selection : null,
    locked: value.locked === true,
    scopeTag: typeof value.scopeTag?.label === 'string' && typeof value.scopeTag?.tag === 'string'
      ? { label: value.scopeTag.label, tag: value.scopeTag.tag } : null,
  };
}

function readDraft(key, projectId) {
  try {
    const value = JSON.parse(globalThis.localStorage?.getItem(key) || 'null');
    return value?.projectId === projectId ? textDraft(value, projectId) : undefined;
  } catch { return undefined; }
}

export function useWorkbenchDraft(projectId, userEpoch = 0, scopeKind = 'personal') {
  const space = userStorageKey(DRAFT_KEY);
  const key = `${space}:${scopeKind === 'personal' ? '' : `${encodeURIComponent(scopeKind)}:`}${encodeURIComponent(projectId)}`;
  const epoch = useRef(userEpoch);
  epoch.current = userEpoch;
  const session = useMemo(() => ({ last: readDraft(key, projectId) }), [key, projectId, userEpoch]);
  const activeSession = useRef(session);
  activeSession.current = session;
  const [stored, setStored] = useState(() => ({ key, value: readDraft(key, projectId) }));
  let current = stored;
  if (stored.key !== key) {
    current = { key, value: readDraft(key, projectId) };
    setStored(current);
  }
  const saveDraft = useCallback(value => {
    // 空间切换后忽略旧页面回调，项目间跳转仍结算原项目已接受的输入。
    if (epoch.current !== userEpoch || userStorageKey(DRAFT_KEY) !== space) return;
    // 离开的输入框只能结算自己保存的文字，后续页面的新编辑优先。
    if (activeSession.current !== session
      && JSON.stringify(readDraft(key, projectId)) !== JSON.stringify(session.last)) return;
    let next = textDraft(value, projectId);
    if (!next?.text && !next?.selection && !next?.scopeTag) next = undefined;
    try {
      if (next) globalThis.localStorage?.setItem(key, JSON.stringify(next));
      else globalThis.localStorage?.removeItem(key);
    } catch { /* 浏览器存储不可用时，当前输入框继续保留文字。 */ }
    session.last = next;
    setStored(previous => previous.key === key ? { key, value: next } : previous);
  }, [key, projectId, space, userEpoch, session]);
  return { draft: current.value, saveDraft, draftKey: `${key}:${userEpoch}` };
}
