# 许可证与合规边界

> 面向本仓 Neosub2api 所有改动者。上游许可证事实见 [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md)，本文件只讲**可执行的边界**：什么不许出现在 `apps/`、进程隔离怎么算达标、哪些上游禁止商用、违规怎么处置、怎么自检。
> 基线日期 2026-11-03。若你手上是离线环境、无法访问 GitHub，则相关条目按「未复核」处理，不得默认宽松许可。

## 1. 结论速览

| 上游 | 许可证 | `apps/` 能否出现其代码 | 唯一允许的接触方式 |
| --- | --- | --- | --- |
| `addxiaoyi/codebuddy2api` | MIT | 可（当前未用，走 HTTP） | HTTP 代理 / 独立进程 |
| `czg86389-hub/muse2api` | MIT | 可 | clean-room 重写 |
| `kaibush/grok-register` | MIT | 可 | clean-room 重写 |
| `jasonxu114514/opencode2api` | GPL-3.0 | **禁止** | clean-room 重写（只读协议） |
| `addxiaoyi/gptGrok2api` | MIT | 可 | clean-room 重写 |
| `addxiaoyi/ZCode2api` | MIT | 可 | clean-room 重写 |
| `Wei-Shaw/sub2api` | LGPL-3.0 | **禁止**（本项目不做链接） | clean-room 重写（只读语义） |
| `JeffHu0912/trae2api` | MIT | 可 | clean-room 重写 / 独立进程 |
| `ColudAI/Deepseek2Go` | 无 LICENSE（All Rights Reserved） | **禁止** | 独立进程，本地自建，**禁止商用分发** |
| `NextAgentX/qoder-trae-workbuddy-switch` | PolyForm-NC 1.0.0 | **禁止** | clean-room 重写 / 独立进程，**禁止商用** |
| `AiXiaoc/codar2api` | MIT | 可 | clean-room 重写 / 独立进程 |

一句话判据：**许可证是 GPL / LGPL / PolyForm-NC / 未声明的，代码与二进制都不得进入 `apps/`，也不得进依赖清单**。MIT 的可以进，但仍优先 clean-room 而不是搬运。

## 2. `apps/` 硬边界

### 2.1 禁止（P0，发现即阻断合并）

1. 上游源码片段：任何函数体、类、类型定义、常量表从上游复制或改写而来（含「改个变量名」式改写）。
2. 上游导入路径：`github.com/Wei-Shaw/sub2api`、`github.com/jasonxu114514/opencode2api`、`github.com/codar-gateway/codar-gateway`、`opencode2api/internal`、`buddy_switch_*` 这类只可能来自上游的标识符。
3. 上游二进制：`.go` / `.rs` 源文件、`go.mod` / `Cargo.toml`、`*.wasm`（如 Deepseek2Go 的 `module.wasm`）、预编译 `*.so` / `*.dll` / `*.exe`。
4. 上游作为依赖：`requirements*.txt`、`package.json` 里出现上游仓库名或其 fork；`pip install git+https://github.com/<owner>/<upstream>` 同理违规。
5. 上游 LICENSE 文本被复制进 `apps/`：那不是「合规」，是许可证混同，只会误导审计。

### 2.2 允许（有明确形态）

| 允许形态 | 例子 | 为什么可以 |
| --- | --- | --- |
| `vendor=` 署名 | `vendor="jasonxu114514/opencode2api（协议参考）"` | 署名与来源说明，不含代码 |
| 模块 docstring 里的协议参考 | `按 docs/providers/opencode2api.md 提炼的协议契约独立实现` | 说明依据，属事实陈述 |
| 协议常量 | 端点路径 `/backend-api/conversation`、错误码、SSE 事件名 | 接口事实，不受版权保护 |
| 本仓自有模块名 | `adapters/sub2api.py`、`/api/sub2api/groups` | 命名沿用 provider id，不是上游代码 |

判断口诀：**这行是「告诉读者我参考了谁」，还是「让程序跑起来」？** 前者允许，后者若来自 GPL/LGPL/NC/无证上游就是违规。

### 2.3 目录职责

