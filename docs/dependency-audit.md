# 依赖与许可证合规体检

> 执行日期：2026-11-04 ・ 只报告不升级，本文档为本次唯一新增文件。
> 工具：`npm audit --json --registry=https://registry.npmjs.org`（apps/web）、`pip-audit 2.10.1`（临时 venv，`%TEMP%\u2api-dep-audit-venv`）、OSV API 查严重级别、pip 解析器 `--dry-run` 验证修复路径。

## 结论

高危及以上共 **8 项**（前端 high 4 + critical 1，后端 high 3 + critical 0，前后端无重叠包）；**无阻断性许可证冲突**——主仓未发现复制 GPL/LGPL/PolyForm-NC 上游源码的证据，依赖树中无 GPL/AGPL 包，项目 MIT 署名 `addxiaoyi2048` 与文档一致；存在 3 个需注意的非阻断项（package.json 缺 license 字段、LGPL 构建期二进制、CC-BY 数据依赖），见第 2 节。

---

## 1. 高危依赖清单（仅 high / critical）

### 1.1 前端 apps/web（npm audit，2026-11-04 实跑）

metadata：total 19 = critical 1 + high 4 + moderate 14（moderate 不计入高危，14 行的处置见 1.3）；依赖总数 555。

| 包 | 安装版本 | 级别 | 漏洞（GHSA / CVE，来自 audit 返回） | 非破坏性修复路径 |
| --- | --- | --- | --- | --- |
| `next`（直接依赖，生产） | 15.5.25（package.json 精确钉版） | **critical**（聚合） | GHSA-2464-fhr4-w9xw：Image Optimization API 使用 SVG 时未授权 RCE（critical，影响 10.0.0–15.5.27）；GHSA-p3m5-59mp-jv5r：App Router Server Actions DoS（high，≤15.5.27）；另含 2 个 moderate 缓存投毒 GHSA-4jqv-mc3x-m676 / GHSA-mcj8-r9mp-w47p | **有**。npm 给出 `fixAvailable: {version: 15.5.29, isSemVerMajor: false}`。注意 `next` 与 `eslint-config-next` 都被精确钉在 15.5.25，升级需同步两处 |
| `sharp`（next 的 optionalDependency，生产） | 0.35.4 | **high** | GHSA-wq5f-xc86-pv6w：librsvg 依赖 CVE-2026-96889 use-after-free（影响 <0.35.5） | **有**。`fixAvailable: true`，修复版本 0.35.5（0.x 内 patch），lockfile 内可非破坏升级 |
| `follow-redirects`（经 axios 引入，生产链） | 1.16.0 | **high** | GHSA-q3jh-wrc6-jgq8、GHSA-8r9p-f939-6h3c（均 <1.16.1，凭据头/跨域凭据剥离问题）；GHSA-mrwp-f3q8-3g9p（1.16.0–1.16.1，CVSS 5.9，自定义凭据随重定向泄露） | **有**。`fixAvailable: true`，修复区间 ≥1.16.2，patch 级。父依赖为 `axios ^1.9.0` |
| `brace-expansion`（dev 工具链：minimatch ← eslint / typescript-eslint） | 1.1.18（根）+ 5.0.9（@typescript-eslint/typescript-estree 嵌套） | **high** | GHSA-qhr7-859c-m2p7（<1.1.20 或 4.0.0–5.0.11，嵌套花括号栈耗尽 DoS，CVSS 7.5）；GHSA-6j4f-fj2g-mc7p（<1.1.19 或 4.0.0–5.0.10，CVSS 7.5） | **有**。`fixAvailable: true`，1.x 线升至 1.1.21、5.x 线升至 5.0.12，均非 semver-major。仅 lint 链，不进运行时 |
| `source-map-js`（dev 工具链：postcss ← @tailwindcss/node） | 1.2.1 | **high** | GHSA-68fv-2mgg-jv7q：索引式 source-map 段偏移导致事件循环 DoS（1.0.0–1.2.1，CVSS 7.5） | **有**。`fixAvailable: true`，修复版本 1.2.2（patch）。构建期工具链 |

补充事实：

