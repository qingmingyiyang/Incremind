export const REMOTE_PROCESSING_HINT = '开启后按本用途自动处理普通资料，无需逐次确认。私密资料及其派生内容仍不外发；关闭后相关云端功能受限，本机保存和查阅仍可用。';
export const PRIVATE_MATERIAL_HINT = '私密资料及其派生内容不交给远程模型。本机保存和查阅仍可用，云端整理和问答会受限。';
export const LOCAL_GENERATION_HINT = '本机生成仅负责生成；转写、识图和向量等用途按各自配置运行。';
export const LOCAL_VECTOR_HINT = '本机向量只负责检索，整理和回答仍按生成模型配置运行。';

const restrictions = {
  private_project_remote_blocked: { reason: '项目为私密', section: 'privacy', title: PRIVATE_MATERIAL_HINT },
  remote_disabled: { reason: '模型外发已关闭', section: 'model', title: REMOTE_PROCESSING_HINT },
};

export function remoteRestriction(error) {
  const code = typeof error === 'string' ? error : error?.code;
  const restriction = Object.hasOwn(restrictions, code) ? restrictions[code] : null;
  return restriction ? { ...restriction, code, message: `${restriction.reason} · 查看设置` } : null;
}
