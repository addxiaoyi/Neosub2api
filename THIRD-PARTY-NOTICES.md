# 第三方组件与许可证清单

> 复核基线：**2026-11-03**。11 家上游全部完成复核，无「未复核」项。
> 复核方式：GitHub REST API `repos/{owner}/{repo}` 取 `license.spdx_id`，再取 `contents/LICENSE` 原文首屏人工比对（含 copyright 行）。证据逐家列在第 2 节。
> 主仓许可证：根目录 [LICENSE](LICENSE) 为 MIT，署名为 `addxiaoyi2048`。
> 可执行边界、自检命令与违规处置：见 [docs/compliance.md](docs/compliance.md)。

## 0. 三种接触方式

| 方式 | 具体含义 | 对主仓许可证的影响 |
| --- | --- | --- |
| **clean-room 重写** | 只读上游 README / 协议文档 / 实测 HTTP 行为，函数、命名、数据结构独立实现 | 无。主仓保持 MIT，可商用分发 |
| **进程隔离 sidecar** | 上游作为独立进程运行，主仓只经 `http://host:port` 调用 | 无链接、无派生作品。但若**再分发**上游镜像/二进制，需自行履行其许可证义务 |
| **未使用** | 只登记、不接入 | 无 |

## 1. 总表（11 家）

「许可证风险」沿用程序内的 `risk_level` 语义：`safe` = 宽松许可可直接合入；`warn` = 传染性许可需隔离；`danger` = 无许可证或禁止商用。

| # | 上游仓库 | 复核到的许可证 | 风险 | 能否并入主仓 | 本项目当前方式 |
| --- | --- | --- | --- | --- | --- |
| 1 | `addxiaoyi/codebuddy2api` | MIT | safe | 可 | 进程隔离（管理端 7864，内置数据面 7863） |
| 2 | `czg86389-hub/muse2api` | MIT | safe | 可 | clean-room（直连 `api.meta.ai`，已无 sidecar） |
| 3 | `kaibush/grok-register` | MIT | safe | 可 | clean-room（注册状态机，控制面内置任务） |
| 4 | `jasonxu114514/opencode2api` | **GPL-3.0** | warn | **不可** | clean-room（直连 `opencode.ai/zen` 与 Go 层） |
| 5 | `addxiaoyi/gptGrok2api` | MIT | safe | 可 | clean-room（直连 `chatgpt.com` / `grok.com`） |
| 6 | `addxiaoyi/ZCode2api` | MIT | safe | 可 | clean-room（直连 `zcode.z.ai`） |
| 7 | `Wei-Shaw/sub2api` | **LGPL-3.0** | warn | 有条件；本项目选择不并入 | clean-room（分组/配额/429 语义，落库自设计） |
| 8 | `JeffHu0912/trae2api` | MIT | safe | 可 | clean-room 直连 + 可选 sidecar（7871） |
| 9 | `ColudAI/Deepseek2Go` | **无 LICENSE**（All Rights Reserved） | danger | **不可** | 进程隔离（5001），仅限本地自建、评估用途 |
| 10 | `NextAgentX/qoder-trae-workbuddy-switch` | **PolyForm-Noncommercial-1.0.0** | danger | **不可** | clean-room 直连 + 可选 sidecar（7873，禁止商用） |
| 11 | `AiXiaoc/codar2api` | MIT | safe | 可 | clean-room 直连 + 可选 sidecar（8801） |

> 说明：`addxiaoyi/codebuddy2api` 的 Go 数据面原由 `Sliverkiss/workbuddy2api` 提供，该仓库现已 404，故只按 codebuddy2api 一条登记。

## 2. 逐家说明

### 2.1 `addxiaoyi/codebuddy2api` — MIT
- 地址：https://github.com/addxiaoyi/codebuddy2api ・ LICENSE：https://github.com/addxiaoyi/codebuddy2api/blob/main/LICENSE
- 证据：API `license.spdx_id=MIT`；原文首行 `MIT License`，署名 `Copyright (c) 2026 HanHan666666` / `Copyright (c) 2026 Chris`。
- 我们做的：`apps/control/app/adapters/codebuddy.py` 只做 HTTP 代理（默认 `127.0.0.1:7864`），转发 OpenAI Chat / Anthropic Messages。不引用其 Python 包，不打包其镜像。

