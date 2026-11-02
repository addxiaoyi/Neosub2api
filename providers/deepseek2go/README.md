# Deepseek2Go sidecar 说明 ⚠️ 许可证风险

**上游仓库**：`https://github.com/ColudAI/Deepseek2Go`
**许可证**：**无 LICENSE 文件**（All Rights Reserved）🔴

## 重要声明

> 本仓**不拷贝、不修改、不分发** Deepseek2Go 的任何源码。
> unified-2api 仅提供一个 HTTP 代理适配器（`apps/control/app/adapters/deepseek_sidecar.py`），
> 让用户自行 clone 并启动 Deepseek2Go sidecar 进程后，通过标准 HTTP 调用。
>
> **商用前必须移除本 provider 或取得上游作者的书面授权。**

## 形态

独立 sidecar 进程（Go + WASM PoW 运行时）。sidecar 自行管理：
- DeepSeek 反爬虫 `DeepSeekHashV1` PoW（调用外部 WASM 文件）
- 邮箱/手机号 + 密码自动登录
- 账号池轮换

unified-2api 只做 HTTP 代理。

## 支持的协议

| 端点 | 说明 |
| --- | --- |
| `POST /v1/chat/completions` | OpenAI 兼容，流式 + 非流式 |
| `GET /v1/models` | 模型列表 |
| `POST /anthropic/v1/messages` | Claude 兼容（**仅非流式**；适配器做路径重写） |

## 快速启动（本地）

```bash
git clone --depth 1 https://github.com/ColudAI/Deepseek2Go.git ../Deepseek2Go
cd ../Deepseek2Go
# 需要外部 WASM 文件 sha3_wasm_bg.7b9ca65ddd.wasm（自行从 Deepseek2Go 作者渠道获取）
go run ./cmd/server
# 默认端口见 README.md，设为 TRAE_SIDECAR_BASE=http://127.0.0.1:<port>
```

## 合规建议

- 个人自用（非商用）：可作为 sidecar 评估，但随时可能被 Deepseek2Go 作者更新 LICENSE 后禁止
- 商用：**必须移除**，或联系作者补 MIT/Apache 许可
- 替代方案：独立实现 WASM PoW（`DeepSeekHashV1`）+ 用户自带 DeepSeek Token 模式
