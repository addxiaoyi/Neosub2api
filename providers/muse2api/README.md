# muse2api —— 上游参考来源（非依赖）

> 主仓**不依赖**该进程。这里只保留上游信息、版本要求与升级检查项。
> 我们的实现：`apps/control/app/adapters/muse.py`，协议契约：`docs/providers/muse2api.md`。

## 上游信息

- 仓库：<https://github.com/czg86389-hub/muse2api>
- 许可：MIT（`LICENSE` 首行 `MIT License`，Copyright (c) 2026 MUSE2API Authors）
- 形态：Python FastAPI + Uvicorn 单进程，内置 Chromium 账号池；默认端口 `18610`
- 能力：`/v1/chat/completions`、`/v1/models`（本仓已接）；`/v1/responses`、
  `/v1/images/*`、`/v1/videos/*`（上游有，本仓未接）

## 版本要求（硬性）

| 组件 | 最低版本 | 不满足的后果 |
| --- | --- | --- |
| muse2api sidecar | **v1.6.6** | v1.6.5 及更早把 `hatch_vml` 当必填，已登录账号会被拒绝导入 |
| 浏览器扩展 | **1.0.2** | 旧扩展只认 `hatch_sess` + `hatch_vml`，缺 `hatch_vml` 时返回 `essential_missing` 拒绝导入 |
| 油猴脚本 | **1.1.2** | 同上 |

依据：`version.json`（`"version": "1.6.6"`）、`engine.py:45` 与 `store.py:17`
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

- `apps/control/app/adapters/muse.py`：继承 `OpenAICompatAdapter`，只代理
  `/v1/chat/completions` 与 `/v1/models`；
- `MUSE2API_KEY` 以 `Authorization: Bearer` 注入（`app.py:319-328`）；
- 账号池、Cookie 校验、代理、扩展分发全部留在 sidecar，本仓不复制任何源码。

## 快速启动（本地对照）

```bash
git clone --depth 1 https://github.com/czg86389-hub/muse2api.git ../muse2api
cd ../muse2api
docker compose up -d   # 默认 18610，本仓 base_url 建议映射到 127.0.0.1:7865
```

## 已知版本差异

v1.6.5 → v1.6.6 只改账号导入规则与扩展升级提示，`/v1` 协议面未变。
详见 `docs/providers/muse2api.md` §5、§7。