### 2.2 `czg86389-hub/muse2api` — MIT
- 地址：https://github.com/czg86389-hub/muse2api ・ LICENSE：https://github.com/czg86389-hub/muse2api/blob/main/LICENSE
- 证据：API `spdx_id=MIT`；原文 `MIT License`，署名 `Copyright (c) 2026 MUSE2API Authors`。
- 我们做的：`adapters/muse.py` 按 `docs/providers/muse2api.md` 记录的协议事实，直连 Muse 官方 OpenAI 兼容入口（Bearer key）。上游靠浏览器 DOM / Noise WebSocket 跑对话，本仓不复制这套实现。

### 2.3 `kaibush/grok-register` — MIT
- 地址：https://github.com/kaibush/grok-register ・ LICENSE：https://github.com/kaibush/grok-register/blob/main/LICENSE
- 证据：API `spdx_id=MIT`；原文 `MIT License`，署名 `Copyright (c) 2026 AaronL725`。
- 我们做的：`adapters/grok_register.py` + `services/grok_register.py` + `routers/grok.py` 只保留注册任务的状态机与字段语义，编排为控制面内置任务服务；未搬运上游流程代码。

### 2.4 `jasonxu114514/opencode2api` — GPL-3.0
- 地址：https://github.com/jasonxu114514/opencode2api ・ LICENSE：https://github.com/jasonxu114514/opencode2api/blob/main/LICENSE
- 证据：API `spdx_id=GPL-3.0`；原文首行 `GNU GENERAL PUBLIC LICENSE / Version 3, 29 June 2007`。上游为 Go 项目（`go.mod: module opencode2api`）。
- 我们做的：`adapters/opencode.py` 依据 `docs/providers/opencode2api.md` 的协议契约独立实现——模型发现、Chat 与 Anthropic Messages 转发、SSE 直通、可重试状态码换 key。详见第 3 节。

### 2.5 `addxiaoyi/gptGrok2api` — MIT
- 地址：https://github.com/addxiaoyi/gptGrok2api ・ LICENSE：https://github.com/addxiaoyi/gptGrok2api/blob/main/LICENSE
- 证据：API `spdx_id=MIT`；原文 `MIT License`，署名 `Copyright (c) 2026 kunkun`。
- 我们做的：`adapters/gptgrok.py` 直连 ChatGPT Web（`/backend-api/conversation`，SSE）与 Grok Web（`/rest/app-chat/conversations/new`，逐行 JSON），按模型前缀分流。上游另有 responses/messages/images 端点，本仓未接。

### 2.6 `addxiaoyi/ZCode2api` — MIT
- 地址：https://github.com/addxiaoyi/ZCode2api ・ LICENSE：https://github.com/addxiaoyi/ZCode2api/blob/main/LICENSE
- 证据：API `spdx_id=MIT`；原文 `MIT License`，署名 `Copyright (c) 2026`。
- 我们做的：`adapters/zcode.py` 直连 `zcode.z.ai` 的 Anthropic Messages 端点（JWT 双头 + 设备指纹 + 官方 system 指纹校验），OpenAI Chat 由其事件翻译得到，`/v1/messages` 原样透传。不再需要 `zcgw-server`。

### 2.7 `Wei-Shaw/sub2api` — LGPL-3.0
- 地址：https://github.com/Wei-Shaw/sub2api ・ LICENSE：https://github.com/Wei-Shaw/sub2api/blob/main/LICENSE
- 证据：API `spdx_id=LGPL-3.0`；原文首行 `GNU LESSER GENERAL PUBLIC LICENSE / Version 3, 29 June 2007`。上游 Go 后端（`backend/go.mod: module github.com/Wei-Shaw/sub2api`）另有 CLA.md。
- 我们做的：`adapters/sub2api.py` + `sub2api_store.py` + `routers/sub2api.py` 只实现「分组 → 账号池 → 配额扣减 → 429」语义，配额流水表结构自设计。详见第 4 节。

