export const EXTERNAL_CLIENTS = [
  { id: 'claude-code', client: 'claude', label: 'Claude Code', mark: 'CC', credential: 'ANTHROPIC_API_KEY', snapshot: true },
  { id: 'codex', client: 'codex', label: 'Codex', mark: 'CX', credential: 'CODEX_API_KEY', snapshot: true },
  { id: 'deepseek-harness', client: 'deepseek-harness', label: 'DeepSeek Harness', mark: 'DH', credential: null, snapshot: false },
];

const PYTHON_PLACEHOLDER = '<本机Python绝对路径>', ADAPTER_PLACEHOLDER = '<本机mcp.py绝对路径>';
const absolutePath = value => typeof value === 'string' && /^(?:[A-Za-z]:[\\/]|\/)/.test(value);
const quote = (value, shell) => shell === 'powershell' ? `'${value.replaceAll("'", "''")}'` : `'${value.replaceAll("'", "'\"'\"'")}'`;

export function proxyClientConfiguration(data, client, shell = 'powershell') {
  if (!data?.available) return { available: false, reason: data?.reason || 'proxy_connection_pending' };
  if (!['claude', 'codex'].includes(client) || !['powershell', 'sh'].includes(shell)) throw new Error('proxy_configuration_invalid');
  let backend;
  try { backend = new URL(data.backend); }
  catch { return { available: false, reason: 'proxy_backend_invalid' }; }
  if (backend.username || backend.password || backend.search || backend.hash || backend.pathname !== '/'
      || typeof data.remote !== 'boolean'
      || !(backend.protocol === 'https:' || backend.protocol === 'http:' && ['127.0.0.1', '[::1]'].includes(backend.hostname))) return { available: false, reason: 'proxy_backend_invalid' };
  if (data.remote && backend.protocol !== 'https:') return { available: false, reason: 'proxy_https_required' };
  if (!data.remote && !(backend.protocol === 'http:' && ['127.0.0.1', '[::1]'].includes(backend.hostname))) return { available: false, reason: 'proxy_backend_invalid' };
  let authentication = null;
  if (data.remote) {
    try { authentication = validateAuthentication(data.client_setup?.authentication); }
    catch { return { available: false, reason: 'proxy_authentication_unavailable' }; }
  }
  const target = authentication?.target_user_id;
  const base = `${backend.origin}/api/v2/external-agent/proxy/${client}${client === 'codex' ? '/v1' : ''}`;
  let text;
  if (client === 'codex') {
    text = `model_provider = "chriptmas_proxy"\n\n[model_providers.chriptmas_proxy]\nname = "Incremind proxy"\nbase_url = ${JSON.stringify(base)}\nwire_api = "responses"\nenv_key = "CODEX_API_KEY"\nrequires_openai_auth = false\nsupports_websockets = false\nrequest_max_retries = 0\nstream_max_retries = 0\n`;
    if (data.remote) {
      text += '\n[model_providers.chriptmas_proxy.env_http_headers]\n"X-Chriptmas-Device-Key" = "CHRIPTMAS_DEVICE_KEY"\n';
      if (target) text += `\n[model_providers.chriptmas_proxy.http_headers]\n"X-Chriptmas-Target-User" = ${JSON.stringify(target)}\n`;
    }
  } else if (shell === 'powershell') {
    text = `if (-not $env:ANTHROPIC_API_KEY) { throw '请先设置 ANTHROPIC_API_KEY' }\n$env:ANTHROPIC_BASE_URL = ${quote(base, shell)}\n`;
    if (data.remote) text += `if (-not $env:CHRIPTMAS_DEVICE_KEY) { throw '请先设置 CHRIPTMAS_DEVICE_KEY' }\n$env:ANTHROPIC_CUSTOM_HEADERS = "X-Chriptmas-Device-Key: $env:CHRIPTMAS_DEVICE_KEY${target ? '`nX-Chriptmas-Target-User: ' + target : ''}"\n`;
  } else {
    text = `: "\${ANTHROPIC_API_KEY:?请先设置 ANTHROPIC_API_KEY}"\nexport ANTHROPIC_BASE_URL=${quote(base, shell)}\n`;
    if (data.remote) text += `export ANTHROPIC_CUSTOM_HEADERS="X-Chriptmas-Device-Key: \${CHRIPTMAS_DEVICE_KEY:?请先设置 CHRIPTMAS_DEVICE_KEY}${target ? '\nX-Chriptmas-Target-User: ' + target : ''}"\n`;
  }
  return { available: true, base_url: base, text, remote: data.remote, target_user_id: target || null };
}

