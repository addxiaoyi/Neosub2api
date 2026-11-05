export type Role = 'admin' | 'viewer';

export interface Me {
  username: string;
  role: Role;
}

export interface Account {
  /** 账号归属的服务商 id；缺省按 codebuddy 处理（存量账号全是它） */
  provider?: string;
  /** 本端「导入账号」登记的行：上游没有这份账号，删除只回收本端行与镜像凭据 */
  local_only?: boolean;
  /** 该令牌签发的总时长（秒）；后端从 JWT 解出，解不出为 null */
  ttl_seconds?: number | null;
  /** 令牌签发时间（秒）≈ 最近一次刷新时间；后端从 JWT iat 解出，解不出为 null */
  issued_at?: number | null;
  file: string;
  uid: string;
  nickname: string;
  enterprise_id: string;
  expires_at: number;
  is_expired: boolean;
  remain_seconds: number;
  /** 当前可花费积分余额（上游聚合套餐剩余额度），null 表示尚未同步 */
  credits?: number | null;
  /** 来自 workbuddy2api /status 的运行时字段，可能为空 */
  healthy?: boolean | null;
  disabled?: boolean | null;
  /** 被上游禁用时的原因（如 11140 request illegal 需重新登录）；空串表示未给出 */
  disabled_reason?: string | null;
  in_flight?: number | null;
  cooling?: boolean | null;
  /** 冷却剩余秒数（上游权威值：可能是上游明说的重置时刻，也可能是有界退避）；null = 未在冷却 */
  cool_remaining_sec?: number | null;
  /**
   * 被限流的模型清单（上游限额台账）。多模型限流时各模型的恢复时刻**不同**，
   * 账号级冷却时间不足以表达，故逐条列出。
   * 字段名照上游 /status 的 JSON：model / until / reset_at / reason。
   */
  /**
   * 本端给这个账号写的备注（issue #67）。按 uid 存在本端库里，不在上游账号文件里，
   * 所以临时停用（改文件名）不会丢。没有备注时是空串（不是缺字段）。
   */
  note?: string;
  /**
   * 今天成功签到的时刻（epoch 秒）；null / 缺省 = 今天还没签。
   *
   * 判定依据是本端签到记录（腾讯对「今天已签过」回 10001，我们照记成功），
   * 所以它同时代表「本面板签过」与「今天已签到」。界面据此把签到按钮变成
   * 已签到态，避免重复点击（详见 server/routers/accounts.py 的 `_today_start`）。
   */
  checkin_today?: number | null;
  rate_limited_models?: {
    model: string;
    /** 该模型的冷却截止（已被 soft_rate_max 截断） */
    until?: string;
    /** 上游明说的重置时刻（未截断）；无时间文案时缺省 */
    reset_at?: string;
    reason?: string;
  }[];
  success_count?: number | null;
  err_total?: number | null;
  breaker_fails?: number | null;
  /**
   * 连败降权截止（上游 issue #114，RFC3339 字符串）。在未来 = 正被降权。
   *
   * 为什么要有：上游把降权**计入 cooling**，所以「冷却中」里混着两类原因完全不同
   * 的情况——限流退避（等一会儿就好）与连败降权（这个号在持续失败）。不区分时
   * 用户看到「冷却中」无从判断该等还是该处理。
   */
  degrade_until?: string | null;
  /** 连续失败计数（降权进度：达阈值即降权，成功后清零） */
  consecutive_fails?: number | null;
  last_success?: string | null;
  last_used?: number | null;
  source: 'file' | 'pool';
  /**
   * 该账号是否出现在上游账号池里（`/status.accounts`）。
   *
   * 我们读的是 auths/ 目录下的文件，上游读的才是池——两者不总一致：上游
   * `LoadDir` 对解析失败的文件静默跳过（`Parse` 在 accessToken 为空时报错）。
   * 那种文件不在池里、永远选不中；若不标出来，界面会显示成「在线」，
   * 出现「面板全绿但调用报没有健康账号」的矛盾。
   */
  in_pool?: boolean;
  /**
   * 本次没取到上游状态（`/status` 里 `connected: false`）——运行时字段全部未知。
   *
   * 与 `in_pool: false` 是**两回事**：前者是「看不到上游」，后者是「上游明确没
   * 加载它」。混为一谈会把正常账号误报成文件损坏（实测确认过这个误报）。
   */
  poolUnknown?: boolean;
  /** 已知上游不会加载该文件时的原因（空 = 未发现明显问题） */
  invalid_reason?: string;
  /**
   * 本面板**主动临时停用**（issue #21）——文件名带 `.disabled` 后缀，
   * 上游的 `workbuddy*.json` glob 因此不再匹配它、不加载该账号。
   *
   * 与 `disabled` 是两回事：那个是上游按错误分类自动禁的（需重新登录），
   * 这个是运维手动停用的，在面板上再点一次「启用」即可恢复。
   *
   * **注意**：新版上游走 `manual_disabled` 状态位停用（见下），那时本字段为
   * false 而 `manual_disabled` 为 true。两种停用都要当「已停用」看。
   */
  disabled_by_panel?: boolean;
  /**
   * 上游的**手动停用**状态位（上游 issue #138/#118，本面板 issue #45）。
   *
   * 语义是「对话流量摘除」而非「账号冻结」：账号仍在池里、**签到 / token 保活 /
   * 猫猫旅行照常执行**，凭证与积分都是活的，只是不被选中转发。与 `disabled`
   * 并列独立（上游对叠加态分别透出），两位都清才真正回池。
   *
   * 有它的时候优先用它：副作用比改名（`disabled_by_panel`）小得多。
   *
   * `null` = 上游这份快照里没这个字段（账号不在池里，或旧版上游没有该状态位）。
   * 与 `false`（明确未停用）区分，同 `disabled` 的口径。
   */
  manual_disabled?: boolean | null;
  /** 手动停用的原因文案（仅 `manual_disabled` 为真时有值） */
  manual_reason?: string;
  /** 账号所属版本（cn / global）；存量账号按域名回退，无该字段时视为 cn */
  realm?: 'cn' | 'global';
  /** 该版本是否支持签到体系（国际版没有） */
  checkin_supported?: boolean;
  /** auth 文件里的 domain，便于确认归属 */
  domain?: string;
}