### 2.8 `JeffHu0912/trae2api` — MIT
- 地址：https://github.com/JeffHu0912/trae2api ・ LICENSE：https://github.com/JeffHu0912/trae2api/blob/main/LICENSE
- 证据：API `spdx_id=MIT`；原文 `MIT License`，署名 `Copyright (c) 2026 connectedGraph`。
- 我们做的：双路径。`adapters/trae.py` 按 `docs/providers/trae2api.md` 独立实现 Solo（`llm_utils_chat`）与 Work 两通道、头构造与流改写；`adapters/trae_sidecar.py` 保留 7871 代理作为可选替代。

### 2.9 `ColudAI/Deepseek2Go` — 无 LICENSE
- 地址：https://github.com/ColudAI/Deepseek2Go（仓库根目录无 LICENSE / NOTICE / COPYING）
- 证据：API `license=null`；`contents/` 列目录只有 `.github/ .gitignore Dockerfile README.md build.sh cmd/ example.config.json go.mod module.wasm`，无任何授权文件；README 亦无授权声明。按 **All Rights Reserved** 处理，未复核项为零。
- 我们做的：`adapters/deepseek_sidecar.py` 只代理 `127.0.0.1:5001`，主仓不含其源码、`module.wasm` 或二进制。详细边界见 `docs/providers/deepseek2go-feasibility.md`。

### 2.10 `NextAgentX/qoder-trae-workbuddy-switch` — PolyForm Noncommercial 1.0.0
- 地址：https://github.com/NextAgentX/qoder-trae-workbuddy-switch ・ LICENSE：https://github.com/NextAgentX/qoder-trae-workbuddy-switch/blob/main/LICENSE
- 证据：GitHub API 返回 `spdx_id=NOASSERTION`（PolyForm 不在其许可证白名单内），但 `LICENSE` 正文首行是 `Required Notice: Copyright (c) 2026 NextAgentX` 与 `PolyForm Noncommercial License 1.0.0`。**不要因为 API 显示 NOASSERTION 就当未知宽松许可**，以原文为准。
- 我们做的：`adapters/qoder.py` 独立实现 COSY 网关的协议事实（加密原语全部用标准库自写）；`adapters/qoder_sidecar.py` 保留 7873 代理，属禁止商用部分。

### 2.11 `AiXiaoc/codar2api` — MIT
- 地址：https://github.com/AiXiaoc/codar2api ・ LICENSE：https://github.com/AiXiaoc/codar2api/blob/main/LICENSE
- 证据：API `spdx_id=MIT`；原文 `MIT License`，署名 `Copyright (c) 2026 AiXiaoc`。上游 Go 单二进制（`go.mod: module github.com/codar-gateway/codar-gateway`）。
- 我们做的：双路径。`adapters/codar.py` 独立实现 `/v1/models`（读私有目录）、chat/completions、messages、responses 转发的 SSE 透传与多 token 轮换；扫码/短信登录、新 IP 验证、签到仍留在 `codar2api`（`adapters/codar_sidecar.py`，8801）。

## 3. GPL-3.0：为什么主仓里没有任何 opencode2api 代码

GPL-3.0 的传染性按「衍生作品」判定。只要主仓包含上游源码、或链接其编译产物，整个分发物就得整体按 GPL-3.0 开源，与本仓 MIT 定位冲突。因此本项目对该仓库只做三件事：

1. **只读协议层**：读上游 README 与 `docs/providers/opencode2api.md` 里记录的实测 HTTP 行为（端点路径、鉴权头、SSE 事件形状、错误码），不读实现。
2. **独立实现**：`adapters/opencode.py` 的函数划分、命名、错误模型、重试策略均为自写；不引入 `opencode2api/internal` 等上游包路径。
3. **零残留**：仓库内没有 `.go` 文件，没有 `go.mod`，依赖清单里没有上游模块；`providers/opencode2api/` 只有 README 与一份*可选*对照 compose（拉取上游镜像独立运行，不在主 compose 内）。

可核查证据（2026-11-03 实跑，见 `docs/compliance.md` 第 5 节）：`apps/` 内 `github.com/jasonxu114514` 与 `opencode2api/internal` 命中数为 0；`*.go` 文件数 0。

同样适用于 **2.10 的 clean-room 路径**：`adapters/qoder.py` 不含 Rust 侧任何 crate 代码（`buddy_switch_*` 标识符命中 0），也不调用其二进制。

## 4. LGPL-3.0：为什么不链接 sub2api