export function proxyConfigurationMessage(configuration) {
  return ({ proxy_https_required: '服务器代理需要 HTTPS 地址，请使用 HTTPS 连接后刷新。',
    mcp_connection_https_required: '服务器代理需要 HTTPS 地址，请使用 HTTPS 连接后刷新。',
    proxy_authentication_unavailable: '服务器未提供设备鉴权配置，请更新服务器后刷新。',
    proxy_backend_invalid: '服务器地址格式不可用，请核对连接地址后刷新。',
    proxy_connection_pending: '正在读取实际接入地址。' })[configuration.reason] || '接入信息暂不可用，请刷新后重试。';
}

function validateAuthentication(authentication) {
  const harness = authentication?.deepseek_harness;
  if (authentication?.device_key_environment !== 'CHRIPTMAS_DEVICE_KEY'
      || authentication.target_user_environment !== 'CHRIPTMAS_MCP_TARGET_USER'
      || authentication.target_user_id !== null && (typeof authentication.target_user_id !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._~-]{0,127}$/.test(authentication.target_user_id))
      || harness?.merge_into !== 'memory-mcp.config.env' || harness.format !== 'yaml'
      || harness.device_key_reference !== 'CHRIPTMAS_DEVICE_KEY: !!js process.env.CHRIPTMAS_DEVICE_KEY\n'
      || harness.target_user_reference !== 'CHRIPTMAS_MCP_TARGET_USER: !!js process.env.CHRIPTMAS_MCP_TARGET_USER\n') throw new Error('connection_authentication_invalid');
  return authentication;
}

function validateClientSetup(setup) {
  if (setup?.requires_local_paths !== true || setup.python_placeholder !== PYTHON_PLACEHOLDER
      || setup.adapter_placeholder !== ADAPTER_PLACEHOLDER || !Array.isArray(setup.shells)
      || setup.shells.length !== 2 || !['powershell', 'sh'].every(shell => setup.shells.includes(shell))) throw new Error('connection_setup_invalid');
  for (const shell of ['powershell', 'sh']) for (const client of ['claude', 'codex']) {
    const template = setup.command_templates?.[shell]?.[client];
    const suffix = ` -- ${quote(PYTHON_PLACEHOLDER, shell)} -I ${quote(ADAPTER_PLACEHOLDER, shell)}`;
    const prefix = typeof template === 'string' ? template.slice(0, -suffix.length) : '';
    if (typeof template !== 'string' || !template.endsWith(suffix)
        || !template.startsWith(`${client} mcp add `) || prefix.includes(PYTHON_PLACEHOLDER)
        || prefix.includes(ADAPTER_PLACEHOLDER)) throw new Error('connection_setup_invalid');
  }
  if (setup.authentication) validateAuthentication(setup.authentication);
  return setup;
}

