import { productFetch } from '../../shared/api/deviceTransport';
import { readResponseJson } from '../../shared/lib/responseJson';
import { libraryBackendUrl } from '../rebuild/libraryOverviewTransport';

async function request(path, body, signal) {
  const response = await productFetch(libraryBackendUrl(`/api/v2/workbench/${path}`), {
    method: body === undefined ? 'GET' : 'POST', cache: 'no-store', signal,
    headers: { 'Content-Type': 'application/json' }, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const result = await readResponseJson(response);
  if (!response.ok) {
    const reasons = { route_correction_shared_owner_unavailable: '共享范围暂不可纠正 · 保留修改',
      route_correction_remember_owner_unavailable: '当前暂不可记住 · 保留修改',
      route_correction_source_unfinished: '处理尚未结束 · 稍后核对', route_correction_already_replaced: '已替换 · 打开替换结果纠正',
      route_correction_operation_exists: '已有纠正记录 · 读取结果', route_original_birth_changed: '原件来源无法核对',
      record_lineage_invalid: '来源连续性无法核对 · 保留修改' };
    Object.assign(reasons, { route_correction_missing_child_requires_review: '本部分尚无结果 · 原执行仍需核对',
      route_correction_missing_file_requires_review: '附件处理尚需核对 · 保留原件',
      route_correction_missing_do_requires_review: '任务执行尚需核对 · 保留原操作',
      route_correction_execution_closed: '原执行已关闭 · 读取新操作' });
    const error = new Error(reasons[result?.detail] || (response.status === 409 ? '原结果已变化 · 重新核对' : '操作未完成 · 保留修改'));
    error.status = response.status; throw error;
  }
  return result;
}
const part = encodeURIComponent;
export const routeCorrectionApi = {
  read: (id, project, signal) => request(`turns/${part(id)}/route-correction?project_id=${part(project)}`, undefined, signal),
  submit: (id, body) => request(`turns/${part(id)}/route-correction`, body),
  operation: id => request(`route-corrections/operations/${part(id)}`),
  cancel: id => request(`route-corrections/operations/${part(id)}/cancel`, {}),
  removeExample: (id, body) => request(`turns/${part(id)}/route-example/delete`, body),
};
