# 账号界面真实集成检查

先在独立 `work` 资料根启动完整服务器 ASGI，监听 `127.0.0.1:8001`。私有凭据 JSON 由服务器验收准备步骤生成，包含管理员的 `device_key` 和 `device_id`，禁止提交或输出其内容。

在 `src/frontend` 执行：

```powershell
$env:CHRIPTMAS_LOGIN_QA_CREDENTIALS = '独立验收凭据文件的绝对路径'
npm test -- --config ../../tests/frontend/integration-login/vitest.config.mjs
```

检查使用真实 React 管理表单、loopback HTTP、生产 ASGI 和 SQLite，创建测试账号、设置新密码，确认旧密码会话失效、普通账号无法新增用户，以及退出后设备失效。只将相对请求路由到实际服务，不替代响应或业务对象。测试不启动服务器，不调用模型，不自动删除账号或资料。

该检查会在指定隔离服务新增测试账号和设备记录，禁止用于正式服务。浏览器实际登录、配对、分享目标和两尺寸两主题检查另行执行。