export function readExternalConnection(data, projectId, client) {
  if (data?.available === false && typeof data.reason === 'string') return data;
  if (data?.available !== true || data.project_id !== projectId || data.version !== 'mcp-connection@1'
      || typeof data.remote !== 'boolean' || !['powershell', 'sh'].includes(data.shell)
      || typeof data.instructions !== 'string' || !data.instructions
      || !['claude', 'codex'].every(key => typeof data.commands?.[key] === 'string' && data.commands[key])) throw new Error('connection_invalid');
  const setup = data.remote && data.client_setup ? validateClientSetup(data.client_setup) : null;
  if (client === 'deepseek-harness') {
    const profile = data.profiles?.['deepseek-harness'];
    if (profile?.version !== 'deepseek-memory-profile@1' || profile.format !== 'json'
        || typeof profile.text !== 'string' || !profile.text) throw new Error('connection_invalid');
    const parsed = JSON.parse(profile.text), entry = parsed?.[0]?.insert?.[0], config = entry?.config;
    if (!Array.isArray(parsed) || parsed.length !== 1 || Object.keys(parsed[0] || {}).length !== 1
        || !Array.isArray(parsed[0].insert) || parsed[0].insert.length !== 1
        || entry.id !== 'memory-mcp' || entry.name !== '@deepseek-ai/dsh-mcp-client'
        || config?.serverName !== 'chriptmas-memory' || config.transport !== 'stdio'
        || !(setup ? config.command === PYTHON_PLACEHOLDER : absolutePath(config.command)) || !Array.isArray(config.args) || config.args.length !== 2
        || config.args[0] !== '-I' || !(setup ? config.args[1] === ADAPTER_PLACEHOLDER : absolutePath(config.args[1]) && /[\\/]mcp\.py$/i.test(config.args[1]))
        || !config.env || Object.keys(config.env).length !== 5
        || !['DEEPSEEK_API_KEY', 'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'CODEX_API_KEY'].every(key => config.env[key] === '')
        || config.failOnStartupError !== true || config.reconnect?.enabled !== false) throw new Error('connection_invalid');
    const backend = new URL(config.env.CHRIPTMAS_MCP_BACKEND_URL);
    if (backend.username || backend.password || backend.search || backend.hash || backend.pathname !== '/'
        || !(backend.protocol === 'https:' || backend.protocol === 'http:' && ['127.0.0.1', 'localhost', '[::1]'].includes(backend.hostname))) throw new Error('connection_invalid');
  }
  return data;
}

export function clientConnectionText(data, client, paths) {
  if (!data?.available) return null;
  if (!data.remote) return client === 'deepseek-harness' ? data.profiles[client].text : data.commands[client];
  if (!data.client_setup) return null;
  const setup = validateClientSetup(data.client_setup);
  if (!paths.python || !paths.adapter) return null;
  if (!['powershell', 'sh'].includes(paths.shell) || !absolutePath(paths.python) || !absolutePath(paths.adapter)
      || !/[\\/]mcp\.py$/i.test(paths.adapter)
      || [paths.python, paths.adapter].some(value => value.length > 4096 || /[\r\n\0]/.test(value)
        || value.includes(PYTHON_PLACEHOLDER) || value.includes(ADAPTER_PLACEHOLDER))) throw new Error('local_paths_invalid');
  if (client === 'deepseek-harness') {
    const profile = JSON.parse(data.profiles[client].text), config = profile[0].insert[0].config;
    if (config.command !== PYTHON_PLACEHOLDER || config.args[0] !== '-I' || config.args[1] !== ADAPTER_PLACEHOLDER) throw new Error('connection_setup_invalid');
    config.command = paths.python; config.args[1] = paths.adapter;
    return JSON.stringify(profile, null, 2);
  }
  const template = setup.command_templates[paths.shell][client];
  const suffix = ` -- ${quote(PYTHON_PLACEHOLDER, paths.shell)} -I ${quote(ADAPTER_PLACEHOLDER, paths.shell)}`;
  // 只替换后端模板末尾的两个完整参数；本机路径按所选终端逐项引用。
  return `${template.slice(0, -suffix.length)} -- ${quote(paths.python, paths.shell)} -I ${quote(paths.adapter, paths.shell)}`;
}

export function harnessAuthenticationText(data, includeTarget = false, selectedUserId = null) {
  if (!data?.remote || !data.client_setup?.authentication) return null;
  const authentication = validateAuthentication(data.client_setup.authentication);
  if (includeTarget && (!selectedUserId || authentication.target_user_id !== selectedUserId)) throw new Error('connection_target_changed');
  return authentication.deepseek_harness.device_key_reference + (includeTarget ? authentication.deepseek_harness.target_user_reference : '');
}

export function externalReadiness(plugin, allowRemote) {
  if (!plugin) return { memory: '读取中', task: '读取中', missing: [], action: null };
  const memory = !plugin.capabilities.memory ? '暂不可用' : !plugin.enabled ? '客户端停用' : !allowRemote ? '外发关闭' : '可配置 MCP';
  const missing = [];
  if (!plugin.installed) missing.push('应用执行器未发现');
  else if (!plugin.runtime_version) missing.push('执行器版本待核');
  if (!plugin.has_key) missing.push('任务凭据未配置');
  if (plugin.reason === 'external_host_isolation_unavailable') missing.push('执行隔离暂不可用');
  if (plugin.reason === 'external_executor_version_unsupported') missing.push('执行器版本不支持');
  if (!plugin.enabled) missing.push('客户端未启用');
  if (!allowRemote) missing.push('外部外发未开启');
  const reasonLabel = { external_host_isolation_unavailable: '执行隔离暂不可用', external_executor_not_installed: '应用执行器未发现',
    external_executor_version_unverified: '执行器版本待核', external_executor_version_unsupported: '执行器版本不支持',
    external_executor_credentials_unavailable: '任务凭据未配置', external_agent_disabled: !allowRemote ? '外发关闭' : '客户端停用' }[plugin.reason];
  const task = plugin.available && plugin.capabilities.task ? '可派发' : reasonLabel || missing[0] || '暂不可用';
  const action = plugin.reason === 'external_agent_disabled' && !allowRemote ? { label: '开启外发', kind: 'egress' }
    : plugin.reason === 'external_agent_disabled' && !plugin.enabled ? { label: '启用', kind: 'enable' }
      : plugin.reason === 'external_executor_credentials_unavailable' && plugin.id === 'deepseek-harness' ? { label: '配置模型', kind: 'generation' }
        : { label: plugin.reason === 'external_executor_credentials_unavailable' ? '配置凭据' : plugin.reason?.includes('version') ? '查看版本' : '查看条件', kind: 'conditions' };
  return { memory, task, missing, action };
}
