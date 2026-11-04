# grok-register —— 上游参考来源（非依赖）

> 主仓**不包含**该注册机进程，也不内置浏览器自动化。
> 我们的实现：`apps/control/app/services/grok_register.py` + `apps/control/app/routers/grok.py`，
> 协议与状态机：`docs/providers/grok-register.md`。

## 上游信息

- 仓库：<https://github.com/kaibush/grok-register>
- 许可：**MIT（许可证分层：可入主仓）**
- 形态：FastAPI + React + Camoufox/CloakBrowser 的 Web 注册控制台
- 关键 API：`/api/health`、`/api/stats`、`/api/job/start|stop`、`/api/job/logs`、
  `/api/accounts`、`/api/config`、`/api/connectivity`

## 主仓如何实现同等编排

- `grok_tasks` 任务表 + 进程内队列，全局并发与单任务 workers 上限；
- 状态机：`queued → running → succeeded / failed / cancelled`；
- 失败按 `max_attempts` 指数退避重新入队；`retry` 将 attempts 归零；
- 管理 API：`/api/grok/register/tasks`、`/tasks/{id}/cancel`、`/tasks/{id}/retry`、`/worker`；
- provider 侧只登记 `register` 能力位，不暴露 chat/models。

## 执行器边界

主仓不做真实浏览器注册。任务执行器通过 `GROK_REGISTER_EXECUTOR_URL` 指向外部运行时
（HTTP POST，返回 JSON）。未配置时任务以明确原因失败，不会伪造成功。
上游注册机可作为该执行器的参考实现，需自行 clone、独立部署，不进入本仓。
