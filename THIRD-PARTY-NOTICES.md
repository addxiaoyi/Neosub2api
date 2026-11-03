# 第三方组件与许可证清单

> 收录本仓参考 / 代理 / 重写的 **11 个上游仓库**。数据以 [`upstreams/upstreams.yaml`](upstreams/upstreams.yaml) 与各上游仓库 LICENSE 文件为准，本文件跟随更新。
> 主仓自身为 MIT（见根目录 [LICENSE](LICENSE)）。集成形态两种：
> - **主仓自研（clean-room）**：只按公开协议契约重写，不含上游代码，可直接商用分发
> - **sidecar 直通**：上游跑独立进程，主仓只发 HTTP，不做编译期/链接期依赖
>
> 「风险等级」：safe = MIT/Apache/BSD 等宽松许可；danger = 无 LICENSE 或禁止商用，商用前必须移除或取书面授权。

---

## 一、逐家清单（11 家）

| # | 上游仓库 | 许可证 | license_source | 集成形态 | 主仓对应实现 | 风险 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | `addxiaoyi/codebuddy2api` | MIT | LICENSE 文件 | sidecar 直通 | `adapters/codebuddy.py`（代理 7864 管理端） | safe |
| 2 | `czg86389-hub/muse2api` | MIT | LICENSE 文件 | sidecar 直通 | `adapters/muse.py`（7865） | safe |
| 3 | `kaibush/grok-register` | MIT | LICENSE 文件 | 主仓自研 | `adapters/grok_register.py` + `services/grok_register.py` | safe |
| 4 | `jasonxu114514/opencode2api` | GPL-3.0 | LICENSE 文件 | 主仓自研（clean-room，规避传染） | `adapters/opencode.py`（直连 Zen/Go） | safe |
| 5 | `addxiaoyi/gptGrok2api` | MIT | LICENSE 文件 | sidecar 直通 | `adapters/gptgrok.py`（7867） | safe |
| 6 | `addxiaoyi/ZCode2api` | MIT | LICENSE 文件 | sidecar 直通 | `adapters/zcode.py`（8787） | safe |
| 7 | `Wei-Shaw/sub2api` | LGPL-3.0 | LICENSE 文件 | 主仓自研（clean-room，不链接上游库） | `adapters/sub2api.py` + `sub2api_store.py` | safe |
| 8 | `JeffHu0912/trae2api` | MIT | LICENSE 文件 | sidecar 直通 + 主仓自研 | `adapters/trae_sidecar.py`（7871）；`adapters/trae.py` 为自研直连 Solo/Work | safe |
| 9 | `ColudAI/Deepseek2Go` | **无 LICENSE**（All Rights Reserved） | 未声明 | sidecar 直通 | `adapters/deepseek_sidecar.py`（5001） | **danger** |
| 10 | `NextAgentX/qoder-trae-workbuddy-switch` | **PolyForm-Noncommercial-1.0.0** | LICENSE 文件 | sidecar 直通 + 主仓自研 | `adapters/qoder_sidecar.py`（7873）；`adapters/qoder.py` 为自研 COSY 直连 | **danger**（sidecar 部分） |
| 11 | `AiXiaoc/codar2api` | MIT | LICENSE 文件 | sidecar 直通 | `adapters/codar_sidecar.py`（8801） | safe |

> 条目 1 备注：`addxiaoyi/codebuddy2api` 的 Go 数据面曾由 `Sliverkiss/workbuddy2api` 提供；该仓库现已不可访问（GitHub 404），故不作为独立上游登记，只按 codebuddy2api 一条记录。

## 二、按集成形态归类

### 1. 主仓自研（MIT，主仓直接分发）

| 上游参考 | 上游许可证 | 主仓实现 | clean-room 边界 |
| --- | --- | --- | --- |
| opencode2api | GPL-3.0 | `adapters/opencode.py` | 只取端点/鉴权/SSE/错误码契约，函数与命名全新 |
| sub2api | LGPL-3.0 | `adapters/sub2api.py` + `sub2api_store.py` | 只取分组/配额/429 语义，落库模型自设计 |
| grok-register | MIT | `services/grok_register.py` + `routers/grok.py` | 只取任务字段/状态机语义，编排全新 |
| trae2api | MIT | `adapters/trae.py` | 只取 Solo/Work 协议事实，头构造与改写逻辑自写 |
| qoder-trae-workbuddy-switch | PolyForm-NC | `adapters/qoder.py` | 只取 COSY 协议事实，加密原语全部标准库自实现 |

> 这五条路径**不依赖外部进程**即可运行（`trae_sidecar` / `qoder_sidecar` 仍在，但只是可选替代）。

### 2. sidecar 直通（上游跑在别处，主仓只发 HTTP）