- `apps/` —— 只放本仓原创代码，MIT。
- `providers/` —— 只放上游参考说明（README.md）与*可选*的对照 compose；**不放源码、不放二进制**。
- `docs/providers/` —— 只写协议事实与实测记录；不贴上游代码块（含 ` ```go ` / ` ```rust ` 围栏）。
- `upstreams/` —— 登记清单，由巡检脚本读取。

## 3. 进程隔离怎么落地

### 3.1 五条硬性条件（全部满足才叫隔离）

1. **独立进程**：上游跑在自己的进程/容器里，主仓进程不 `import` 它、不 `dlopen` 它。
2. **只走 HTTP**：通信只有 `http(s)://host:port` 请求；不共享内存、不读对方私有文件、不加载 `.wasm`/`.so`。
3. **不带源码**：主仓镜像构建时 `COPY` 不涉及上游目录；`docker-compose.yml` 不 `build:` 上游。
4. **不改上游**：需要定制就改本仓适配器；上游升级等于换外部目录/镜像 tag，绝不打补丁。
5. **不默认分发**：上游二进制不进发行包。用户自建由用户自己获取并遵守其许可证。

### 3.2 现状端口表

| provider id | 默认地址 | 说明 |
| --- | --- | --- |
| `codebuddy` | `http://127.0.0.1:7864` | codebuddy2api 管理端；Go 数据面 7863 |
| `trae_sidecar` | `http://127.0.0.1:7871` | trae2api |
| `qoder_sidecar` | `http://127.0.0.1:7873` | qoder-switch，**禁止商用** |
| `deepseek_sidecar` | `http://127.0.0.1:5001` | Deepseek2Go，**禁止商用**（历史默认 7872 已废弃） |
| `codar_sidecar` | `http://127.0.0.1:8801` | codar2api |
| 可选对照 | 7869 / 7870 | `providers/opencode2api`、`providers/sub2api` 的 fragment，**不在主 compose 内** |

### 3.3 隔离状态的代码级自证

- `apps/control/app/registry.py` 的 `allows_loopback()` 只对「默认地址是 loopback」的 provider 放行回环上游；clean-room 的 provider 默认指向公网域名，不会被误配成本机。
- `apps/control/app/net_guard.py` 的 `guard_upstream_url()` 在发起请求前做地址校验；sidecar 地址由 `Settings`（env）或数据库 `providers.base_url` 提供，代码里不写死上游实现。
- 主仓 `requirements.txt` / `package.json` 无任何上游依赖（见第 5 节检查 4）。

### 3.4 若你确实要分发 sidecar

隔离成立不等于分发无义务。打包上游二进制时：

- MIT 上游：镜像内附 LICENSE 全文与版权行。
- GPL-3.0：附 LICENSE，并提供完整对应源码（同镜像内或书面 offer）。
- LGPL-3.0：附 LICENSE 与源码获取方式，保留用户可替换该库的路径。
- 无 LICENSE / PolyForm-NC：**不得**打包进商用分发物。

## 4. 商用禁用清单

### 4.1 清单

| 上游 | 状态 | 必须清理的东西 |
| --- | --- | --- |
| `ColudAI/Deepseek2Go` | 无 LICENSE，All Rights Reserved | `adapters/deepseek_sidecar.py`；`config.py` 中 `deepseek_sidecar_base`；`registry.py` 的 `ENV_BASE_FIELDS` 条目；`.env.example` 的 `DEEPSEEK_SIDECAR_BASE`；`docker-compose.yml` 相关服务；`providers/deepseek2go/` |
| `NextAgentX/qoder-trae-workbuddy-switch` | PolyForm-NC 1.0.0 | `adapters/qoder_sidecar.py`；`qoder_sidecar_base`；`ENV_BASE_FIELDS` 条目；`.env.example` 的 `QODER_SIDECAR_BASE`；`providers/qoder-switch/`。**`adapters/qoder.py` 可保留**（clean-room 直连） |

自研路径与 sidecar 是两回事：删 sidecar 不要顺手删掉 `qoder.py` / `codar.py` / `trae.py` 这类仓内实现。

### 4.2 清理后的自检

