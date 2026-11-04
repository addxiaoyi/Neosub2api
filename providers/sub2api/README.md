# sub2api —— 上游参考来源（非依赖）

> 主仓**不再依赖**该进程。这里只保留上游参考信息与可选的对照编排。
> 我们的实现：`apps/control/app/adapters/sub2api.py` + `sub2api_store.py`，
> 管理路由：`apps/control/app/routers/sub2api.py`，
> 协议与落库设计：`docs/providers/sub2api.md`。

## 上游信息

- 仓库：<https://github.com/Wei-Shaw/sub2api>
- 许可：**LGPL-3.0（许可证分层：只能进程隔离）**——不得链接上游库，
  本仓不包含其源码、也不链接/修改它；主仓实现为 clean-room 自研 MIT
- 能力：订阅配额分发的 AI API 网关（分组、账号池、复合路由）

## 主仓如何实现同等能力

- 自设计 SQLite 三表：`sub2_groups`（分组配额）、`sub2_accounts`（账号池/冷却）、
  `sub2_ledger`（扣减流水）；
- 自实现选择算法：`last_used_at` 轮转 + `weight`、账号级冷却、额度过滤；
- 自实现配额扣减：非流式取响应 `usage`，流式在流结束取 SSE usage；
- 自实现 429：分组额度耗尽或账号池全部不可用时确定性返回；
- 管理 API：`/api/sub2api/groups`、`/accounts`、`/ledger`、`/accounts/{id}/reset`；
- 不复用上游表结构、函数体、变量命名或注释。

## 可选对照实现

`docker-compose.fragment.yml` 仅用于对照上游行为，不作为主仓运行依赖：

```bash
docker compose -f providers/sub2api/docker-compose.fragment.yml up -d
```