- critical 的 RCE（GHSA-2464-fhr4-w9xw）针对**自托管的 Image Optimization 运行时**；本项目主构建路径是静态导出（`npm run build:export`，`NEXT_OUTPUT_EXPORT=1`），导出产物不含图片优化服务器，`package.json` 里仍保留 `next start` 脚本——若以 `next start` 方式部署则暴露面存在。
- `follow-redirects` 只被 axios 的 Node 适配器使用，浏览器端 bundle 不包含该路径；但按依赖清单它属生产依赖，计入高危表。

### 1.2 后端 apps/control（pip-audit 2.10.1，2026-11-04 实跑）

清单输入：`requirements.txt`（fastapi==0.115.6 / uvicorn[standard]==0.34.0 / httpx==0.28.1 / PyYAML==6.0.3）+ `requirements-dev.txt`（pytest>=8.0 / pytest-asyncio>=0.23）。仓内**无 pyproject.toml**（全仓 glob 无匹配），故只按 requirements 解析。
[体检当时的清单快照，原文保留] 当前已对齐 fastapi==0.162.0 / starlette==1.3.1，且 `requirements.txt` 已改为全钉编译产物，见第 5 节。

- 漏洞总数报「14 known vulnerabilities in 1 package」：14 条是 **7 个唯一漏洞 × 2**（同一 PYSEC 各出现两次，一条带 GHSA 别名、一条带 CVE 别名）。
- 唯一有漏洞的包是 **starlette 0.41.3**（fastapi==0.115.6 的传递依赖；pip-audit 解析到 0.41.3 说明 fastapi 0.115.6 对 starlette 有 `<0.42.0` 上界约束）。fastapi / uvicorn / httpx / PyYAML 及其余 20 个解析出的传递包 **0 漏洞**。
- 级别判定：pip-audit JSON 不含 severity，以下级别逐条取自 OSV API（GHSA 的 `database_specific.severity`），非推测。

| 唯一漏洞 | 级别（OSV） | 影响 | 修复版本（pip-audit fix_versions） |
| --- | --- | --- | --- |
| GHSA-7f5h-v6xp-fcq8 / CVE-2025-62727 | **HIGH** | 畸形 `Range` 头触发 `FileResponse` 解析 O(n²) CPU 耗尽（StaticFiles / 任何 FileResponse 端点） | 0.49.1 |
| GHSA-wqp7-x3pw-xc5r / CVE-2026-48818 | **HIGH** | Windows 下 StaticFiles 收到 UNC 路径触发出站 SMB，泄露服务账户 NTLMv2 哈希（SSRF）；POSIX 不受影响 | 1.1.0 |
| GHSA-82w8-qh3p-5jfq / CVE-2026-54283 | **HIGH** | `application/x-www-form-urlencoded` 解析忽略 `max_fields`/`max_part_size`，单请求即可阻塞事件循环或撑爆内存 | 1.3.1 |
| GHSA-2c2j-9gv5-cj73 / CVE-2025-54121 | moderate | multipart 大文件落盘时阻塞主线程 | 0.47.2 |
| GHSA-86qp-5c8j-p5mr / CVE-2026-48710 | moderate | `Host` 头未校验 → `request.url.path` 与真实路由路径不一致，可绕过基于 URL 的授权 | 1.0.1 |
| GHSA-x746-7m8f-x49c / CVE-2026-48817 | moderate | `HTTPEndpoint` 用 getattr 按方法名分发，未限制 HTTP 动词（需特定路由写法才可利用） | 1.1.0 |
| GHSA-jp82-jpqv-5vv3 / CVE-2026-54282 | low | 不以 `/` 开头的路径使 `request.url.hostname` 可被诱导（路由 404，暴露面在中间件） | 1.3.0 |

**修复路径（pip 解析器实测，非记忆）：**

