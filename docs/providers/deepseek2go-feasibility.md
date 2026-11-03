# Deepseek2Go PoW 可行性评估（F2-B，不实现）

> 依据：对上游项目 `Deepseek2Go`（All Rights Reserved，仓库内无 LICENSE）公开流程的只读分析，
> 以及对其随包下发的 `module.wasm`（26,612 字节）做的静态结构解析。
> 本文件只记录事实与结论，**不包含**上游源码片段，也未把任何二进制/WASM 复制进主仓。
> 主仓实现：无（本轮只做可行性验证）。

## 结论

**需逆向且不确定。**

- 哈希内核是公开标准的 SHA3（Keccak-f[1600]，24 轮），这部分可以自研，不涉及许可问题。
- 但把 `challenge` / `salt` / `expire_at` / `difficulty` 变成 `answer` 的那层「wrapper 逻辑」
  （nonce 编码、拼接顺序、比较符号、循环上限）无法从静态结构直接读出，需要反汇编 + 动态验证。
- 上游把这段逻辑编译进 WASM 下发，说明它被视为可变的协议细节；一旦上游换版本，自研实现需要跟进。
- 上游仓库无 LICENSE 文件（All Rights Reserved），WASM 来源与再分发许可不明，**不能作为二进制依赖进主仓**。

## 证据

### 1. 调用面（PoW 是 completion 的前置）

| 步骤 | 端点 | 关键字段 |
| --- | --- | --- |
| 申请挑战 | `POST https://chat.deepseek.com/api/v0/chat/create_pow_challenge` | body `{"target_path":"/api/v0/chat/completion"}`；返回 `algorithm:"DeepSeekHashV1"`、`challenge`、`salt`、`difficulty`、`expire_at`、`signature`、`target_path` |
| 求解 | 本地 `wasm_solve` | `prefix = "{salt}_{expire_at}_"`，难度按浮点传入 |
| 提交 | `POST /api/v0/chat/completion` | 头 `x-ds-pow-response = base64(json{6 个挑战字段 + answer})` |

### 2. WASM 静态结构（`module.wasm`）

| 项 | 观测值 |
| --- | --- |
| 文件 | 26,612 字节，WASM version 1 |
| 段 | type(78)、function(48)、table(5)、memory(3)、global(9)、export(147)、element(18)、code(22,249)、data(3,964)、custom(59) |
| import 段 | **不存在**：零宿主导入，自包含 |
| memory | min 17 页（约 1.1 MB），无 max，非 shared |
| 函数 | 47 个 code function、11 个 type |
| 导出 | `memory`；`wasm_solve sig=(6 params, 0 results)`；`wasm_deepseek_hash_v1 sig=(3, 0)`；`__wbindgen_add_to_stack_pointer (1,1)`；`__wbindgen_export_0 (2,1)`；`__wbindgen_export_1 (4,1)`；`__wbindgen_export_2 (3,0)` |

`wasm_solve` 的 6 个参数对应 `(retptr, challenge_ptr, challenge_len, prefix_ptr, prefix_len, difficulty)`，
返回值通过内存回写（`status` 在 `retptr`，结果 `f64` 在 `retptr+8`），这是 wasm-bindgen 的常见返回值约定。

### 3. 哈希内核

data 段（3,964 字节）可打印串共 56 条，其中直接暴露了编译来源：

```
'sha3-wasm/src/lib.rs '
'core/src/fmt/num.rs'
'/rust/deps/dlmalloc-0.2.6/src/dlmalloc.rs'
'0x0001020304050607080910111213141516171819202122232425262728293031323334353637383940414243444546474849505152535455565758'
```

说明：内核是 Rust `sha3` crate 的 wasm 构建（Keccak-f[1600]，标准 24 轮），内存由 dlmalloc 管理，
另有 wasm-bindgen 的栈指针/分配器胶水函数。**算法本身不是私有实现**，纯 Python/JS 重写 SHA3 没有障碍。

### 4. Token 模式同样必须过 PoW

上游的模式判定里，用户自带 Token 与匿名模式走同一条 `getPowResponseB64(token)`，
`callCompletionEndpoint(..., pow)` 必带 `x-ds-pow-response`。也就是说：

- 没有「用 Token 绕过 PoW」的路径；
- 走 Token 模式的上限 = 走匿名模式的上限 + 账号额度限制；
- 即使拿到付费 Token，仍要维护 PoW 求解器。

## 判定过程

| 判据 | 结果 |
| --- | --- |
| 能否静态读出导入/导出/内存布局 | 能（见上表） |
| 能否静态读出哈希算法与轮数 | 能（SHA3 / Keccak-f[1600] 24 轮，标准算法） |
| 能否静态读出 nonce 编码、拼接顺序、难度比较 | **不能**：code 段 22 KB，需反汇编或动态埋点 |
| 是否有公开的 PoW 规范 | 无，只有客户端实现本身 |
| 上游会不会变 | 会：WASM 随服务端下发，版本不固定 |
| 二进制许可是否允许进 MIT 主仓 | 不允许：无 LICENSE，来源不明 |

## 如果将来要做，最小可行路径

1. 只自研 SHA3，不引入上游二进制；把 `answer` 的搜索逻辑写成本仓代码（需先完成逆向）。
2. 逆向用黑盒：申请真实 challenge，用不同 `challenge/prefix/difficulty` 组合比对输出，
   先确认 `answer` 是否为整数 nonce、比较是 `< 2^256/difficulty` 还是前缀零位。
3. 必须补一条「上游换 WASM 版本」的降级策略：求解失败直接返回明确的上游错误，
   不重试、不伪装成功。
4. 在许可证明确前，最多做成本仓外的可选 sidecar，不进入主仓分发物。

## 风险

- 主要风险不是 SHA3，而是 wrapper 逻辑的语义不确定 + 上游随时换实现。
- 次要风险是许可：即使算法公开，上游二进制/源码不可复制，逆向结论也只能作为「自研参考」。
- 结论保持「需逆向且不确定」，不建议在 F2 排期里开工。
