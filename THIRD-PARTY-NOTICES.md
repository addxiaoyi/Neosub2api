# 第三方组件与许可证清单

> unified-2api 曾评估 10 家上游仓库。自 F1 起主仓走 clean-room 重写路线：
> 只依据公开 README / 协议说明 / 实测 HTTP 行为提炼契约，实现全部新写，
> 不复制上游源码。风险等级：
> - **clean-room**：主仓自研 MIT 实现，上游仅作协议参考，可直接商用分发
> - **safe**：MIT/Apache/BSD 等宽松许可，代码可自由合入主仓
> - **sidecar（danger）**：无 LICENSE 或禁止商用，独立进程隔离，**商用前必须移除**
>
> 契约文档见 `docs/providers/`，上游参考来源见 `providers/*/README.md`。

---

## ✅ clean-room 自研（MIT，主仓直接分发）

| # | 上游参考 | 上游许可证 | 主仓实现 | 说明 |
| --- | --- | --- | --- | --- |
| 1 | opencode2api | GPL-3.0 | `adapters/opencode.py`（直连 Zen/Go） | 只取端点/鉴权/SSE/错误码契约，函数与命名全新 |
| 2 | sub2api | LGPL-3.0 | `adapters/sub2api.py` + `sub2api_store.py` | 只取分组/配额/429 语义，落库模型自设计 |
| 3 | grok-register | MIT | `services/grok_register.py` + `routers/grok.py` | 只取任务字段/状态机语义，编排全新 |

> 这三家**不再**以 sidecar 进程形式接入，主仓不依赖外部进程即可运行。
> `providers/` 下对应目录仅保留「上游参考来源」说明与可选对照编排。

## ✅ safe（可自由合入）

| # | 仓库 | 许可证 | 接入形态 | 限制 |
| --- | --- | --- | --- | --- |
| 4 | **CodeBuddy 数据面/管理端**（本地部署） | 由内部交付 | 独立进程 + FastAPI 适配器代理 | 需 self-host |
| 5 | muse2api | MIT | 独立上游进程 + HTTP 代理适配器 | 无 |
| 6 | gptGrok2api | MIT | 独立上游进程 + HTTP 代理适配器 | 无 |
| 7 | ZCode2api | MIT | 独立上游进程 + HTTP 代理适配器 | 无 |
| 8 | **trae2api** `JeffHu0912/trae2api` | MIT | sidecar 独立 Go 进程 + `adapters/trae_sidecar.py` 代理 | F2 计划重写为内置实现 |
| 9 | Grok Register（注册机） | MIT | 见 clean-room #3 | 已内置 |

## 🔴 sidecar（禁止商用，仅评估）

| # | 仓库 | 许可证 | 接入形态 | 硬限制 |
| --- | --- | --- | --- | --- |
| 10 | **Deepseek2Go** `ColudAI/Deepseek2Go` | **无 LICENSE**（All Rights Reserved） | sidecar 独立进程 + `adapters/deepseek_sidecar.py` 代理 | 任何形式的商用必须移除；源码不入本仓 |
| 11 | **qoder-trae-workbuddy-switch** `NextAgentX/qoder-trae-workbuddy-switch` | **PolyForm Noncommercial 1.0.0** | sidecar 独立进程 + `adapters/qoder_sidecar.py` 代理 | 明确禁止任何商用；作者书面授权前不得商用 |

---

## 合规边界（商用前 checklist）

### 商用前必须完成

- [ ] 移除 #10 Deepseek2Go sidecar 及其适配器（或取得作者书面授权 + 作者补 LICENSE）
- [ ] 移除 #11 qoder sidecar 及其适配器（或取得 NextAgentX 书面商用授权）
- [ ] 确认 clean-room 三家的实现与上游无实质性相似（保留本仓提交历史作为独立创作证据）

### clean-room 模式的合规前提

- 实现只对齐**协议契约**：端点路径、鉴权头、请求/响应字段、SSE 分片、错误码、限流
- 不粘贴上游函数体，不沿用其内部变量命名与注释
- `docs/providers/*.md` 只写「依据公开 README / 协议说明 / 实测 HTTP 行为」，不贴源码片段
- `providers/*/README.md` 只放参考来源与可选对照编排，不放上游源码
- 主仓 LICENSE 保持 MIT，可分发、可商用

### sidecar 模式的合规前提（仅剩 danger 两家）

- 本仓不含 sidecar 源码——它们 clone 在外部目录（如 `research2/`）
- `adapters/*_sidecar.py` 只含 HTTP 客户端调用，不含上游算法/业务逻辑
- docker-compose 通过网络地址引用 sidecar，不 include 上游源码目录

---

## License URLs

| 仓库 | 许可证链接 |
| --- | --- |
| opencode2api（参考） | 见本地 `research/` 目录 clone |
| sub2api（参考） | 见本地 `research/` 目录 clone |
| grok-register（参考） | 见本地 `research/` 目录 clone |
| trae2api | https://github.com/JeffHu0912/trae2api/blob/main/LICENSE |
| muse2api | 见本地 `research/` 目录 clone |
| gptGrok2api | 见本地 `research/` 目录 clone |
| ZCode2api | 见本地 `research/` 目录 clone |
| Deepseek2Go | **无 LICENSE 文件**（GitHub 仓库根目录） |
| qoder | https://github.com/NextAgentX/qoder-trae-workbuddy-switch/blob/main/LICENSE |