1. `pip install --dry-run "fastapi==0.115.6" "starlette==1.3.1"` → **ResolutionImpossible**：当前钉版下 starlette 无任何可用修复版本（三个 HIGH 的最低修复要求 0.49.1 / 1.1.0 / 1.3.1，全部越过 `<0.42.0` 上界）。
2. `pip install --dry-run "fastapi" "starlette==1.3.1"` → 解析成功，**Would install fastapi-0.162.0 starlette-1.3.1**。
3. 结论：**已升级并回归**（2026-11-04 更新）。已按上述解析路径把 `fastapi` 升到 0.162.0 并显式钉 `starlette==1.3.1`，全量 `pytest tests/ -q` 结果 611 passed / 1 skipped / 0 failed（171.42s，唯一 skip 为 Windows POSIX 权限位），三个 HIGH 全部消除。锁文件同样钉 `starlette==1.3.1`，与本结论对齐。
   历史风险评估（保留供追溯）：原结论为**无非破坏性修复路径**——要修 3 个 HIGH 必须连带 fastapi 0.115.6 → 0.162.0（跨越约 46 个 0.x minor，FastAPI 0.x 不保证向后兼容），需完整回归 `/v1/*` 与 `/api/*` 全部路由，当时按要求不执行。

> `pip list --outdated` 未采用：它只比对已安装环境的版本、不给漏洞与修复版本信息；且体检要求装在临时环境，比对目标本身不存在。能力可用的 `pip-audit` 为准。

### 1.3 前端 14 个 moderate：接受并留档（2026-11-04 处置）

**14 行不是 14 个漏洞**：实为 **1 个真实 advisory + 13 行 `via` 级联**。真实项是 `minimatch` 的 **GHSA-g3hv-75wg-m249**（glob 模式被放大时的 DoS，CVSS:4.0，仅可用性影响，无机密性/完整性风险）；其余 13 行是各自父包在 audit 里指向 minimatch 的 `via` 级联计数，不构成独立漏洞。影响面：14 行全部 dev-only（eslint / @typescript-eslint / tailwind 构建链），只在构建期执行；npm audit 的生产依赖面（production 口径 192 包）0 漏洞；运行镜像只 `COPY out/`，静态产物不含该代码路径。

**为什么不可修（三条路径均实证阻断，可安全升级数 = 0）：**

1. **沿现有大版本升级**：minimatch v3 线末版 3.1.5、v10 线末版 10.2.6，均仍在受影响范围；修复起点是 11.0.1，对 3.x / 10.x 没有回移版本。
2. **等链头上游迁版**：eslint 10 的依赖声明仍是 `minimatch ^10.2.6`，未迁到 ≥11，升级链头没有出口。
3. **硬钉 minimatch@11**：`@eslint/eslintrc` 源码用 `import minimatch from "minimatch"` 的 default import，而 v11 是纯 ESM、无 default 导出，eslint 启动期直接 SyntaxError（只读评估实测结论）。

**接受理由**：dev-only 且仅构建期执行，产物与运行镜像零暴露；该 DoS 需要攻击者控制 glob pattern，而 pattern 全部来自手写的 eslint 配置文件，无外部输入通路；CVSS 4.0 只影响可用性；生产依赖 0 漏洞。综合判定为可接受的残留风险，按「接受并留档」处置，不改 `package.json`、`package-lock.json` 与 `Dockerfile`。

**复查触发条件**（满足任一即重新评估）：

- `@eslint/eslintrc` 移除 default import（改为命名导入或 ESM 兼容引用），硬钉 11 的启动期阻断消失；
- 任一上游（eslint / eslint-config-next / `@typescript-eslint/*`）把 minimatch 提到 ≥11；
- npm audit 为 minimatch 报出 ≤10 线的修复版本（出现回移）。

---

## 2. 许可证一致性核对

### 2.1 实际检查的文件