LGPL-3.0 允许以动态链接方式使用库而不传染主程序，但前提是「可替换的库边界 + 保留反向工程权利」。本项目不采用这条路，理由是把边界做干净比做合规声明更省事：

- 不把 sub2api 作为 Python 依赖、Go module 或前端包引入；`requirements.txt` / `package.json` 里没有它。
- 不复制其 Go 后端源码片段；`sub2api_store.py` 的表结构与扣减流水为自设计。
- 只复现公开语义：分组、账号池轮换、配额扣减单位（token）、额度耗尽返回 429。这些属接口行为，不构成表达性代码。
- 若未来确需与其进程互通，只允许 HTTP（`providers/sub2api/docker-compose.fragment.yml` 的可选对照），不得改为 `ctypes` / `cgo` / 动态库加载。

## 5. 商用禁用清单（发布前必须处理）

| 上游 | 许可证状态 | 涉及文件 | 处置 |
| --- | --- | --- | --- |
| `ColudAI/Deepseek2Go` | 无 LICENSE，All Rights Reserved | `adapters/deepseek_sidecar.py`、`DEEPSEEK_SIDECAR_BASE`、`providers/deepseek2go/` | 商用发布前移除，或取得作者书面授权并由作者补充 LICENSE |
| `NextAgentX/qoder-trae-workbuddy-switch` | PolyForm-NC 1.0.0，禁止任何商业用途 | `adapters/qoder_sidecar.py`、`QODER_SIDECAR_BASE`、7873 服务 | 商用发布前移除，或取得 NextAgentX 书面商用授权。`adapters/qoder.py`（clean-room 直连）不受此限，可保留 |

两家都属「本地自建、评估用途」。对外文案不得写成「内置支持」「开箱即用」，只能写「可对接用户自行部署的独立进程」。具体措辞红线见 `docs/compliance.md` 第 4.3 节。

## 6. 分发时的声明模板

主仓自身分发包（源码或镜像）建议附带以下段落，与本文档一并提供：

```
本产品为 MIT 许可软件（Copyright (c) 2026 addxiaoyi2048）。
本产品包含对以下开源项目的协议行为独立实现，未包含其源代码，亦未与其链接：
  - opencode2api (GPL-3.0)      —— 仅参考公开协议，代码独立编写
  - sub2api (LGPL-3.0)          —— 仅参考公开语义，代码独立编写
  - qoder-trae-workbuddy-switch (PolyForm Noncommercial 1.0.0) —— 仅参考 COSY 协议事实
  - muse2api / gptGrok2api / ZCode2api / trae2api / codar2api / grok-register (MIT)
本产品不包含、也不分发 ColudAI/Deepseek2Go（无许可证）与 qoder-trae-workbuddy-switch
的 sidecar 二进制；如需使用，由部署方自行获取并遵守其许可证。
第三方组件完整清单见 THIRD-PARTY-NOTICES.md。
```

若确实要随分发物打包某个 sidecar（例如自托管镜像内预置上游二进制），额外义务：

- MIT 上游：镜像内附其 LICENSE 全文与版权行。
- GPL-3.0 上游：镜像内附 LICENSE 全文，并提供对应源码获取方式（书面 offer 或同镜像内源码）。
- LGPL-3.0 上游：附 LICENSE 全文与源码获取方式，且保留替换库的可行路径。
- 无 LICENSE / PolyForm-NC：**不得**打包进商用分发物。

## 7. 维护约定

1. 新增 provider 先改 `upstreams/upstreams.yaml`（`license` / `license_source` / `integration` / `analyzed_commit`），再写 `docs/providers/<name>.md` 协议契约，最后同步本文件。
2. `license` 必须来自上游 LICENSE 原文或 GitHub `license.spdx_id`。拿不到就写「未复核」，不得推测为 MIT。
3. 遇到 `NOASSERTION`（如 PolyForm 系列）以 LICENSE 正文为准，不能当「无限制」。
4. 只要许可证是 GPL / LGPL / PolyForm-NC / 未声明，默认只允许 clean-room 或进程隔离，禁止拷贝源码、禁止加入依赖清单。
5. 每次新增或升级上游后，跑 `docs/compliance.md` 第 5 节自检命令，结果留档。
