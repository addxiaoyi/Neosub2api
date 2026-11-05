# 第三方组件与许可证清单

> 复核基线：**2026-11-03**。11 家上游全部完成复核，无「未复核」项。
> 复核方式：GitHub REST API `repos/{owner}/{repo}` 取 `license.spdx_id`，再取 `contents/LICENSE` 原文首屏人工比对（含 copyright 行）。证据逐家列在第 2 节。
> 主仓许可证：根目录 [LICENSE](LICENSE) 为 MIT，署名为 `addxiaoyi2048`。
> 依赖清单（第 8、9 节）复核基线：**2026-11-04**，Python 版本取自 `apps/control/requirements.txt`（`uv pip compile` 编译产物，生成信息见 8.1）、许可证取自各包 `METADATA`；前端取值来自 `apps/web/package-lock.json` + `node_modules/<pkg>/package.json` 的 `license` 字段，全部为实读，未凭印象填写。
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
本产品运行时依赖（第 8 节）均为 MIT / BSD-3-Clause / Apache-2.0 / ISC / PSF-2.0 / MPL-2.0 等宽松许可；
构建期依赖（sharp 的 libvips 二进制、caniuse-lite、eslint/tailwind/typescript 链，第 9 节）不随分发物提供。
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
6. `apps/control/requirements.txt` 或 `apps/web/package-lock.json` 变动后，同步第 8 节的版本与许可证；许可证只从包自身元数据（`METADATA` / `package.json`）读取，读不到就标 `unknown` 并写明原因，不得推测。`sharp` / `caniuse-lite` / eslint / tailwind 链若有进出，同步第 9 节。

## 8. 运行时第三方依赖清单（pip / npm 直接与传递依赖）

第 1–7 节讲 11 家上游仓库；本节讲本仓自己 `pip install` / `npm ci` 装进来的包。复核基线 **2026-11-04**。

**取值方式（实读，非记忆）：**

- Python：**版本**取自 `apps/control/requirements.txt` 编译产物（23 包全钉），生成命令 `uv pip compile --python-platform x86_64-unknown-linux-gnu --python-version 3.11 --no-strip-markers apps/control/requirements.in -o apps/control/requirements.txt`（uv 0.13.7，生成日期 2026-11-04，编译环境 Python 3.11 / linux x86_64，对齐运行镜像 `python:3.11-slim`）；**许可证字段**读各包 `METADATA` 的 `License-Expression` / `License` / `Classifier`。依赖随运行镜像分发，证据是 `apps/control/Dockerfile:12-13`（`pip install -r requirements.txt` 于 `python:3.11-slim`）。
- 前端：读 `apps/web/package-lock.json` 与 `node_modules/<pkg>/package.json` 的 `license` 字段。产物是静态导出（`next.config.ts` 的 `output: 'export'`），运行镜像只含 `out/` 静态文件，见第 9 节。

### 8.1 Python（`apps/control`）