export interface AccountsResponse {
  total: number;
  accounts: Account[];
  /** 已从上游同步到运行时状态的账号数 */
  pool_synced?: number;
  /** 上游是否可达 */
  pool_available?: boolean;
}

/** 上游为一组账号给出的计数（`/status` 的顶层汇总与 realm_totals 同构） */
export interface PoolCounts {
  total: number;
  healthy: number;
  cooling: number;
  disabled: number;
  in_flight_full: number;
}

export interface UpstreamStatus {
  connected: boolean;
  accounts?: Record<string, unknown>[];
  cooling?: number;
  disabled?: number;
  healthy?: number;
  total?: number;
  in_flight_full?: number;
  /**
   * 上游**按版本分好组**的计数（cn / global）。
   * 界面切到某个版本时要的是这一份，而不是顶层那份全局汇总——
   * 后者的 healthy/cooling 含两个版本，直接用会在国际版视图下显示国内版的数。
   * 注意 healthy 是**汇总层**字段，账号明细里没有它（曾因此在界面上恒显示 0）。
   */
  realm_totals?: Record<string, PoolCounts>;
  redis_mode?: string;
  sticky_sessions?: number;
  error?: string;
}

/** 单条模型在某个 provider 上的真实来源 */
export type ModelEntrySource = 'upstream' | 'declared' | 'declared_fallback';

/** provider 的就绪状态（与 /api/providers 同一套语义） */
export type ModelReadiness = 'ready' | 'needs_config';

/** 同一模型在某一家的来源与就绪状态（后端 /api/models 的 provider_states） */
export interface ModelProviderState {
  source: ModelEntrySource;
  readiness: ModelReadiness;
  requires: string[];
  enabled: boolean;
  available: boolean;
  unavailable_reason: string | null;
  fallback_reason: string | null;
}