| 文件 | 检查内容 | 结果 |
| --- | --- | --- |
| `LICENSE` | 署名 | MIT，`Copyright (c) 2026 addxiaoyi2048` |
| `apps/web/package.json` | license 字段、name | **无 `license` 字段**（仅 name/version/private）；name=`unified-2api-web` |
| `apps/web/package-lock.json` | 全部 555 个包的 license 字段（prod 228 条全部有字段） | 见 2.3 |
| `apps/control/requirements.txt`、`requirements-dev.txt` | 依赖清单 | 只有版本约束，无许可证元数据（pip 清单常态） |
| `apps/control/pyproject.toml` / 根 `package.json` | 署名一致性 | **均不存在**（全仓 glob 无 pyproject.toml）——三处声明只有 LICENSE 一处有 license + 署名 |
| `THIRD-PARTY-NOTICES.md` | 署名、上游逐家许可证 | 与 LICENSE 一致（第 5、128 行均写 `addxiaoyi2048`）；11 家上游证据齐全 |
| `upstreams/README.md` | 四档分层定义 | MIT 7 家可入主仓 / GPL-3.0 opencode2api + LGPL-3.0 sub2api 进程隔离 / PolyForm-NC qoder-switch 禁商用 / 无 LICENSE Deepseek2Go 禁商用 |
| `docs/ADDING-A-PROVIDER.md:88` | risk_level 语义 | `safe`(MIT/Apache/BSD) / `warn`(GPL 类) / `danger`(无 LICENSE 或禁商用)，与四档分层同构对齐 |
| `apps/control/app/adapters/{opencode,sub2api,qoder,qoder_sidecar,deepseek_sidecar}.py` | 上游 header 残留 | grep `Copyright|SPDX|GPL|@license|adapted from|derived from`：**无上游版权头**，命中仅为自身 `risk_note`/`license=` 合规声明 |
| 全仓 `.go` / `go.mod` | GPL 上游（Go 项目）源码混入 | **0 个** |
| `providers/{opencode2api,sub2api,qoder-switch,deepseek2go}` | 是否 vendor 上游源码 | 仅 `README.md` + 可选 `docker-compose.fragment.yml`，**无源码无二进制** |
| vendor 目录（`vendor|third_party|external` glob） | 代码搬运痕迹 | 仅 `.runtime/python/.../httptools.../vendor/`（本地 Python 运行时内 httptools 自带的 llhttp/http-parser 许可文件）；`.runtime/` 与 `apps/web/node_modules` **均未被 git 跟踪**（`git ls-files` 计数 0），不属分发物 |

### 2.2 三个重点结论

1. **无 GPL/LGPL/PolyForm-NC 源码复制**：opencode.py / sub2api.py / qoder.py 中出现的上游仓库名仅是 `vendor="jasonxu114514/opencode2api（协议参考）"` 这类**归属标注字符串**（adapters/opencode.py:27、sub2api.py:45、qoder_sidecar.py:22、deepseek_sidecar.py:25），非代码；0 个 Go 文件、0 个 vendor 源码目录、0 个上游版权头。与 `THIRD-PARTY-NOTICES.md` 第 3、4 节的 clean-room 声明相互印证。
2. **署名一致性**：LICENSE ✓、THIRD-PARTY-NOTICES.md ✓ 均为 `addxiaoyi2048`；**package.json 缺 `license` 字段、pyproject.toml 不存在**——不是矛盾，是两处声明缺失，属待补项。
3. **依赖中无 GPL/AGPL 静态链接进分发物**：555 个 npm 包 license 全量扫描，GPL/AGPL/SSPL/BUSL/PolyForm/NC 命中 **0**。存在两处需注意的宽松但带义务的许可（均不构成阻断）：
   - **LGPL-3.0-or-later（10 + 4 个）**：`@img/sharp-libvips-*` 平台二进制与 `@img/sharp-win32-*`（`Apache-2.0 AND LGPL-3.0-or-later`），是 sharp（next 的 optionalDependency）的原生库，仅构建期使用；`node_modules` 不进 git、静态导出 `out/` 只含打包后的 JS/CSS，**当前不触发 LGPL 分发义务**。若未来把 `node_modules` 或含它的构建镜像对外分发，需提供其源码获取方式与替换许可。
   - **CC-BY-4.0（1 个）**：`caniuse-lite`（browserslist 浏览器兼容数据，构建期查询用）。其数据不以可识别形式进入导出产物；对外分发含其数据的构建元数据时需署名。
   - Python 侧 requirements 解析出的 22 个包均为宽松许可族（MIT/BSD/Apache/PSF），无 copyleft 命中。

### 2.3 生产依赖许可证分布（apps/web lock，非 dev 条目）