| 包 | 版本 | 许可证 | 来源字段 |
| --- | --- | --- | --- |
| fastapi | 0.162.0 | MIT | `License-Expression: MIT`（0.115.6 → 0.162.0） |
| starlette | 1.3.1 | BSD-3-Clause | `License-Expression: BSD-3-Clause`（本次显式钉版） |
| uvicorn | 0.34.0 | BSD-3-Clause | `License: BSD-3-Clause` + `Classifier: License :: OSI Approved :: BSD License` |
| httpx | 0.28.1 | BSD-3-Clause | `License` + `Classifier` 同上 |
| PyYAML | 6.0.3 | MIT | `License: MIT` + `Classifier: License :: OSI Approved :: MIT License` |
| pydantic | 2.14.2 | MIT | `License-Expression: MIT` |
| pydantic-core | 2.50.2 | MIT | `License-Expression: MIT` |
| annotated-types | 0.8.0 | MIT | `License-Expression: MIT` + `Classifier` |
| typing-extensions | 4.16.0 | PSF-2.0 | `License-Expression: PSF-2.0` |
| typing-inspection | 0.4.4 | MIT | `License-Expression: MIT` |
| annotated-doc | 0.0.5 | MIT | `License-Expression: MIT`；**本次新增传递依赖**，来自 fastapi 0.162.0 的 `Requires-Dist: annotated-doc>=0.0.2` |
| opentelemetry-api | 1.45.2 | Apache-2.0 | `License-Expression: Apache-2.0`；**本次新增传递依赖**，来自 fastapi 0.162.0 的 `Requires-Dist: opentelemetry-api>=1.44.0` |
| anyio | 4.16.0 | MIT | `License-Expression: MIT`；starlette 1.3.1 的 `Requires-Dist: anyio<5,>=3.6.2` |
| h11 | 0.16.0 | MIT | `License: MIT` + `Classifier` |
| httpcore | 1.0.9 | BSD-3-Clause | `License-Expression` + `Classifier` |
| certifi | 2026.10.23 | **MPL-2.0** | `License: MPL-2.0` + `Classifier: License :: OSI Approved :: Mozilla Public License 2.0 (MPL 2.0)` |
| idna | 3.20 | BSD-3-Clause | `License-Expression: BSD-3-Clause` |
| click | 8.5.0 | BSD-3-Clause | `License-Expression: BSD-3-Clause` |
| python-dotenv | 1.2.4 | BSD-3-Clause | `License: BSD-3-Clause`（`uvicorn[standard]`） |
| httptools | 0.9.0 | MIT | `License-Expression: MIT`（`uvicorn[standard]`） |
| watchfiles | 1.3.0 | MIT | `License` + `Classifier`（`uvicorn[standard]`） |
| websockets | 17.4 | BSD-3-Clause | `License-Expression: BSD-3-Clause`（`uvicorn[standard]`） |
| uvloop | 0.23.0（锁文件钉版；此前为「未锁定」，2026-11-04 收口） | MIT | 锁文件行：`uvloop==0.23.0 ; platform_python_implementation != 'PyPy' and sys_platform != 'cygwin' and sys_platform != 'win32'`。本机（Windows）未安装，读不到本地 `METADATA`，许可证沿用 PyPI JSON `info.license = "MIT License"`；该包 `Classifier` 同时列了 Apache 与 MIT 两条，按 `license` 字段取 MIT。仅非 win32 平台由 `uvicorn[standard]` 装入，Linux 运行镜像内存在 |

本表 23 行与锁文件的 23 个包一一对应，2026-11-04 按锁逐行核对无遗漏、无多列。

两点必须知道的边界：

- **本仓没有 Python 锁文件**（体检当时的旧状态，止于 2026-11-04）。`requirements.txt` 原先只钉 5 个直接依赖，上表传递依赖版本是本机 site-packages 实测值，容器构建时 `pip` 可能解析到同主版本的其他小版本（许可证字段基本不变，版本可能有差异）。**当前状态（2026-11-04 起）**：`requirements.txt` 已改为 `uv pip compile` 全钉编译产物（23 包），上表版本即锁文件版本，容器构建不再有解析漂移；再生成方式见 `docs/dependency-audit.md` 第 5 节。
- `colorama 0.4.6` **不在锁文件内**（2026-11-04 编译目标为 linux x86_64，`uvicorn[standard]` 的 `sys_platform == 'win32'` 依赖不纳入），即由本仓锁文件安装时 Windows 开发机与 Linux 运行镜像都不装它；`uvicorn` 源码无 `colorama` 导入（本地 site-packages 全量检索 0 命中），不装不影响导入。其元数据只有 `Classifier: License :: OSI Approved :: BSD License`，**没有**可判读的 SPDX 字段，故标 `BSD`（未细到 `-3-Clause`），不推测。

### 8.2 前端（`apps/web`）