export interface ModelInfo {
  id: string;
  /** 上游原始归属字段，等于主归属 provider */
  owned_by?: string;
  /** 上下文窗口（上游动态拉取给出；静态回退表为固定值） */
  context_length?: number;
  context_window?: number;
  /**
   * 最大输出 token。**只有上游动态拉取的条目才带这个键**，内置静态回退表没有，
   * 前端据此判断列表来源（见 ModelListResponse.source）。
   */
  max_output_tokens?: number | null;
  /** 主归属 provider（首个返回该 id 的一家） */
  provider?: string;
  /** 全部归属 provider；多于一家即多家共享 */
  providers?: string[];
  /** 主归属的来源 */
  source?: ModelEntrySource;
  /** 逐家的来源与就绪状态，键为 provider id */
  provider_states?: Record<string, ModelProviderState>;
  /** 聚合后的就绪状态：任一归属家可用即 ready */
  readiness?: ModelReadiness;
  /** 聚合后的缺失配置项（各家 requires 去重） */
  requires?: string[];
  /** 任一归属家既启用又就绪 */
  enabled?: boolean;
  available?: boolean;
  /** 不可用原因；available 为 true 时为 null */
  unavailable_reason?: string | null;
  /** 主归属处于 declared_fallback 时的上游失败原因 */
  fallback_reason?: string | null;
}

/** 模型列表来源：dynamic = 上游实时动态拉取；static = 上游内置静态回退表 */
export type ModelSource = 'dynamic' | 'static' | 'unknown';

export interface ModelListResponse {
  models: ModelInfo[];
  source: ModelSource;
  count: number;
}

export interface ApiKey {
  id: number;
  name: string;
  prefix: string;
  enabled: boolean;
  created_at: number;
  last_used_at: number | null;
  /** 仅在创建时返回一次 */
  key?: string;
}

/**
 * 管理面**作用域化 API Token**（见 docs/api-tokens.md）。
 *
 * 与上面那把网关密钥（`ApiKey`）是**两套东西**：`ApiKey` 只授权模型调用；
 * 本类型授权的是管理接口 `/api/*`，供脚本 / CI 免登录调用。
 */
export interface ApiToken {
  id: number;
  name: string;
  /** 明文前 12 字符，用于展示与识别（明文本身不会再回传） */
  prefix: string;
  /** 权限：只读 / 管理员（角色由它决定） */
  scope: 'readonly' | 'admin';
  enabled: boolean;
  /** 到期时刻（epoch 秒）；null = 永不过期 */
  expires_at: number | null;
  created_at: number;
  /** 创建者用户名（审计用） */
  created_by: string;
  last_used_at: number | null;
  last_used_ip: string | null;
}

/** 创建令牌的返回：比 ApiToken 多一个**仅此一次**的明文字段。 */
export interface CreatedApiToken extends ApiToken {
  token: string;
}

/**
 * 密钥/令牌批量操作的逐条回执。
 * 成功项只回 `ok`，失败项带后端原文原因；响应里不会出现密钥明文或哈希。
 */
export interface BatchKeyResult {
  id: string;
  ok: boolean;
  error?: string;
}

export interface BatchKeyResponse {
  ok: boolean;
  action: string;
  results: BatchKeyResult[];
}

export interface RequestLog {
  id: number;
  ts: number;
  key_id: number | null;
  key_name: string | null;
  /** 处理本次请求的服务商 id（codebuddy / sub2api 等） */
  provider: string;
  /**
   * 来源 IP。后端 request_logs 表没有这一列，恒为 '' —— 界面显示「未采集」。
   */
  ip: string;
  model: string | null;
  /** 模型映射结果。后端未采集，恒为 null */
  mapped_model: string | null;
  status: number;
  prompt_tokens: number;
  completion_tokens: number;
  latency_ms: number;
  /** 首字延迟（毫秒）。后端未采集，恒为 null */
  first_token_ms: number | null;
  /** 提示词缓存三段 token。后端未采集，恒为 null */
  cache_hit_tokens: number | null;
  cache_miss_tokens: number | null;
  cache_write_tokens: number | null;
  /** User-Agent。后端未采集，恒为 null */
  ua: string | null;
  error: string | null;
  stream: boolean;
  /** 本次调用真实扣的费（micro-USD 整数）。没记账时为 null，属于哪一类没记账看 credit_status */
  credit: number | null;
  /** 记账口径：billed=已扣费；estimated=按正文估算计费；unpriced=模型未定价；
   *  usage_missing=上游没回 usage；not_billed=失败/断流/被拒不写流水；unlinked=历史存量未关联流水 */
  credit_status: CreditStatus;
  /** credit_status 为 estimated 的布尔捷径；仅为 true 时金额是网关估算，不是上游真值 */
  credit_estimated: boolean;
  /** 本次实际用的上游账号。后端未采集，恒为 null */
  account: string | null;
  /** 运维是否已标记排查；只改这一列，不影响计费口径 */
  resolved: boolean;
  resolved_at: number | null;
  resolved_by: string;
}

