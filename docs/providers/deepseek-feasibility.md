# DeepSeek PoW 可行性验证（第一阶段，未实现）

> 结论先行，证据在后。本轮**只做只读验证，没有写任何实现代码**。
> 本文件取代 `docs/providers/deepseek2go-feasibility.md` 中「需逆向且不确定」的旧表述，
> 补充了对 `module.wasm` 的黑盒实测结果。

## 结论：C 无法判定

**一句话理由**：WASM 里的 `wasm_deepseek_hash_v1` 实测输出**不等于任何标准哈希的明文结果**，
说明 challenge → answer 的派生层被编译进了二进制，而缺少真实 challenge（要账号）就无法验证自研替代。

工程处置上按 **B（继续 sidecar）** 执行：在拿到真实凭证并完成验证前，不新建自研 adapter。

---

## 依据

### 1. PoW 是 completion 的硬前置（无「免 PoW」路径）

| 观测 | 位置 |
| --- | --- |
| 申请挑战：`POST /api/v0/chat/create_pow_challenge`，body `{"target_path":"/api/v0/chat/completion"}` | `main.go:545-549` |
| 挑战字段：`algorithm/challenge/salt/difficulty/expire_at/signature/target_path` | `main.go:376-384` |
| prefix 固定拼法 `"{salt}_{expire_at}_"` | `main.go:470` |
| 回传头 `x-ds-pow-response = base64(json{algorithm,challenge,salt,answer,signature,target_path})` | `main.go:578-593` |
| OpenAI 路径每次 completion 前都取 PoW | `main.go:845-854` |
| Anthropic 路径同样每次取 PoW（用户自带 Token 模式也走这里） | `main.go:1231-1243` |

### 2. WASM 来源：本地文件，不是远程下发

- 配置项 `wasm_path`，默认 `sha3_wasm_bg.7b9ca65ddd.wasm`：`main.go:47`、`main.go:118`、`main.go:133`
- `os.ReadFile(path)` + Wazero `Instantiate`，运行期没有任何网络取模块的路径：`main.go:412-425`
- 仓库自带 `module.wasm`（26,612 B），`example.config.json` 指向它。上游**无 LICENSE 文件**（目录内只有 `.gitignore/.github/cmd/build.sh/Dockerfile/example.config.json/go.mod/module.wasm/README.md`），按 All Rights Reserved 处理。

### 3. 导出函数签名（解析 type/function/export 段得到，非推测）

| 导出 | 签名 |
| --- | --- |
| `wasm_solve` | `(i32, i32, i32, i32, i32, f64) -> ()`，调用方按 `(retptr, challenge_ptr, challenge_len, prefix_ptr, prefix_len, difficulty)` 传参 |
| `wasm_deepseek_hash_v1` | `(i32, i32, i32) -> ()`，按 wasm-bindgen 约定 `(retptr, ptr, len)`，返回值为内存中的 `[ptr,len]`，实测 `len=64`（64 个 hex 字符） |
| `__wbindgen_add_to_stack_pointer` | `(i32) -> i32` |
| `__wbindgen_export_0` | `(i32, i32) -> i32`（分配器，`(size, align)`） |

- 模块**没有 import 段**（自包含，无法在运行期取外部数据）。
- 结果读取方式（`retptr` 处 `i32` 状态 + `retptr+8` 处 `f64`）：`main.go:520-542`

### 4. 关键实测：WASM 的 hash 不是「明文的 SHA3-256」

在仓库外的临时目录用 Node 原生 `WebAssembly` 加载同一个 `module.wasm`，按上面的 ABI 调用，
结果如下（每组同输入重复调用，结果稳定）：

| 输入 | `wasm_deepseek_hash_v1` 输出 | 标准 `SHA3-256` | 是否相等 |
| --- | --- | --- | --- |
| `""` | `e594808bc5b7151ac160c6d39a02e0a8e261ed588578403099e3561dc40c26b3` | `a7ffc6f8bf1ed76651c14756a061d662f580ff4de43b49fa82d80a4b80f8434a` | 否 |
| `hello` | `50605e468e6d6ead913d7d7ccc4687b83ded157cf0a0c5e011eefece12712fa5` | `3338be694f50c5f338814986cdf0686453a888b84f424d792af4b9202398f392` | 否 |
| `abc` | `f841106c601ce9be9bc38525e90d4178d47f21dd8eb9f238fc55ffaa4ca94506` | `3a985da74fe225b2045c172d6bd390bd855f086e3e9d525b46bfe24511431532` | 否 |

