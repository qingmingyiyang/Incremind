import { expect, it } from 'vitest';
import { workbenchErrorMessage } from '@src/features/workbench/workbenchApi';

const cases = [
  ['server_model_balance_insufficient', '余额不足 · 联系管理员'],
  ['server_model_balance_account_blocked', '消费待核算 · 联系管理员'],
  ['server_model_balance_bound_unavailable', '该模型不支持共享计费 · 联系管理员'],
  ['server_model_balance_price_unavailable', '模型价格未配置 · 联系管理员'],
  ['server_model_balance_model_forbidden', '模型未获授权 · 联系管理员'],
  ['server_model_binding_changed', '模型分配已变化 · 重新发送'],
  ['server_model_purpose_unavailable', '尚未分配模型 · 联系管理员'],
  ['server_model_profile_disabled', '模型已停用 · 联系管理员'],
  ['server_model_credential_unavailable', '模型密钥不可用 · 联系管理员'],
  ['server_model_secret_unavailable', '模型密钥不可用 · 联系管理员'],
  ['server_model_not_allowed', '模型未获授权 · 联系管理员'],
  ['server_model_balance_job_unauthorized', '调用权限不可用 · 联系管理员'],
  ['server_model_billing_owner_unavailable', '计费状态不可用 · 联系管理员'],
  ['server_model_billing_unavailable', '计费状态不可用 · 联系管理员'],
];

it.each(cases)('uses the finite message for %s without provider text', (code, message) => {
  expect(workbenchErrorMessage(code)).toBe(message);
  expect(workbenchErrorMessage({ code, message: 'PRIVATE_PROVIDER_TEXT' })).toBe(message);
});

it.each(['server_model_unrecognized', 'server_model_billing_binding_invalid',
  'server_model_billing_wire_not_reserved', 'server_model_billing_wire_already_invoked'])(
  'keeps %s private', code => {
  expect(workbenchErrorMessage(code)).toBe('操作未完成 · 重试');
  expect(workbenchErrorMessage({ code, message: 'PRIVATE_PROVIDER_TEXT' },
    '未完成')).toBe('未完成');
});

it('retains source, privacy and ordinary failures', () => {
  expect(workbenchErrorMessage('source_changed_retry')).toBe('资料已变化 · 重新发送');
  expect(workbenchErrorMessage('remote_disabled')).toBe('模型外发已关闭 · 查看设置');
  expect(workbenchErrorMessage('private_project_remote_blocked')).toBe('项目为私密 · 查看设置');
  expect(workbenchErrorMessage('answer_generation_failed')).toBe('回答未完成 · 重试');
});