删完后 `GET /api/providers` 不应再出现对应 id；`grep` 不应再命中两个仓库名（除 `docs/` 与 `upstreams/` 的历史记录）。跑第 5 节检查 1/2/4 应为 0 或仅剩署名。

### 4.3 对外表述红线

这两家的风险主要不在代码，在**宣传口径**。默认 deployment 不含它们时，文案必须一致：

| 不要写 | 改写为 | 原因 |
| --- | --- | --- |
| 「内置 DeepSeek 支持」「开箱即用 DeepSeek」 | 「提供可插拔的 DeepSeek 适配位；上游实现需自行部署并取得授权」 | 「内置」会被视为随产品分发了无许可证二进制 |
| 「支持 Qoder 商用版」「企业可用 Qoder 通道」 | 「Qoder 接入不含 upstream 侧进程；请自行确认许可范围」 | PolyForm-NC 明确排除商业用途，宣称商用等于诱导违约 |
| 功能表把 sidecar 列为默认能力 | 单列「可选：需自行部署的独立进程」 | 默认能力=分发承诺 |
| 用上游项目 logo / 名称做主视觉 | 仅在「参考来源」小节列仓库链接 | 避免暗示背书或官方关系 |

README 与站点文案若与本节冲突，以本节为准（文案改动属另一条工作流，不在此文件实施）。

## 5. 自检：五条命令

在仓库根目录执行。全部为只读，不改文件。任何一条结果不符即视为不合规，先按第 6 节处置。

### 检查 1 — 上游导入路径 / 编译产物标识符（硬性，必须 0）

```powershell
$files = Get-ChildItem apps -Recurse -Include *.py,*.ts,*.tsx,*.js,*.go,*.rs,*.json,*.yaml,*.yml,*.toml -File |
  Where-Object { $_.FullName -notmatch '\\(node_modules|\.next|__pycache__|\.git)\\' }
$files | Select-String -Pattern 'github\.com/Wei-Shaw/sub2api|github\.com/jasonxu114514|github\.com/codar-gateway|opencode2api/internal|buddy_switch_|reloaded/internal'
```

判据：无输出 = 通过。

### 检查 2 — 上游 owner 前缀（人工复核，只允许署名行）

```powershell
$files = Get-ChildItem apps -Recurse -Include *.py,*.ts,*.tsx,*.js,*.json,*.yaml,*.yml -File |
  Where-Object { $_.FullName -notmatch '\\(node_modules|\.next|__pycache__|\.git)\\' }
$files | Select-String -Pattern 'jasonxu114514/|Wei-Shaw/|NextAgentX/|ColudAI/|AiXiaoc/|JeffHu0912/|kaibush/|czg86389-hub/|addxiaoyi/'
```

判据：所有命中行必须同时包含 `vendor=`、位于 docstring 内、或以 `#` 开头。出现其他形态（例如拼接进 URL 作为默认上游）即为违规。基线 12 条全部是 `vendor=`。

### 检查 3 — 仓库内不该有 Go / Rust 源码或上游二进制（必须 0）

```powershell
Get-ChildItem -Recurse -Include *.go,*.rs,*.wasm,*.so,*.dll,*.exe -File |
  Where-Object { $_.FullName -notmatch '\\(node_modules|\.git|\.runtime|\.next)\\' } |
  Select-Object -ExpandProperty FullName
```

判据：无输出 = 通过。`.runtime/` 是本地 Python venv，排除。

### 检查 4 — 依赖清单里没有上游（必须 0）

```powershell
Select-String -Path apps\control\requirements.txt,apps\control\requirements-dev.txt,apps\web\package.json `
  -Pattern 'opencode2api|sub2api|qoder|deepseek2go|codar2api|grok-register|muse2api|zcode2api|gptgrok2api|trae2api|codebuddy2api' -CaseSensitive:$false
```

判据：无输出 = 通过。

### 检查 5 — 文档没有贴上游代码

```powershell
Get-ChildItem docs\providers,providers -Recurse -Include *.md -File |
  Select-String -Pattern '^\s*```(go|rs|rust|gomod)|\bfunc [A-Z]\w*\(|package main|impl .+\{'