MIT 177 ・ Apache-2.0 16 ・ ISC 15 ・ LGPL-3.0-or-later 10 ・ `Apache-2.0 AND LGPL-3.0-or-later` 3 ・ BSD-3-Clause 3 ・ 0BSD 1 ・ `Apache-2.0 AND LGPL AND MIT` 1 ・ CC-BY-4.0 1 ・ `MIT AND ISC` 1；缺字段 0。

---

## 3. 修复建议与优先级（仅建议，本次未执行任何升级）

| 优先级 | 对象 | 动作 | 依据 / 风险 |
| --- | --- | --- | --- |
| **P0** | `next` 15.5.25 → 15.5.29 | 同步改 package.json 的 `next` 与 `eslint-config-next` 两个精确钉版，重新 `npm install` 出 lock | 唯一 critical（RCE）+ high；npm 明示非 major 修复。部署形态为静态导出时 RCE 暴露面低，但 `next start` 脚本仍在，且 Server Actions DoS 与缓存投毒同版本修复 |
| **P1** | `starlette` 0.41.3（连带 fastapi 0.115.6 → 0.162.0） | 单独立项：升级 + 全路由回归（`/v1/*`、`/api/*`、SSE 流式） | 3 个 HIGH，无同约束内修复（实测 ResolutionImpossible）。跨 46 个 0.x minor，是后端风险最大的一次依赖变更；Windows NTLM 泄露项仅 Windows 部署受影响 |
| **P1** | `sharp` 0.35.4 → 0.35.5、`follow-redirects` 1.16.0 → ≥1.16.2 | `npm update sharp follow-redirects`（lockfile 内非破坏） | 均为生产链 high 且 patch 级修复；follow-redirects 走 axios 链，sharp 走 next optional |
| **P2** | `brace-expansion`、`source-map-js` | `npm audit fix`（不带 `--force`）可非破坏解决 | 仅 dev/lint/构建链，不进运行时与产物 |
| **P2** | 许可证补齐 | package.json 增加 `"license": "MIT"`（与 LICENSE 对齐）；若后端将来需要打包元数据再补 pyproject.toml 并写 `license`/`authors` | 消除「三处声明只有一处存在」的缺口，纯增量无代码影响 |
| **P3** | LGPL / CC-BY 分发边界 | 在对外分发文档中沿用 `THIRD-PARTY-NOTICES.md` 第 6 节模板，补一句 sharp-libvips（LGPL）与 caniuse-lite（CC-BY）「仅构建期、不分发二进制」的说明 | 非阻断，属声明完备性 |

---

## 4. 本次未覆盖的范围

- **未升级、未修改** `package-lock.json` / `package.json` / `requirements*.txt` 及任何代码；未执行 git 写操作。（该表述对应体检当时；2026-11-04 后续收口把 `requirements*.txt` 改为编译锁文件，见第 5 节，`package.json` / `package-lock.json` 至今原样。）
- moderate / low 未列入主表：前端 14 个 moderate、后端 starlette 4 个 moderate + 1 个 low 已在第 1 节列出明细但不计入高危数量。
- 未扫描 `.runtime/python`（本地嵌套 Python 运行时，git 未跟踪）与 `apps/web/node_modules` 内**已安装但不在 lock 解析面**的包；npm/pip 审计均基于清单解析。
- 未做许可证的传递性法律判断（如 LGPL 在具体分发形态下的合规方案），结论以文件证据为准。
- 11 家上游 sidecar 镜像内部的依赖与许可证未扫（不在主仓分发物内，按 `upstreams/README.md` 由部署方自担）。
- CVE/GHSA 详情以外未抓取任何网页内容；所有版本号、漏洞编号、修复版本均来自 npm audit / pip-audit / OSV API / pip 解析器本次实跑输出。

---

## 5. Python 锁文件与再生成方式（2026-11-04 补）

`apps/control` 的依赖清单从「直接依赖钉版 + 传递依赖每次构建重新解析」改为 pip-tools 路线的 `.in` 源清单 + 全钉锁文件：

| 文件 | 作用 |
| --- | --- |
| `requirements.in` | 源清单，手工维护：6 个直接依赖（fastapi / starlette / uvicorn[standard] / httpx / PyYAML / cryptography），保留原注释与顺序 |
| `requirements.txt` | 编译产物，26 个包全钉，`pip install -r` 直接消费 |
| `requirements-dev.in` | 源清单：`-r requirements.in` + pytest / pytest-asyncio |
| `requirements-dev.txt` | 编译产物，32 个包全钉（运行时 26 + pytest 链 6） |