补充穷举：`sha1 / sha3-224/256/384/512 / shake128 / shake256 / blake2b / blake2s / sm3 / sha512-256`、
UTF-16LE/BE、前后补零、常见前后缀（`deepseek`、`DeepSeekHashV1`、`sha3`、`v1` 等）**全部不匹配**。

含义：data 段里的 `'sha3-wasm/src/lib.rs'` 只能证明「内核用了 Rust sha3 crate」，
**不能**证明「导出函数就是 sha3-256(输入)」——两者之间还有一层我们读不到的派生/编码。
这正是旧文档中「wrapper 逻辑无法静态读出」的具体化，而且现在有了可复现的反证。

### 5. `wasm_solve` 黑盒也拿不到答案

每次用**全新实例**、按 Go 的调用顺序 `(retptr, challenge, prefix, difficulty)` 调用，
difficulty 取 `1 / 10 / 1000 / 100000 / 1000000`，合成的 challenge/prefix（`abcdef0123456789`、`salt_1700000000_`）：

- 结果全部是 `tag=0`（即 `None`，Go 侧会当成失败返回）；
- 一旦某次调用触发 `unreachable`（参数顺序不对时），该实例后续调用会持续 trap，说明 panic 后模块状态不可复用；
- 无法从合成输入得到任何 `answer`，也就无法用「已知 answer → 验证 hash 预期」这条黑盒路径反推派生逻辑。

### 6. 仓内现状（代理形态与端口）

