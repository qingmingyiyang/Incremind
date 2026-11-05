# 设置面板真实集成检查

在仓库的 `src/frontend` 目录执行：

```powershell
npm test -- --config ../../tests/frontend/integration-settings/vitest.config.mjs --maxWorkers=1
```

使用 Windows 项目 `.venv`、真实生产 ASGI 工厂、SQLite 和设置接口。脚本在 `work/S1-live-*` 创建独立资料根，复制公开配置示例，保存专用验证值，禁止外发且不调用模型。仅在自有 Uvicorn 确认绑定 8001 后开始请求，结束后关闭自有服务。8001 已被占用时直接失败。普通前端测试不自动收集 `.real.jsx`。

覆盖单价独立保存、模型与快模型及单价的顺序提交、部分成功后的修正重试、真实第二客户端的价格与快模型 CAS 冲突、冲突后重复保存、已观察父模型变化后的停止，以及首项失败后不提交后续字段。订阅账号在 GET 与 PATCH 之间变化的原子保护受既有接口字段限制，本检查只验证已经观察到的变化。
