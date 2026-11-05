const labels = {
  missing_assets: '缺少文件', invalid_assets: '文件无效', missing_dependencies: '缺少依赖',
  available: '待加载', loading: '加载中', loaded: '已加载', load_failed: '加载失败',
  inference_failed: '生成失败', restart_required: '需要重启',
  runtime_unavailable: '运行环境不可用',
};
const errors = {
  local_model_config_invalid: '模型配置无效', local_model_tokenizer_invalid: '分词器文件或对话模板无效',
  local_model_weights_invalid: '权重文件或分片索引无效', local_model_weights_incomplete: '权重不完整',
  local_model_assets_changed_restart_required: '模型文件已变化，请重启应用后加载',
  local_model_load_failed: '实际加载未完成，点击测试重试',
  local_model_inference_failed: '实际生成未完成，点击测试重试',
  local_model_runtime_unavailable: '本机生成依赖无法初始化，请检查运行环境',
  local_model_assets_changed: '模型文件正在变化，完成后刷新设置',
};

export function localGenerationStatus(readiness) {
  if (!readiness) return { label: '状态待读取', detail: '刷新设置后读取本机资源状态', selectable: false };
  const missing = readiness.missing_files || [], invalid = readiness.invalid_files || [];
  const dependencies = readiness.missing_dependencies || [];
  const detail = [missing.length ? `缺少 ${missing.join('、')}` : '',
    invalid.length ? errors[readiness.reason_code] || `检查 ${invalid.join('、')}` : '',
    dependencies.length ? `缺少依赖 ${dependencies.join('、')}` : ''].filter(Boolean).join(' · ')
    || errors[readiness.reason_code]
    || (readiness.status === 'available' ? '文件和依赖已具备，首次使用时加载' : '');
  return { label: labels[readiness.status] || '状态待读取', detail,
    selectable: readiness.eligible === true && readiness.status !== 'restart_required',
    retry: ['load_failed', 'inference_failed'].includes(readiness.status) };
}