| 上游 | 形态 | 端口 | 是否随仓分发 |
| --- | --- | --- | --- |
| codebuddy2api（7864 管理端 + 内置 Go 数据面 7863） | 独立进程，HTTP 代理 | 7863 / 7864 | 否 |
| codar2api | sidecar，HTTP 透传 | 8801 | 否 |
| muse2api | sidecar，HTTP 代理 | 7865 | 否 |
| gptGrok2api | sidecar，HTTP 代理 | 7867 | 否 |
| ZCode2api | sidecar，HTTP 代理 | 8787 | 否 |
| trae2api | sidecar，HTTP 代理 | 7871 | 否 |
| Deepseek2Go | sidecar，HTTP 代理 | 5001 | 否 |
| qoder-trae-workbuddy-switch | sidecar，HTTP 代理 | 7873 | 否 |

---

## 三、商用前检查清单

### 3.1 必须移除或取得书面授权

- [ ] **Deepseek2Go**（#9）：仓库无 LICENSE → 默认 All Rights Reserved。商用发布前移除 `adapters/deepseek_sidecar.py`、compose 服务与 `.env` 条目，或取得作者书面授权 + 作者补 LICENSE。
- [ ] **qoder-trae-workbuddy-switch 的 sidecar**（#10）：PolyForm Noncommercial 1.0.0 明确禁商用（含内部商业项目与托管服务）。移除 `adapters/qoder_sidecar.py` 与 7873 服务，或取得 NextAgentX 书面商用授权；**`adapters/qoder.py`（自研 COSY 直连）不受此限制**，可保留。
- [ ] 清理 `docker-compose.yml`、`.env.example`、`config/prices.yaml` 中对应 danger 项的残留条目（避免「移除适配器但服务仍在」）。
- [ ] 确认自研路径与上游无实质性相似：保留本仓提交历史作为独立创作证据。

### 3.2 可以保留

- [ ] 主仓全部代码与 `apps/control` 内置适配器（MIT）。
- [ ] 五条 clean-room 路径（opencode / sub2api / grok-register / trae / qoder）——不链接 GPL/LGPL/PolyForm-NC 上游代码。
- [ ] MIT 的 sidecar 引用（codebuddy2api、muse2api、gptGrok2api、ZCode2api、trae2api、codar2api）——仅按 HTTP 地址调用。
- [ ] `providers/*/README.md` 的参考来源说明与 `docs/providers/*.md` 契约文档（只写协议事实，不贴源码）。

### 3.3 sidecar 直通的合规前提

- [ ] **进程间 HTTP**：主仓与 sidecar 只通过 `http://host:port` 通信，不共享内存、不加载对方的 `.so` / `.dll` / `.wasm`。
- [ ] **不链接**：主仓依赖清单（Python requirements / Go go.mod / 前端 package.json）中不出现上游仓库或其 fork。
- [ ] **不拷贝**：仓库树内没有上游源码副本；`providers/*/README.md` 只记录 clone 命令与端口，不存放代码。
- [ ] **不修改**：需要定制时改主仓适配器，不改上游文件；上游升级走「替换外部目录」而不是打补丁。
- [ ] **不再分发**：容器镜像 / 发行包中不打包上游二进制；`docker-compose.yml` 只引用网络地址。
- [ ] **许可证随行**：若最终产品仍需分发 sidecar（如自托管镜像），必须在镜像内附带对应 LICENSE 文本与版权声明。

### 3.4 每次新增 provider 时的检查

1. 先在 `upstreams/upstreams.yaml` 记录 `license` / `license_source` / `integration` / `analyzed_commit`。
2. 再写 `docs/providers/<name>.md` 契约（只写协议事实）。
3. 同步本文件对应行；`license` 不确定时写「未核实」，不要猜。
4. 若 `license` 为 GPL/LGPL/PolyForm-NC/未声明，默认只能 sidecar 或 clean-room，不得拷贝源码。

---

## License URLs

| 仓库 | 许可证链接 |
| --- | --- |
| addxiaoyi/codebuddy2api | https://github.com/addxiaoyi/codebuddy2api/blob/main/LICENSE |
| czg86389-hub/muse2api | https://github.com/czg86389-hub/muse2api/blob/main/LICENSE |
| kaibush/grok-register | https://github.com/kaibush/grok-register/blob/main/LICENSE |
| jasonxu114514/opencode2api | https://github.com/jasonxu114514/opencode2api/blob/main/LICENSE |
| addxiaoyi/gptGrok2api | https://github.com/addxiaoyi/gptGrok2api/blob/main/LICENSE |
| addxiaoyi/ZCode2api | https://github.com/addxiaoyi/ZCode2api/blob/main/LICENSE |
| Wei-Shaw/sub2api | https://github.com/Wei-Shaw/sub2api/blob/main/LICENSE |
| JeffHu0912/trae2api | https://github.com/JeffHu0912/trae2api/blob/main/LICENSE |
| ColudAI/Deepseek2Go | **无 LICENSE 文件**（仓库根目录无授权声明） |
| NextAgentX/qoder-trae-workbuddy-switch | https://github.com/NextAgentX/qoder-trae-workbuddy-switch/blob/main/LICENSE |
| AiXiaoc/codar2api | https://github.com/AiXiaoc/codar2api/blob/main/LICENSE |