| 包 | 版本 | 许可证 | 来源字段 |
| --- | --- | --- | --- |
| next | 15.5.29（钉版） | MIT | `node_modules/next/package.json` `license` + lock `node_modules/next.license` |
| react | 19.3.0（声明 `^19.0.0`） | MIT | `license` 字段 |
| react-dom | 19.3.0 | MIT | `license` 字段 |
| scheduler | 0.28.0 | MIT | `license` 字段（react-dom 传递） |
| axios | 1.20.0（声明 `^1.9.0`） | MIT | `license` 字段 |
| follow-redirects | 1.16.2 | MIT | `license` 字段（axios 传递，本次升级到 1.16.2） |
| sharp | 0.35.5 | Apache-2.0 | `license` 字段；next 的 `optional` 依赖，**不随分发物提供**，见第 9 节 |
| postcss | 8.5.28（`overrides` 钉版） | MIT | `license` 字段 |
| source-map-js | 1.2.2（`overrides` 钉版） | BSD-3-Clause | `license` 字段 |
| brace-expansion | 1.1.21 / 5.0.12（`overrides` 钉版） | MIT | `license` 字段；两处均在 dev 链（eslint → `@typescript-eslint`） |

其余生产条目（`@radix-ui/*`、`@tabler/icons-react`、`recharts`、`motion`、`date-fns`、`lucide-react` 等）全量以 `package-lock.json` 为准，分布见 8.3。

### 8.3 全量统计与冲突结论

对 `package-lock.json` 全部 **555** 条包条目统计，其中**非 devDependencies 的 228 条**许可证分布如下（`license` 字段缺失 0 条，即 **npm 侧无 `unknown`**；另有 327 条 `dev: true` 的开发/构建期条目，处置见第 9 节）：

| 许可证 | 条数 | 说明 |
| --- | --- | --- |
| MIT | 177 | 宽松 |
| Apache-2.0 | 16 | 宽松（含 sharp 0.35.5 本体与 `@img/sharp-darwin*` / `@img/sharp-linux*` 等平台壳包） |
| ISC | 15 | 宽松 |
| BSD-3-Clause | 3 | 宽松（含 source-map-js 1.2.2） |
| 0BSD | 1 | 宽松 |
| MIT AND ISC | 1 | 宽松 |
| LGPL-3.0-or-later | 10 | **copyleft**：`@img/sharp-libvips-*` 各平台二进制，见第 9 节 |
| Apache-2.0 AND LGPL-3.0-or-later | 3 | **copyleft**：`@img/sharp-win32-*` 平台包 |
| Apache-2.0 AND LGPL-3.0-or-later AND MIT | 1 | **copyleft**：`@img/sharp-wasm32` |
| CC-BY-4.0 | 1 | **署名类**：`caniuse-lite`，见第 9 节 |

结论：

1. **无许可证冲突**。生产依赖树里没有 GPL / AGPL / SSPL / BUSL / 商业限制许可，与主仓 MIT 分发不冲突；上游 11 家的 GPL / LGPL / PolyForm-NC / 无许可风险仍只在第 1、5 节，与依赖树无关。
2. **唯一的两条非宽松项都不进分发物**：LGPL-3.0-or-later 的 `@img/sharp-libvips-*` / `@img/sharp-*` 平台包与 CC-BY-4.0 的 `caniuse-lite`，边界与触发条件写在第 9 节。
3. Python 侧唯一的弱 copyleft 是 `certifi`（MPL-2.0）：**它随运行镜像分发**，因此镜像内保留其 `LICENSE` 全文即可；本仓未修改其任何文件，不触发源码公开义务，与 MIT 主仓兼容。

## 9. 仅构建期、不随分发的依赖

**边界证据**：`apps/web/Dockerfile` 是多阶段构建——`npm ci`（第 4 行）与 `npm run build:export`（第 8 行）只发生在 `node:22-alpine` 构建阶段，最终镜像只 `COPY --from=build /app/out`（第 10-12 行）。也就是说 **`node_modules` 不进入任何对外分发物**，运行镜像里只有 `out/` 静态文件与 nginx。

