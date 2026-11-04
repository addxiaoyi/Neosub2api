# 上游巡检运行说明（check_upstreams.py）

## 需要令牌

- 脚本从 `GH_TOKEN` / `GITHUB_TOKEN` 环境变量读取令牌，不回显、不落盘。
- 无令牌时匿名额度只有 60 次/小时，11 家仓约 20+ 次请求，首跑就会撞限流；
  CI/无人值守环境必须注入令牌（工作流里用 `secrets.GITHUB_TOKEN`）。
- 无令牌运行时输出会显式警告「未配置令牌，可能被限流」，
  JSON 模式对应 `token_notice` 字段（`authenticated` 仍为 `false`）。

## 限流时的表现

- 403/429 会做有上限的指数退避重试：3 次、4s+8s+16s，总退避约 28s（≤30s），
  退避耗尽仍失败才记为 `error`（`reason=rate_limited`，退出码 2）。
- 一旦有请求退避耗尽，同一次运行内后续请求不再重复退避，直接报错，
  避免每家仓都等满 28s。
- 匿名限流不代表上游不可达：带令牌重跑（`GH_TOKEN=... python scripts/check_upstreams.py`）
  即可区分「真不可达」与「没带令牌」。

## --update 语义

- 只回写 `checked_at`（当天日期），不改 `analyzed_commit`、不改清单地址。
- 只回写本次确实拿到 HEAD 的记录：`status != error` 的仓库才盖章；
  error 仓（限流/404/网络）不写，防止谎称「查过」。

## CI 位置与判定口径

- `.github/workflows/ci.yml` 的 `upstream-check` job（显示名
  `upstream-check (drift informational / error fails)`），仅在 PR 与手动触发时运行，
  注入 `secrets.GITHUB_TOKEN`；token 为空（如 fork PR 场景）时打印
  「未带令牌，跳过」并放行。
- 退出码在 CI 里的判定：
  - `0`：全部 unchanged，通过。
  - `1`（有仓 updated）：**不失败**——打印「有 N 家上游出现新 commit，已记录但未推进基线；
    协议面影响需人工判定，见 docs/UPSTREAM-SYNC.md」的 notice 后放行。
    这类状态可能是已读码判定为无害、基线刻意不推进的仓，长期为真，
    拦红会让无关 PR 习惯性忽略这一步。
  - `2`（有仓 error/不可达，含限流退避耗尽）：**拦住 CI**（`continue-on-error: false`），
    打印 error 注解并以原退出码失败。
