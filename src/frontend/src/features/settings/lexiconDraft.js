const version = (term, revision) => ({ id: term.id, term: term.term, weight: term.weight, status: term.status, revision });
const sameTerm = (left, right) => ['id', 'term', 'weight', 'status'].every(key => left[key] === right[key]);

export function beginLexiconDraft(term, revision) {
  const current = version(term, revision);
  return { base: current, current, name: term.term, weight: term.weight, dirty: false, stale: false, conflict: false };
}

export function editLexiconDraft(draft, changes) {
  const value = { ...draft, ...changes };
  return { ...value, dirty: value.name !== value.base.term || value.weight !== value.base.weight };
}

export function reconcileLexiconDraft(draft, term, revision) {
  const current = version(term, revision);
  if (draft.current.id !== term.id) return beginLexiconDraft(term, revision);
  if (draft.current.revision === revision && sameTerm(draft.current, current)) return draft;
  if (!draft.dirty && !draft.stale) return beginLexiconDraft(term, revision);
  return { ...draft, current, stale: true, conflict: !sameTerm(draft.base, current) };
}

export function reviewLexiconDraft(draft, choice) {
  if (choice === 'current') return beginLexiconDraft(draft.current, draft.current.revision);
  if (choice !== 'keep') throw new Error('lexicon_draft_choice_invalid');
  return { ...draft, base: draft.current, stale: false, conflict: false,
    dirty: draft.name !== draft.current.term || draft.weight !== draft.current.weight };
}

export function lexiconDraftOperations(draft, action) {
  if (draft.stale) throw new Error('lexicon_draft_requires_review');
  if (action === 'toggle') return [{ action: draft.base.status === 'active' ? 'retire' : 'restore', term: draft.base.term }];
  const name = draft.name.trim();
  if (!name || draft.base.status !== 'active') throw new Error('lexicon_draft_term_unavailable');
  return [{ action: name !== draft.base.term ? 'rename' : 'set_weight', term: draft.base.term,
    ...(name !== draft.base.term ? { new_term: name } : {}), weight: draft.weight }];
}