/** GET /api/logs 每行的记账口径，取值来自 apps/control/app/routers/logs.py 的 _classify_credit */
export type CreditStatus = 'billed' | 'estimated' | 'unpriced' | 'usage_missing' | 'not_billed' | 'unlinked';

export interface UsagePoint {
  day: string;
  requests: number;
  prompt_tokens: number;
  completion_tokens: number;
  /** 当日实际扣费合计 */
  credit: number;
  /**
   * 当日失败请求数（4xx + 5xx 合计）。
   *
   * 单独来自 `request_logs` 而非用量汇总——后者只含有 token 或扣费的请求，
   * 被拒绝的调用与全池不可用（503，零 token）在里面根本不存在。所以这一项
   * 补上了「用量表天然看不到失败」的盲区。
   *
   * 可选：老版本后端不返回该字段，界面按 0 处理（不显示失败）。
   */
  failed?: number;
}

export interface StatsSummary {
  today_requests: number;
  today_tokens: number;
  /** 实际扣费（上游 usage.credit 合计）；上游未返回该字段时恒为 0 */
  today_credit: number;
  week_credit: number;
  total_credit: number;
  week_requests: number;
  week_tokens: number;
  total_requests: number;
  total_tokens: number;
  active_keys: number;
  top_model: string | null;
  /**
   * 统计写入的健康状态。`ok: false` 时详情说明为什么可疑。
   *
   * 存在意义：统计是旁路写入（失败不影响转发），坏了以后界面看不出异常——
   * 数字只是停着不动，页面照常轮询。所以要把「今天有请求但统计为 0」这种
   * 组合显式报出来，而不是等用户自己发现。
   */
  /**
   * `logs_today` 是**本该累计用量**的今日调用数（服务端按与 `bump_usage`
   * 相同的条件统计）。界面用它拼译文，不要去正则解析 `detail` 那句中文——
   * 那样改文案会让译文静默失效（曾经如此）。
   */
  usage_health?: {ok: boolean; detail: string; logs_today?: number};
  /**
   * 失败请求数（4xx / 5xx 分档，今天与近 7 天）。
   *
   * 为什么不在 `today_requests` 里体现：那个数来自用量汇总，只含**成功**
   * 请求（有 token 或有扣费）。失败请求在汇总里完全不存在，于是界面上的
   * 「请求数」与趋势图都只反映成功量——全池中断那天看起来像「没有请求」。
   * 这一项把失败显式补出来。
   *
   * 可选：老版本后端不返回，界面按全 0 处理（不显示失败相关提示）。
   */
  failures?: {
    today_4xx: number;
    today_5xx: number;
    week_4xx: number;
    week_5xx: number;
  };
}

export interface UserItem {
  username: string;
  role: Role;
}

export interface UpstashConfig {
  /** Upstash 地址，支持 https://xxx.upstash.io 或 xxx.upstash.io */
  url: string;
  /** 是否已配置 token（内容不回传） */
  has_token: boolean;
  /** token 掩码，仅用于展示 */
  token_masked: string;
}