| 依赖 | 版本 | 许可证 | 来源字段 | 为什么只在构建期 |
| --- | --- | --- | --- | --- |
| sharp | 0.35.5 | Apache-2.0 | `package.json` `license` | next 的 `optional` 依赖；且 `next.config.ts` 里 `images.unoptimized: true` + `output: 'export'`，构建与运行都不做服务端图片优化，只被装进构建阶段 |
| `@img/sharp-libvips-*`（darwin/linux/linuxmusl/arm 等 10 个变体） | 1.3.4 | **LGPL-3.0-or-later** | `package-lock.json` 各条 `license`，均 `optional: true` | 各平台 libvips 预编译二进制，`npm ci` 按 `os/cpu` 选择性安装到构建阶段的 `node_modules`，不复制进 `out/` |
| `@img/sharp-win32-*`（arm64/ia32/x64） | 0.35.5 | **Apache-2.0 AND LGPL-3.0-or-later** | `package-lock.json` `license`，`optional: true` | Windows 平台壳包（捆绑 libvips），同上不进 `out/` |
| `@img/sharp-wasm32` | 0.35.5 | **Apache-2.0 AND LGPL-3.0-or-later AND MIT** | `package-lock.json` `license`，`optional: true` | WASM 变体，同上 |
| `@img/sharp-darwin-*` / `@img/sharp-linux*` / `@img/sharp-freebsd-wasm32` / `@img/sharp-webcontainers-wasm32` / `@img/colour` | 0.35.5（colour 1.1.0） | Apache-2.0（colour 为 MIT） | `package-lock.json` `license`，`optional: true` | 其余平台壳包与配色辅助包，同样是构建阶段按平台安装 |
| caniuse-lite | 1.0.30001810 | **CC-BY-4.0** | `package-lock.json` / `node_modules/caniuse-lite/package.json` `license` | 浏览器兼容性数据，只供构建期 browserslist 查询，数据本身不进 `out/` |
| eslint 链：eslint 9.39.5、eslint-config-next 15.5.29、`@eslint/eslintrc`、`@typescript-eslint/*` 等（lock 中含 `eslint` 的 43 条 dev 条目） | 见 lock | MIT 30 / Apache-2.0 8 / ISC 3 / BSD-2-Clause 1 / BlueOak-1.0.0 1 | 各包 `package.json` `license`，lock 中均为 `dev: true` | `devDependencies` 及其传递链，只在 `npm ci` 默认安装时出现 |
| tailwind 链：tailwindcss 4.3.3、`@tailwindcss/postcss` 4.3.3、`@tailwindcss/oxide-*`、tw-animate-css（含 `tailwind` 的 17 条 dev 条目） | 见 lock | MIT（17/17） | 各包 `package.json` `license`，`dev: true` | `devDependencies`；只参与把源码编译成 CSS，包本身不进最终镜像（产物里只有生成的 CSS，不含其源码） |
| typescript | 5.9.3 | Apache-2.0 | `package.json` `license` | `devDependencies`，仅类型检查与编译 |
| brace-expansion 1.1.21 / 5.0.12（`overrides` 钉版） | 见 8.2 | MIT | lock `license`，`dev: true` | 位于 eslint 传递链，仅构建期 |

**声明是否齐全、以及触发条件：**

- 当前分发物**不包含**任何 LGPL-3.0 的 libvips 二进制与 CC-BY-4.0 的 caniuse-lite 数据，因此不触发「附库源码获取方式」「署名」义务；上面的表就是对外可引用的「仅构建期、不随分发」声明（`docs/dependency-audit.md` 的 P3 项对应本节）。
- **若将来**改成随镜像携带完整 `node_modules`、或重新打开服务端图片优化，义务立即触发：
  - `@img/sharp-libvips-*`（LGPL-3.0-or-later）：镜像内需附许可证全文与对应库源码获取方式，并保留用户可替换该库的路径；
  - `caniuse-lite`（CC-BY-4.0）：需附署名、许可证链接与来源说明。
- `@img/*` 与 `sharp` 的许可证是**复合表达式**（`AND` 连接），对外声明必须整串写，不能只写 Apache-2.0 而漏掉 LGPL-3.0-or-later。