```

判据：无输出 = 通过。协议字段、路径、状态码可以写；函数体不行。

POSIX 等价（CI 用）：

```bash
set -e
grep -rEn 'github\.com/Wei-Shaw/sub2api|github\.com/jasonxu114514|github\.com/codar-gateway|opencode2api/internal|buddy_switch_' apps && exit 1 || true
test -z "$(find . -path ./node_modules -prune -o -path ./.git -prune -o -name '*.go' -print -o -name '*.rs' -print)"
grep -En 'opencode2api|sub2api|qoder|deepseek2go|codar2api|grok-register|muse2api|zcode2api|gptgrok2api|trae2api|codebuddy2api' apps/control/requirements.txt apps/web/package.json && exit 1 || true
```

### 5.6 基线记录（2026-11-03 实跑）

| 检查 | 结果 |
| --- | --- |
| 1 上游导入路径 | 0 命中 |
| 2 owner 前缀 | 12 条，全部为 `vendor=` 署名 |
| 3 Go/Rust/二进制 | 0 文件 |
| 4 依赖清单 | 0 命中（`requirements.txt` 4 个包，`package.json` 43 个依赖，均为通用库） |
| 5 文档代码块 | 0 命中 |
| 扫描范围 | `apps/` 下 261 个代码/配置文件 |

附加人工复核：`providers/` 仅含 `README.md` 与两份 `docker-compose.fragment.yml`（opencode2api 7869 / sub2api 7870），后者拉取上游镜像、不在主 compose 内。

## 6. 发现违规怎么处理

### 6.1 分级

| 级别 | 情形 | 处置时限 |
| --- | --- | --- |
| P0 | GPL/LGPL/NC/无证上游的源码或二进制出现在 `apps/`、依赖清单、或发版产物 | 立即停止合并与发版，当日修复 |
| P1 | 上游标识符以「能跑起来」的形态出现（默认上游地址、硬编码端点表） | 当轮修复 |
| P2 | 文档贴了上游代码块、署名缺失或写错 | 下一轮修复 |

### 6.2 流程

1. **冻结**：暂停相关分支合并与发版，不要先「顺手改改看」。
2. **定位**：记录文件路径、行号、引入 commit、来源上游与许可证（引用第 5 节命令输出作证据）。
3. **移除或替换**：优先 clean-room 重写；无法独立实现就降级为 sidecar，或直接下线该 provider。
4. **清残留**：依赖清单、env、compose、前端能力位、价格表、文档一并清；避免「适配器删了但服务还在」。
5. **回归**：跑第 5 节全部检查，结果贴进 PR 描述；补充一条阻断该形态的测试或文案检查。
6. **留档**：在 `upstreams/upstreams.yaml` 与 `THIRD-PARTY-NOTICES.md` 记录本次处置；不要只留在聊天里。

### 6.3 已经发布出去怎么办

- 撤回受影响 release / 镜像 tag，或替换为净化版本。
- 若涉及 GPL/LGPL 代码：按对应许可证补开源义务或彻底移除，二者择一，不能拖。
- 若涉及 NC / 无证上游：通知使用者停止商用，并给出移除指引。
- 重新跑自检并公开结果摘要。

### 6.4 升级上游时

先读 `docs/UPSTREAM-SYNC.md` 的顺序：核对许可证是否变化 → 核对协议是否变化 → 再决定是否推进 `analyzed_commit`。**许可证变了要当新上游重新评估**，尤其注意 GPL → AGPL、MIT →  BUSL/PolyForm 这类收紧。

## 7. 变更门禁

新增 provider 或接入新上游，必须同时满足：

- [ ] `upstreams/upstreams.yaml` 有 `license` 与 `license_source`，拿不到就写「未复核」
- [ ] `docs/providers/<name>.md` 写的是协议事实，没有代码块
- [ ] 若许可证为 GPL/LGPL/NC/无声明：`apps/` 内不出现其代码、不进依赖清单、默认只 sidecar 或 clean-room
- [ ] `THIRD-PARTY-NOTICES.md` 总表同步，`risk_level` 与许可证一致（GPL/LGPL 不得标 `safe`）
- [ ] 第 5 节五条检查全部符合判据