export interface UpstreamConfig {
  /** 是否成功读取到上游配置文件；false 时前端应提示并禁止保存 */
  available?: boolean;
  /**
   * 保存后若需要用户手动做一步（容器部署下无法自动重启上游），后端带回此提示。
   * 有值时界面用醒目提示转达，而不是显示「正在自动应用」。
   */
  reload_hint?: string;
  /** 上游配置文件路径 */
  config_path?: string;
  /** 读取失败原因 */
  error?: string;
  listen?: string;
  api_key_masked?: string;
  auth_dir?: string;
  /** 上游 config.json 中声明的 auth_dir，与管理端不一致时出现 */
  upstream_auth_dir?: string;
  schedule?: Record<string, unknown>;
  pool?: Record<string, unknown>;
  cooldown?: Record<string, unknown>;
  features?: Record<string, unknown>;
  session_sticky?: Record<string, unknown>;
  prompt?: Record<string, unknown>;
  server?: Record<string, unknown>;
  upstream?: Record<string, unknown>;
  /** 国际版（workbuddy.ai）路由：enabled / chat_base / billing_base */
  global?: Record<string, unknown>;
  /**
   * 上游的运维管理端点开关：enabled。
   *
   * 决定账号页「临时停用」走哪条路——开启后走上游的停用接口（只摘转发流量，
   * 签到与保活照常），不开启则回退为改文件名（账号完全退出账号池）。
   * 上游默认关闭，所以面板提供一个开关，否则用户只能手改 config.json。
   */
  admin?: Record<string, unknown>;
  upstash?: UpstashConfig;
}

/** 一个套餐的到期时刻与它名下的可用额度 */
export interface CreditExpiry {
  /** 到期时刻（epoch 秒）。腾讯下发的是 UTC+8 墙钟，后端已换算成绝对时刻 */
  at: number;
  /** 该套餐当前的可用额度 */
  amount: number;
}

/** 积分查询来源：实时查询 or 命中 60 秒缓存 */
export interface CreditsMeta {
  cached: boolean;
  cache_age: number | null;
  message?: string;
  /** 各套餐到期时间，按到期时刻升序；只含仍有余额的套餐 */
  expiries?: CreditExpiry[];
}

/** 更新任务所处的阶段；空闲时后端给空串 */
export type UpdateStep =
  | 'download'
  | 'verify'
  | 'stage'
  | 'stop'
  | 'swap'
  | 'start'
  | 'health'
  | 'rollback';

export type UpdateJobState = 'idle' | 'running' | 'done' | 'failed';

/** 发布包签名校验结果：已验证 / 无签名 / 签名无效 / 未执行 */
export type UpdateSignature = 'verified' | 'unsigned' | 'invalid' | 'none';

export interface UpdateJob {
  state: UpdateJobState;
  /** 空闲时为空串 */
  step: UpdateStep | '';
  /** 0-100 */
  progress: number;
  /** epoch 秒；0 = 尚未开始 */
  started_at: number;
  /** epoch 秒；0 = 尚未结束 */
  finished_at: number;
  error: string;
  log_tail: string[];
}

export interface UpdateStatus {
  current_version: string;
  latest_version: string;
  /** 是否有比当前版本更新的版本可用 */
  available: boolean;
  /** 发布通道，如 stable */
  channel: string;
  signature: UpdateSignature;
  /** 本部署是否找得到可用的更新器 */
  updater_found: boolean;
  /** 是否运行在容器里 */
  in_container: boolean;
  /** 是否具备执行更新的条件（容器/服务管理器未配置时为 false） */
  can_apply: boolean;
  /** can_apply=false 时的原文原因 */
  reason: string;
  job: UpdateJob;
}

export interface Versions {
  manager: string;
  upstream_connected: boolean;
  upstream_accounts: number | null;
  upstream_dir: string;
}

/** 更新日志中的一条（level 1 = 二级缩进，作为子项展示） */
export interface ChangelogItem {
  level: number;
  text: string;
}

export interface ChangelogSection {
  /** 中文分类：安全 / 新增 / 修复 / 改进 / 说明 / 计划中 */
  title: string;
  items: ChangelogItem[];
}

export interface ChangelogVersion {
  /** 版本号，未发布的开发内容为「未发布」 */
  version: string;
  /** 发布日期，未发布时为空串 */
  date: string;
  /** 是否为尚未发布的开发内容 */
  unreleased: boolean;
  sections: ChangelogSection[];
}

export interface Changelog {
  /** CHANGELOG.md 是否可读 */
  available: boolean;
  /** 不可读时的原因 */
  error?: string;
  /** 文件路径（便于排查） */
  path?: string;
  /** 版本总数（未被截断时） */
  total?: number;
  versions: ChangelogVersion[];
  /** 版本过多，列表被截断 */
  truncated?: boolean;
  /** 当前部署版本，用于高亮 */
  current?: string;
}

export interface Page<T> {
  total: number;
  items: T[];
}

