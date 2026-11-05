import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { beginCredentialDraft, completeCredentialDraft, editCredentialDraft, receiveCredentialDraft, reviewCredentialDraft, submitCredentialDraft } from '../../src/frontend/src/features/settings/credentialDraft.js';

const input = JSON.parse(await readFile(process.argv[2], 'utf8'));
const scope = `synthetic-user-a:${input.kind}:${input.target}`;
const identity = value => input.kind === 'cookie' ? value.revision : JSON.stringify(value);
const defaults = value => input.kind === 'cookie' ? { content: '' } : { baseUrl: value.base_url || '', model: value.model || '', key: '' };
const secret = input.kind === 'cookie' ? 'content' : 'key';
let draft = beginCredentialDraft(scope, identity(input.before), defaults(input.before));
for (const [name, value] of Object.entries(input.fields)) draft = editCredentialDraft(draft, name, value);
const first = submitCredentialDraft(draft);
assert.deepEqual(input.failures, [400, 409]);
assert.equal(draft.fields[secret], input.fields[secret]);
draft = receiveCredentialDraft(draft, scope, identity(input.after), defaults(input.after));
assert.equal(draft.fields[secret], input.fields[secret]);
assert.equal(draft.stale, true);
assert.throws(() => submitCredentialDraft(draft), error => error.status === 409);
draft = reviewCredentialDraft(draft);
const submitted = submitCredentialDraft(draft);
assert.deepEqual(submitted.fields, input.fields);
assert.equal(submitted.identity, identity(input.after));
assert.equal(first.identity, identity(input.before));
if (input.kind === 'cookie') {
  const file = new File([input.fields.content], 'cookies.txt', { type: 'text/plain' });
  assert.equal(await file.text(), draft.fields.content);
}
if (input.saved) {
  const completed = completeCredentialDraft(draft, submitted, identity(input.saved), defaults(input.saved));
  assert.equal(completed.fields[secret], '');
  assert.equal(completed.stale, false);
  const later = editCredentialDraft(draft, secret, 'synthetic-later-input');
  const retained = completeCredentialDraft(later, submitted, identity(input.saved), defaults(input.saved));
  assert.equal(retained.fields[secret], 'synthetic-later-input');
  assert.equal(retained.stale, true);
  const switched = receiveCredentialDraft(draft, 'synthetic-user-b', identity(input.saved), defaults(input.saved));
  assert.equal(switched.fields[secret], '');
  assert.equal(completeCredentialDraft(switched, submitted, identity(input.saved), defaults(input.saved)), switched);
}
process.stdout.write(JSON.stringify({ fields: submitted.fields, expected_revision: input.after.revision,
  checks: input.saved ? 10 : 5, scope, target: input.target }));