**再生成命令**（必须按目标平台编译；在 Windows 本机直接 `pip-compile` / `uv pip compile` 会漏掉 `uvloop` 的 `sys_platform != 'win32'` marker 并多带 Windows-only 的 `colorama`）：

```bash
uv pip compile --python-platform x86_64-unknown-linux-gnu --python-version 3.11 --no-strip-markers apps/control/requirements.in -o apps/control/requirements.txt
uv pip compile --python-platform x86_64-unknown-linux-gnu --python-version 3.11 --no-strip-markers apps/control/requirements-dev.in -o apps/control/requirements-dev.txt
```

- 编译环境：uv 0.13.7，目标 Python 3.11 / `x86_64-unknown-linux-gnu`（对齐 `apps/control/Dockerfile` 的 `python:3.11-slim`），首次生成日期 2026-11-04，2026-11-05 因新增 `cryptography` 按同一命令重新生成（两份锁都以旧产物为 preference，除加密三项外版本未漂移）；`--no-strip-markers` 用于保留环境标记，否则 uv 会按目标平台把 marker 求值后剥掉。
- 关键行：`uvloop==0.23.0 ; platform_python_implementation != 'PyPy' and sys_platform != 'cygwin' and sys_platform != 'win32'`；`cffi==2.1.1 ; platform_python_implementation != 'PyPy'`、`pycparser==3.12 ; implementation_name != 'PyPy' and platform_python_implementation != 'PyPy'`（cryptography 的传递依赖，marker 只排除 PyPy，Windows 开发机与 Linux 镜像都会装）；`colorama` 因目标平台为 linux 未进入锁文件，没有任何 Windows-only 包被无条件锁定。
- 验证（2026-11-04 首次生成时，全部 `pip install --dry-run`，未往任何运行环境安装）：本机 Python 3.11.9 对 prod / dev 两份锁解析成功；`python:3.12-slim` 容器（对齐 CI 的 3.12 口径）同样解析成功；`python:3.11-slim` 容器确认 `uvloop-0.23.0` 正常纳入、`Would install` 全量列出当时 23 / 29 个包且无报错。
- **2026-11-05 重新生成并实装**：本机 Python 3.11.9 按锁装上 `cryptography==50.0.2` / `cffi==2.1.1` / `pycparser==3.12`，`backup_crypto.backend_info()` 与 `update_signing.backend_info()` 均返回 `implementation=lib`、`library_version=50.0.2`；全量 `python -m pytest -q` 为 `1045 passed, 1 skipped`（唯一 skip 是 Windows POSIX 权限位的 `[env]` 项），`scripts/check_no_bad_skip.py` 退出码 0。这解释了为什么零 skip 门在未装库的环境会红：互通用例的 `skipif` 代码路径保留，但按依赖声明准备的环境里不会触发。
- **2026-11-05 复跑（本批新增远端备份用例后）**：同一台机器、同一份锁，全量 `python -m pytest -q` 为 `1051 passed, 1 skipped, 3 subtests passed`（661.15s），唯一 skip 仍是 `test_hardening.py:99` 那条带 `[env]` 的 win32 用例；`scripts/check_no_bad_skip.py apps/control/pytest-full.log` 退出码 0。**与上一段的 1045 对不上**：`test_backup_remote.py` / `test_s3_sigv4.py` / `test_backup_schedule.py` 单跑是 `45 passed`，若 1045 是同一工作树的数，加上它们应为 1090。所以 1045 只能当作那次时点的记录，不能当作「本批之前的基线」来推算增量；以本次 1051 为准。
- `Dockerfile` 与 CI 不改：`pip install -r requirements.txt` 对全钉文件无感知；CI 的 pip 缓存 key 已按 `requirements*.txt` 内容变化自动失效（`.github/workflows/ci.yml:23`）。
- 改动仅限 `.in` 源清单与两份锁文件；`package.json` / `package-lock.json` / Dockerfile 未动，未执行任何 git 写操作。
