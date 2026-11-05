import { browserStorage, readStoredJson, writeStoredJson } from '../../shared/lib/browserStorage';
import { readSharedCommands, saveSharedCommand } from './sharedProjectCommands';

const identity = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(value);
const encoded = value => JSON.stringify(value);
const linkKey = command => `${command.storageKey}:extraction:${encodeURIComponent(command.id)}:preview`;
const fail = () => { throw new Error('提取操作记录无法核对，请保留浏览器资料'); };

export function extractionCommands(storageKey) {
  return readSharedCommands(storageKey).map(command => {
    const expected = command.expected, body = command.body;
    if (!identity(command.id) || !identity(expected?.source) || !identity(expected?.target)
      || command.storageKey !== storageKey || expected.action !== 'extraction'
      || command.path !== `/${encodeURIComponent(expected.source)}/extractions`
      || Object.keys(body).sort().join(',') !== 'recognition_ids,target_project_id'
      || body.target_project_id !== expected.target
      || body.recognition_ids !== null && (!Array.isArray(body.recognition_ids) || !body.recognition_ids.length
        || new Set(body.recognition_ids).size !== body.recognition_ids.length || body.recognition_ids.some(id => !identity(id)))) fail();
    return command;
  });
}

export function createExtractionCommand(storageKey, source, target, recognitionIds) {
  if (!identity(source) || !identity(target) || recognitionIds !== null && (!Array.isArray(recognitionIds)
    || !recognitionIds.length || new Set(recognitionIds).size !== recognitionIds.length || recognitionIds.some(id => !identity(id)))) fail();
  const command = { version: 1, storageKey, id: `extract-command-${crypto.randomUUID()}`,
    path: `/${encodeURIComponent(source)}/extractions`,
    body: { target_project_id: target, recognition_ids: recognitionIds === null ? null : [...recognitionIds].sort() },
    expected: { action: 'extraction', source, target }, phase: 'prepared', createdAt: new Date().toISOString() };
  saveSharedCommand(command);
  return command;
}

export function extractionLink(command) {
  const result = readStoredJson(linkKey(command), browserStorage());
  if (result.unavailable || result.invalid) fail();
  const value = result.value;
  if (value === null) return null;
  if (value.id !== command.id || !identity(value.preview_id) || value.adoption !== null &&
    (!Number.isSafeInteger(value.adoption.expected_revision) || value.adoption.expected_revision < 1
      || !Array.isArray(value.adoption.confirmed_ids) || !value.adoption.confirmed_ids.length
      || new Set(value.adoption.confirmed_ids).size !== value.adoption.confirmed_ids.length
      || value.adoption.confirmed_ids.some(id => !identity(id)) || value.adoption.batch_size !== 8)) fail();
  return value;
}

export function linkExtractionPreview(command, previewId) {
  if (!identity(previewId)) fail();
  const previous = extractionLink(command);
  if (previous && previous.preview_id !== previewId) fail();
  const next = previous || { id: command.id, preview_id: previewId, adoption: null };
  if (!writeStoredJson(linkKey(command), next)) fail();
  return next;
}

export function confirmExtractionManifest(command, preview) {
  const previous = extractionLink(command);
  if (!previous || previous.preview_id !== preview.id || preview.shared_project_id !== command.expected.source
    || preview.target_project_id !== command.expected.target) fail();
  const body = { expected_revision: preview.revision, confirmed_ids: [...preview.recognition_ids], batch_size: 8 };
  if (previous.adoption && encoded(previous.adoption) !== encoded(body)) fail();
  const next = { ...previous, adoption: body };
  if (!writeStoredJson(linkKey(command), next)) fail();
  return next;
}

export function markExtractionCommand(command, phase) {
  saveSharedCommand({ ...command, phase });
}