/* ── 管理端审计日志 ─────────────────────────────────── */

export interface AuditLog {
  id: number;
  ts: number;
  /** 操作者用户名（anonymous 表示未认证） */
  actor: string;
  /** login / login_failed / update_user / delete_user ... */
  action: string;
  target: string;
  detail: string;
  ip: string;
}

export interface AuditLogPage {
  items: AuditLog[];
  total: number;
}

/* ── 安全面（net_guard 地址守卫 / 登录失败节流）───────────── */

/** 上游地址守卫配置（/api/security/guard）。全部只读——清单是代码常量，不是库表。 */
export interface GuardInfo {
  /** 是否允许私网/环回地址作为上游（settings.allow_private_upstreams） */
  allow_private_upstreams: boolean;
  /** 恒定封禁的主机名（如元数据服务域名） */
  blocked_hostnames: string[];
  /** 恒定封禁的网段（CIDR 字符串） */
  blocked_addresses: string[];
  /** 封禁的地址类别（loopback/private/link_local/...） */
  blocked_categories: string[];
  /** 允许的上游协议 */
  allowed_schemes: string[];
}

/** 仍在节流窗口内的一条登录失败记录（进程内存态） */
export interface LoginFailureActive {
  ip: string;
  username: string;
  /** 窗口内失败次数 */
  failures: number;
  first_ts: number;
  last_ts: number;
  /** 达到上限 = 已锁定 */
  locked: boolean;
  /** 锁定剩余秒数（未锁定为 0） */
  retry_after: number;
}

export interface LoginFailuresResponse {
  /** all = 全站（仅 admin）；self = 当前账号的聚合数字 */
  scope: 'all' | 'self';
  window_sec: number;
  max_failures: number;
  /** 窗口内仍在计数的 IP+用户名组合（scope=self 时恒为空） */
  active: LoginFailureActive[];
  /** audit_logs 里 login_failed 的历史统计（scope=self 时只算自己账号） */
  stats: {total: number; today: number};
  /** 最近的失败审计记录（scope=self 时恒为空） */
  recent: AuditLog[];
  /** 仅 scope=self：窗口内针对自己账号的失败次数 */
  window_failures?: number;
  /** 仅 scope=self：针对自己账号的最近一次失败时刻，0 表示没有 */
  last_failed_ts?: number;
}

/* ── sub2api 配额域（订阅账号池 / 分组 / 扣减流水）──────── */

export interface Sub2apiGroup {
  id: string;
  name: string;
  platform: string;
  /** 订阅总额度（token 数），0 表示不限 */
  quota_limit: number;
  /** 已用量 */
  quota_used: number;
  /** 剩余额度；limit=0 时为 null（不限） */
  quota_remaining: number | null;
  /** 上次重置时刻（epoch 秒） */
  reset_at: number;
  enabled: boolean;
  /** 该分组下的账号总数 */
  accounts: number;
  /** 当前可用（启用 + 未耗尽 + 未冷却）的账号数 */
  active_accounts: number;
}

export interface Sub2apiAccount {
  id: number;
  group_id: string;
  name: string;
  upstream_base: string;
  /** 分发权重 */
  weight: number;
  quota_limit: number;
  quota_used: number;
  quota_remaining: number | null;
  enabled: boolean;
  /** 冷却截止时刻（epoch 秒），0 表示未冷却 */
  cooldown_until: number;
  last_used_at: number;
  has_api_key: boolean;
}

export interface Sub2apiLedgerEntry {
  id: number;
  ts: number;
  group_id: string;
  account_id: number | null;
  model: string;
  prompt_tokens: number;
  completion_tokens: number;
  /** 本次扣减的 token 数（prompt + completion） */
  cost: number;
  status: number;
  note: string;
  reason: string | null;
}

/* ── 定价（price_books）────────────────────────────────── */

export interface PriceBookRow {
  id: number;
  provider: string;
  model: string;
  /** input / output / cache_read / cache_write 等 */
  token_class: string;
  /** 每百万 token 的微美元价（1e-6 USD） */
  price_micro_per_1m: number;
  /** 每百万 token 的美元价（展示用） */
  price_usd_per_1m: number;
  currency: string;
  effective_from: number;
  created_by: string;
  note: string;
}

