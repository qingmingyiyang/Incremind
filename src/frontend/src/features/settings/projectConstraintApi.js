import { productFetch } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';

export async function previewConstraintActivation({ projectId, activation, signal }) {
  if (typeof projectId !== 'string' || !projectId) throw new Error('constraint_project_unavailable');
  const response = await productFetch(libraryBackendUrl(`/api/v2/projects/${encodeURIComponent(projectId)}/constraints/activation-preview`), {
    method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(activation), signal,
  });
  const value = await readResponseJson(response);
  if (!response.ok) {
    const error = new Error('关键词未添加 · 重试');
    error.code = value.detail; error.limits = value.limits; error.status = response.status;
    throw error;
  }
  return value;
}

export function constraintActivationFailure(error) {
  if (error?.code === 'constraint_keyword_duplicate') return '关键词已存在 · 修改';
  if (error?.code === 'constraint_keywords_too_many' && Number.isInteger(error.limits?.max_keywords))
    return `最多${error.limits.max_keywords}个关键词 · 移除后添加`;
  if (error?.code === 'constraint_keyword_invalid' && Number.isInteger(error.limits?.max_keyword_chars))
    return `关键词限${error.limits.max_keyword_chars}字 · 修改`;
  return '关键词未添加 · 重试';
}
