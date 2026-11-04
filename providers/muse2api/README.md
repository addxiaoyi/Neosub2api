# muse2api —— 上游参考来源（非依赖）

> 主仓**不依赖**该进程。这里只保留上游信息、版本要求与升级检查项。
> 我们的实现：`apps/control/app/adapters/muse.py`，协议契约：`docs/providers/muse2api.md`。

## 上游信息

- 仓库：<https://github.com/czg86389-hub/muse2api>
- 许可：**MIT，可入主仓**（`LICENSE` 首行 `MIT License`，Copyright (c) 2026 MUSE2API Authors）
- 形态：Python FastAPI + Uvicorn 单进程，内置 Chromium 账号池；默认端口 `18610`
- 能力：`/v1/chat/completions`、`/v1/models`（本仓已接）；`/v1/responses`、
  `/v1/images/*`、`/v1/videos/*`（上游有，本仓未接）

## 版本要求（硬性）

| 组件 | 最低版本 | 不满足的后果 |
| --- | --- | --- |
| muse2api sidecar | **v1.6.6** | v1.6.5 及更早把 `hatch_vml` 当必填，已登录账号会被拒绝导入 |
| 浏览器扩展 | **1.0.2** | 旧扩展只认 `hatch_sess` + `hatch_vml`，缺 `hatch_vml` 时返回 `essential_missing` 拒绝导入 |
| 油猴脚本 | **1.1.2** | 同上 |

依据：上游 `version.json` 当前为 `"version": "1.6.7"`（2026-11-04 核对，同日已读码比对
v1.6.7 变更集，见文末「已知版本差异」；未在本机实跑）；门槛判断来自 `engine.py:45` 与 `store.py:17`
（`ESSENTIAL_COOKIES = ("hatch_sess",)`）、`extension/manifest.json:4`（`1.0.2`）、
`tools/muse2api_cookie_importer.user.js:4`（`@version 1.1.2`）。

## 升级检查项（每次部署后逐条过）

1. **sidecar 版本**：`curl.exe -s http://127.0.0.1:7865/openapi.json` 里的 `info.version`
   必须 ≥ `1.6.6`；容器内也可 `docker compose exec <service> cat /app/version.json`
   （`Dockerfile:30` 已 `COPY version.json`）。
2. **扩展版本**：`chrome://extensions` / `edge://extensions` 显示 `1.0.2`，并**必须点「重新加载」**。
   服务端升级不会自动更新已加载的未打包扩展（`README.md:129-133`、`admin.html:290`）。
3. **油猴版本**：Tampermonkey 面板里为 `1.1.2`。
4. **逐个浏览器个人资料核对**：`version.json` 的 `highlights` 明确要求每个 profile 单独升级。
5. **重新导入后点「测试」**：导入成功只说明记录已保存，可用性以上游会话验证为准
   （`tests/test_session_only_import.py:12-22`）。

任一检查项不通过，不要继续联调——账号导入会静默失败。

## 主仓如何实现同等能力

- `apps/control/app/adapters/muse.py`：继承 `OpenAICompatAdapter`，只接
  `/v1/chat/completions` 与 `/v1/models`，能力位 chat/models，协议 `openai.chat.completions`；
- **主路径为直连**：默认 base_url `https://api.meta.ai`（官方 OpenAI 兼容入口），
  `Authorization: Bearer <api_key>` 注入，SSE 逐 chunk 直通，不经过上游账号池；
- **sidecar 仅作回退**：把 base_url 指回 `127.0.0.1:7865` 即走旧网页网关，此时
  账号池、Cookie 校验（`options.cookie`）、代理、扩展分发全部留在 sidecar
  （`app.py:319-328`），本仓不复制任何源码。

## 快速启动（本地对照）

```bash
git clone --depth 1 https://github.com/czg86389-hub/muse2api.git ../muse2api
cd ../muse2api
docker compose up -d   # 默认 18610；仅回退时把本仓 base_url 指到 127.0.0.1:7865
```

## 已知版本差异

- **已核对**：v1.6.5 → v1.6.6 只改账号导入规则与扩展升级提示。
  详见 `docs/providers/muse2api.md` §5、§7。
- **v1.6.7 协议面已读码核对（2026-11-04，`1cd0168` → `58c1e7c`，2 个 commit）**：
  `/v1/chat/completions`、`/v1/models` 与 `Bearer` 鉴权均未变；变更集中在
  `/v1/images/*`、`/v1/videos` 的参考素材（新增 `videos` / `audios` / `files` 字段，
  上限 4→10 个、单个 10→40 MiB、合计 24→100 MiB）、multipart 编辑字段与
  composer/附件 DOM 检测，本仓未接这些端点，直连与 sidecar 回退路径不受影响。
  **未覆盖**：`engine.py` 的浏览器 DOM 内部实现只检索了变更关键词、未逐行读，
  v1.6.7 未在本机实跑，运行行为（含新版输入区修复的实际效果）未验证。