export interface PriceResolveResult {
  provider: string;
  model: string;
  at: number;
  /** 各 token_class 对应的命中行 */
  lines: Record<
    string,
    {
      price_id: number;
      provider: string;
      model: string;
      price_micro_per_1m: number;
      price_usd_per_1m: number;
    }
  >;
  /** 未命中价格的 token_class 列表 */
  missing: string[];
}

/* ── Grok 注册任务（/api/grok/register）────────────────── */

export type GrokRegisterTaskStatus = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';

/** 任务行：字段与 apps/control/app/db.py 的 grok_tasks 表一一对应 */
export interface GrokRegisterTask {
  id: number;
  status: GrokRegisterTaskStatus;
  /** 目标注册数量 */
  count: number;
  workers: number;
  email_provider: string;
  proxy: string;
  payload_json: string;
  result_json: string;
  attempts: number;
  max_attempts: number;
  /** 后端原样的失败原因，成功时为空串 */
  error: string;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  next_run_at: number;
}

/** 入参与后端 TaskCreate 同名同义，范围限制也一致 */
export interface GrokRegisterTaskInput {
  count: number;
  workers: number;
  email_provider: string;
  proxy: string;
  max_attempts: number;
  payload: Record<string, unknown>;
}

/** GET /api/grok/register/worker：队列计数 + 执行器配置状态 */
export interface GrokRegisterWorkerStats {
  queued: number;
  running: number;
  succeeded: number;
  failed: number;
  cancelled: number;
  max_concurrency: number;
  max_workers: number;
  executor: 'http' | 'unconfigured';
}

/** grok_task_events 的一行：状态机每次流转落一条，不是前端按时间戳倒推 */
export interface GrokRegisterEvent {
  ts: number;
  /** 阶段：queued / running / retried / retry / succeeded / failed / cancelled */
  step: string;
  /** 该阶段结果：ok / failed */
  status: string;
  /** 后端原样原因文本；失败事件即错误原文，前端不得截断 */
  detail: string;
}

/** GET /api/grok/register/tasks/{id}/detail：任务行 + 结构化步骤 + 时间线 */
export interface GrokRegisterDetail {
  task: GrokRegisterTask;
  status: GrokRegisterTaskStatus;
  error: string;
  steps: GrokRegisterEvent[];
  timeline: {
    created_at: number;
    started_at: number | null;
    finished_at: number | null;
    next_run_at: number;
  };
}

/** GET /api/system/backups：data/backups 下的归档，新→旧 */
export interface BackupItem {
  name: string;
  size_bytes: number;
  /** 归档文件的 mtime（秒） */
  created_at: number;
  /** 已过 mask_text，可直接展示 */
  path: string;
  /** 同名 .sha256 侧车是否存在 */
  has_checksum: boolean;
}

export interface BackupList {
  backups: BackupItem[];
  count: number;
  dir: string;
}

/** POST /api/system/backup */
export interface BackupCreated {
  ok: boolean;
  name: string;
  size_bytes: number;
  duration_ms: number;
  path: string;
  sha256: string;
  created_at: number;
}

/** POST /api/system/restore：pre_restore_backup 是恢复前的自动快照，可原样再恢复一次退回去 */
export interface BackupRestored {
  ok: boolean;
  restored: string;
  path: string;
  pre_restore_backup: string;
  pre_restore_path: string;
  duration_ms: number;
  selfcheck_ok: boolean;
  /** 恢复后各表行数（来自归档 manifest） */
  rows_after: Record<string, number>;
}

/** 提示词库列表项：正文只回摘要，详情再取 */
export interface PromptSummary {
  id: number;
  title: string;
  description: string;
  category: string;
  sub_category: string;
  mode: string;
  image_size: string;
  created_by: string;
  created_at: number;
  updated_at: number;
  excerpt: string;
}

export interface PromptDetail extends Omit<PromptSummary, 'excerpt'> {
  prompt: string;
}

export interface PromptPage {
  total: number;
  page: number;
  size: number;
  items: PromptSummary[];
}

export interface PromptInput {
  title: string;
  prompt: string;
  description?: string;
  category?: string;
  sub_category?: string;
  mode?: string;
  image_size?: string;
}