- 适配器只是 HTTP 转发，Anthropic 路径要重写成 `/anthropic/v1/messages`：[deepseek_sidecar.py:47-51](file:///D:/qwq/%E9%A1%B9%E7%9B%AE/unified-2api/apps/control/app/adapters/deepseek_sidecar.py#L47-L51)
- 仓内默认 base URL 是 `http://127.0.0.1:5001`：[config.py:81](file:///D:/qwq/%E9%A1%B9%E7%9B%AE/unified-2api/apps/control/app/config.py#L81)、[config.py:109](file:///D:/qwq/%E9%A1%B9%E7%9B%AE/unified-2api/apps/control/app/config.py#L109)
- 上游默认端口是 `5001`（`PORT` 可覆盖）：`main.go:1396-1399`
  → 两处默认值已对齐（仓内早期版本为 7872；从旧版升级且 .env 里写过 7872 的，需同步改为 5001 或给 sidecar 设 `PORT=7872`）。
- 上游 Anthropic 流式未实现（直接 501）：`main.go:1237-1241`
- 路由表：`/v1/models`、`/v1/chat/completions`、`/anthropic/v1/models`、`/anthropic/v1/messages`：`main.go:1373-1377`

---

## 为什么不是 A

- A 要求「WASM 只是加速实现、算法等价于已知 hash 挑战」。实测已经否掉了最基础的等价关系（§4）。
- 只有内核（SHA3/Keccak-f[1600]）是公开标准；nonce 编码、拼接位置、比较符号、难度语义全部在 WASM 里，没有公开规范。
- 即使今天逆出来，上游换一个 WASM 版本（文件名自带 hash `7b9ca65ddd`）就可能失效，协议没有版本协商。

## 为什么也不判 B（严格意义）

- B 的语义是「**必须**依赖上游 WASM」，需要证明不可独立实现。
- 本轮只证明了「现有证据不足以独立实现」，没有排除「继续反汇编 wrapper + 用真实 challenge 动态验证」这条路。
- 所以判定停在 C；运维动作上与 B 一致（继续 sidecar，二进制不进仓）。

---

## 走到 A 需要什么（按顺序，缺一不可）

1. **真实账号凭证**：拿一个可用的 DeepSeek token（或邮箱/密码），实际请求一次 `create_pow_challenge`，
   记录完整 `challenge / salt / difficulty / expire_at / signature / target_path`。
   （当前所有合成输入都无法让 `wasm_solve` 返回答案，§5。）
2. **用上游 WASM 求一次真答案**，记录 `(challenge, prefix, difficulty, answer)` 四元组。
3. **黑盒反推派生层**：固定 challenge 改一位看 hash 输出变化；用真实 answer 反向验证「nonce 拼在
   prefix 后还是 challenge 后」「nonce 是十进制还是十六进制」「比较是 `<` 还是 `<=`、阈值怎么由 difficulty 折算」。
4. **纯 Python 复现**：只依赖标准 SHA3（`hashlib.sha3_256` 或自写 Keccak），不引入任何上游产物。
5. **回归**：≥100 组真实 challenge 上自研实现与 WASM 的 answer 一致，且答案能被服务端接受。
6. 许可注意：即便走到 A，也只是「自研协议逻辑」，**上游源码/WASM 依旧不得进仓**。

第 1 步就是当前唯一的硬阻塞点：没有账号，后面 2-5 全部无法执行。

---

## 继续 sidecar 的维持方案与部署要求（B 的运维口径）

**形态**
- 上游进程独立运行（Go + Wazero），仓内只保留 [deepseek_sidecar.py](file:///D:/qwq/%E9%A1%B9%E7%9B%AE/unified-2api/apps/control/app/adapters/deepseek_sidecar.py) 做 HTTP 代理。
- 仓内**不携带** `module.wasm`、不携带上游源码/二进制；由使用者自行准备。

**部署清单**
1. 仓库外 clone 上游，按 README 编译（`go build ./cmd/server`）。
2. 在同目录放 `config.json`：`keys`（网关侧 key）、`accounts`（邮箱/手机号+密码，或直接 token）、`wasm_path` 指向自备的 wasm 文件。
3. 端口：两边默认都是 5001，无需额外配置；若自行改 sidecar 端口（`PORT=xxxx`），须同步网关侧 `DEEPSEEK_SIDECAR_BASE=http://127.0.0.1:xxxx`。
4. 健康检查：`GET /` 返回 `{"service":"deepseek-go-proxy","status":"ok",...}`。
5. 网关侧：provider 保持 `status="sidecar"`、`risk_level="danger"`；`/v1/messages` 仍走 `/anthropic/v1/messages` 重写且仅非流式。

**监控/降级**
- `get_pow_challenge` 失败或 `answer==0` 时上游直接报 `Failed to get PoW`（HTTP 401）——网关应原样透出，不重试、不伪装成功。
- 上游 token 失效时，配置模式会自动重登一次并重试 create_session（`main.go:820-838`），用户自带 token 模式不会重登。
- Anthropic 流式恒为 501，网关侧不要向客户端宣告支持流式。

---

## 需要真实凭证才能确认的部分

| 项 | 现状 | 需要什么 |
| --- | --- | --- |
| `difficulty` 的真实类型/量级（浮点？144000 级？） | 只有代码里 `float64(difficulty)`，无真实样本 | 一次真实 challenge |
| `answer` 语义（整数 nonce？浮点？范围） | WASM 返回 `Option<f64>`，合成输入恒 `None` | 一次真实 challenge + 上游求出的 answer |
| hash 的输入拼接（是否 prefix+challenge+nonce、编码方式） | 已证明 ≠ sha3-256(明文)，具体未知 | 真实 answer 反推 |
| 比较语义（`<` vs `<=`，阈值如何由 difficulty 折算） | 未知 | 真实 challenge 复现 |
| token 模式是否每次都必须 PoW | 代码面上是（每次 completion 前都取） | 真账号抓包确认服务端是否放宽 |
| 登录接口是否仍兼容（`device_id/os=android`） | 代码 2024 口径，未验证 | 真账号登录一次 |
| 服务端是否校验 `x-client-version: 1.3.0-auto-resume` | 未知 | 抓包对比 |

---

## 本轮实际做了什么

- 只读上游 `cmd/server/main.go`、`README.md`、`go.mod`、`example.config.json`；解析 `module.wasm` 的段结构与导出签名；在**仓库外**临时目录用 Node 原生 WebAssembly 做了 hash/solve 黑盒调用（未把任何二进制/WASM 复制进仓）。
- **没有修改任何代码**，因此 `adapters/__init__.py` 无需新增注册行（结论不是 A）。
- 未执行任何 git 写操作。
