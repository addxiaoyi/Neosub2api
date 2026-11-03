# 新增一家 provider（控制面现行步骤）

本文是「加第 15 家 / 第 N 家 provider」的唯一现行说明。控制面适配器是唯一扩展点；**前端一行不动**（基准仓库 `codebuddy2api` 原样，provider 列表由 `/api/providers` 提供）。前端红线见 [FRONTEND-BASELINE.md](FRONTEND-BASELINE.md)。

## 0. 先定接入形态

| 形态 | 适用 | 做法 |
| --- | --- | --- |
| 内置直连 | 上游是公开 HTTP 协议，鉴权简单 | 继承 `OpenAICompatAdapter`，只写 `ProviderSpec` + `build()` |
| sidecar 透传 | 上游是需要独立进程的网关（登录、续期、代理池都在里面） | 控制面只做 HTTP 转发，不复制上游源码或二进制 |
| 非推理服务 | 注册任务、调度一类 | 照 `grok_register`：`protocols` 为空，能力位只留实际支持的 |

判断标准：需要把上游源码或二进制放进本仓的，一律走 sidecar。

## 1. 写适配器

新建 `apps/control/app/adapters/<name>.py`，最小实现参考 [codar_sidecar.py](file:///d:/qwq/项目/unified-2api/apps/control/app/adapters/codar_sidecar.py)：

```python
from ..config import Settings
from .base import ProviderSpec, stub_capabilities
from .openai_compat import OpenAICompatAdapter


class FooAdapter(OpenAICompatAdapter):
    spec = ProviderSpec(
        id="foo",
        name="Foo Gateway",
        vendor="owner/foo",
        license="MIT",
        runtime="独立进程 sidecar（Go）",
        status="sidecar",
        description="一句话说清它提供什么、边界在哪。",
        default_base_url="http://127.0.0.1:8802",
        capabilities=stub_capabilities(chat=True, models=True, sidecar=True),
        protocols=("openai.chat.completions",),
        risk_level="safe",
        risk_note="",
    )

    def __init__(self, settings: Settings, base_url: str = "") -> None:
        self._settings = settings
        super().__init__(base_url or settings.foo_sidecar_base)


def build(settings: Settings, base_url: str = "") -> FooAdapter:
    return FooAdapter(settings, base_url)
```

`ProviderSpec` 字段（定义见 [base.py](file:///d:/qwq/项目/unified-2api/apps/control/app/adapters/base.py)）：

| 字段 | 约束 |
| --- | --- |
| `id` | 稳定标识：`providers` 表主键、`x-provider` 取值、`model` 前缀（`<id>/<model>`）。上线后不要改名 |
| `name` / `vendor` / `license` / `runtime` / `status` / `description` | 展示与合规信息；`status` 用 `implemented` / `sidecar`（现有代码里只有这两个值） |
| `default_base_url` | 兜底地址；来自环境变量的地址在下一步单独接 |
| `capabilities` | 用 `stub_capabilities(...)` 只声明真正实现了的能力位，避免「声明了但 501」 |
| `protocols` | `openai.chat.completions` / `anthropic.messages` / `openai.responses`；都不支持就留空 |
| `risk_level` / `risk_note` | `safe`（MIT/Apache/BSD）、`warn`（GPL 类传染性）、`danger`（无 LICENSE 或禁止商用，只能 sidecar 评估） |

协议差异只覆盖需要改的方法：`messages()` 不标准就重写，鉴权头特殊就在 client 层加；不要复制整个适配器。

## 2. 注册到 `BUILDERS`

改 `apps/control/app/adapters/__init__.py` 两处：import 新模块，并在 `BUILDERS` 元组追加 `<name>.build`。

启动时 `ProviderRegistry._seed()` 会自动给 `providers` 表插入一行：默认 `enabled = 0`（只有 `codebuddy` 默认开启），`label` 取 `spec.name`，`base_url` 取默认地址。不需要手写数据库迁移。

默认地址如果来自环境变量（不是 `spec.default_base_url`），要改三处，缺一处地址来源就会被当成 `default`：

1. `config.py` 的 `ENV_BASE_VARS` 加 `"<id>": "<NAME>_BASE"`；
2. `Settings` 加对应字段，`load_settings()` 读 env 并 `.rstrip("/")`；
3. `registry.py` 的 `ENV_BASE_FIELDS` 加 `"<id>": "<settings 字段名>"`。

`registry.resolved()` 只在 pid 同时出现在 `settings.env_base_pids` 时才算 `env` 来源，所以空 env 不会覆盖默认值。

## 3. 接配置（env）

改 `apps/control/app/config.py`：`Settings` 加字段，`load_settings()` 里 `os.getenv(...)` 读取，末尾统一 `.rstrip("/")`。命名沿用现有约定：地址 `<NAME>_BASE`，凭据 `<NAME>_API_KEY`。

注意两条既有机制：

- 凭据不写进 `ProviderSpec`。页面填的 `api_key` / `options` 存在 `settings` 表的 `provider_config.{id}`，由 `registry.get()` 每次 `adapter.configure()` 下发，保存即生效。
- `PATCH /api/providers/{id}` 改 `base_url` 会过 `guard_upstream_url`：默认拒绝环回 / 私网地址。内网 sidecar 场景要么保留默认地址不动，要么显式设 `UNIFIED_ALLOW_PRIVATE_UPSTREAMS=1`。

## 4. 部署条目

- `.env.example`：加 `<NAME>_BASE=...`（或 `<NAME>_API_KEY=`）与一行注释，说明「未启动时该 provider 显示不可达，不假装成功」。
- `docker-compose.yml`：在 `control.environment` 加映射；容器访问宿主机 sidecar 用 `host.docker.internal:<port>`，并给出 `:-` 默认值。
- sidecar 自身不在本仓交付：文档里写清端口、启动方式、凭证放在哪一侧。

## 5. 记录上游

在 `upstreams/upstreams.yaml` 的 `upstreams` 列表追加一项，字段与现有条目一致：

| 字段 | 说明 |
| --- | --- |
| `repo` | `owner/name` |
| `license` / `license_source` | 上游 LICENSE 文件的实际标题与出处；没有就写实际情况 |
| `integration` | `in-repo-rewrite` / `sidecar` / `reference-only` / `not-integrated` |
| `analyzed_commit` / `analyzed_at` / `checked_at` | 真实 `git rev-parse HEAD`；拿不到写 `unknown`。`checked_at` 只记巡检日期，不随巡检自动推进基线 |
| `our_files` | 本仓涉及文件，flow 数组单行 |
| `protocols` | 同上 |
| `notes` | 代理边界、已知差异、商用限制 |

格式是脚本约定：列表项以两空格 `- repo:` 开头、字段四空格缩进、`our_files` / `protocols` 必须单行 `[ "a", "b" ]`，写成 block 数组会让 `scripts/check-upstreams.ps1` 解析失败。

## 6. 写契约文档

- 新建 `docs/providers/<name>.md`：上游与许可证、协议（含非标路径）、必填配置、`options`、能力位、已知限制、风险与商用约束。
- 同步 `docs/PROVIDERS.md`：总览表加一行（id / 名称 / 上游 / 协议 / 默认地址 / 能力位 / 状态 / 风险），再补一节逐家说明。
- 若涉及价格，把数据来源补进 `docs/pricing-sources.md`。

## 7. 价格（可选）

新 provider 的模型没有价格行时不会拦截请求：金额只算有价部分，`estimated = 1`，`note` 记 `unpriced:<类别>`。

补价两条路：

- 种子价：改 `config/prices.yaml`（`provider` 一般写 `*`，记录厂商公开价而非上游结算价），`python scripts/check-prices.py` 校验后 `POST /api/pricing/import`。
- 运行时补价：`POST /api/pricing/books` 插入新行。**不要改旧行**（没有 PATCH/DELETE），改价必须用新的 `effective_from`。字段与口径见 [API-REFERENCE.md](API-REFERENCE.md) 第 10 节。

## 8. 前端：不动

- 前端只渲染 `GET /api/providers` 的返回（`id / name / vendor / license / runtime / status / description / base_url / default_base_url / base_url_source / base_url_warning / credential_base_warnings / enabled / capabilities / protocols / readiness / requires / risk_level / risk_note`）。新 provider 一注册就出现在列表里，不需要改页面。
- 不新增页面、不新增视觉组件、不加 tab、不改导航与配色、不改品牌名、不自造 tokens / shell / states / provider-kit。以上任一都按 [FRONTEND-BASELINE.md](FRONTEND-BASELINE.md) 阻断处理。
- provider 文案走 i18n 时只允许新增键，并同步全部 locale。

## 9. 自检清单

1. `python -m pytest apps/control/tests`（`test_providers.py` 覆盖注册与鉴权，`test_billing.py` 覆盖结算）。当前基线：**250 passed, 1 skipped, 3 subtests passed**。
2. 启动控制面，`GET /api/providers` 出现新 `id`，`enabled=false`、`has_api_key=false`。
3. `PATCH /api/providers/{id}` 启用并填 `api_key`，再查 `enabled=true`、`has_api_key=true`。
4. 带 `x-provider: <id>` 打一次 `/v1/chat/completions`；不可达时必须是 502/503，不能 500。
5. `GET /api/pricing/resolve?provider=<id>&model=<model>` 确认价格命中与 `missing` 符合预期。
6. 跑 `scripts/check-upstreams.ps1` 与 `docs\site\build.ps1`，确认清单和文档站同步。

## 10. 现状与已知缺口

当前 `BUILDERS` 共 14 个：`codar`、`codebuddy`、`codar_sidecar`、`deepseek_sidecar`、`gptgrok`、`grok_register`、`muse`、`opencode`、`qoder`、`qoder_sidecar`、`sub2api`、`trae`、`trae_sidecar`、`zcode`。

`docs/PROVIDERS.md` 总览 14 行已齐，逐家说明编号为 §1–§14（`codar` §11、`codar_sidecar` §12、`qoder` §13、`trae` §14）；本仓实现细节分别在 `providers/codar2api.md`、`providers/codar_sidecar.md`、`providers/qoder-adapter.md`、`providers/trae-adapter.md`（命名避开已有的上游契约文档 `codar2api.md` / `qoder.md` / `trae2api.md`）。
