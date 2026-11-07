import axios, {AxiosError} from 'axios';
import {BASE_PATH} from './base-path';
import {tp} from './i18n';
import type {
  CodebuddyTaskPage,
  CodebuddyTaskReply,
  ConsoleActionReply,
  ConsoleMediaInventory,
  ProviderConsole,
} from './provider-console';
import type {Realm} from './realm-context';
import type {
  ProviderCredential,
  ProviderCredentialInput,
  ProviderHealth,
  ProviderInfo,
  ProviderTokenCheck,
} from './providers';
import type {
  Account,
  CreditsMeta,
  AccountsResponse,
  ApiKey,
  ApiToken,
  BatchKeyResponse,
  CreatedApiToken,
  Me,
  AuditLogPage,
  GuardInfo,
  GrokRegisterDetail,
  GrokRegisterTask,
  GrokRegisterTaskInput,
  GrokRegisterWorkerStats,
  LoginFailuresResponse,
  ModelListResponse,
  Page,
  RequestLog,
  Changelog,
  BackupList,
  BackupCreated,
  BackupRestored,
  RemoteBackupConfig,
  RemoteBackupList,
  RemoteBackupListFailure,
  RemoteBackupRestored,
  RemoteBackupUploaded,
  StatsSummary,
  UpstreamConfig,
  UpstreamStatus,
  UpdateStatus,
  UsagePoint,
  UserItem,
  Versions,
  Sub2apiGroup,
  Sub2apiAccount,
  Sub2apiLedgerEntry,
  PriceBookRow,
  PriceResolveResult,
  PromptDetail,
  PromptInput,
  PromptPage,
} from './types';

/** 单次请求 60s 封顶：转发链路包含上游排队与生成，再久基本已是卡死，交给用户重试 */
const REQUEST_TIMEOUT_MS = 60_000;

export const http = axios.create({
  // 子路径部署时接口也挂在前缀下（反向代理剥掉前缀再转发给本服务）。
  // 这里必须用 basePath，否则所有请求都会打到域名根路径的 /api 上。
  baseURL: BASE_PATH,
  withCredentials: true,
  timeout: REQUEST_TIMEOUT_MS,
});

/**
 * 统一抽取后端错误信息。
 *
 * 后端的报错是中文（服务端不做多语言，见 README 的多语言说明），这里过一遍
 * 短语表：命中已收录的后端文案就换成当前语言，没收录的原样展示——既不需要
 * 后端改造，也不会因为漏收录而显示成键名或空白。
 */
export function errText(e: unknown): string {
  const ax = e as AxiosError<{detail?: string; error?: string}>;
  const d = ax?.response?.data;
  // 非 JSON 响应（如后端裸 404 返回的 HTML 错误页）：body 里是一整页 HTML，
  // 剥了标签也只剩一堆内联 JS，毫无信息量。直接给一个简短的状态提示即可。
  const ct = String(ax?.response?.headers?.['content-type'] || '');
  if (typeof d === 'string' && !ct.includes('application/json')) {
    return ax.response?.status ? `${ax.response.status} ${tp('请求失败')}` : tp('请求失败');
  }
  const raw = (typeof d === 'string' ? d : d?.detail || d?.error) || ax?.message || '';
  return raw ? tp(raw) : tp('请求失败');
}

http.interceptors.response.use(
  (r) => r,
  (error: AxiosError) => {
    if (error.response?.status === 401 && typeof window !== 'undefined') {
      // 会话失效：清掉缓存的登录态，避免仍显示管理员入口
      window.sessionStorage.removeItem('wb-me');
      const path = window.location.pathname;
      // 公开页不跳登录页。当前唯一公开页是登录页本身；将来再加公开页往清单里加一条。
      // 路径必须带 basePath：basePath 只自动作用于 next/router 的跳转，裸的
      // window.location.href 会跳到域名根（通常 404）。
      const loginPath = `${BASE_PATH}/login`;
      const publicPaths = [loginPath];
      if (!publicPaths.some((p) => path.startsWith(p))) {
        window.location.href = loginPath;
      }
    }
    return Promise.reject(error);
  },
);

/**
 * 首屏聚合接口单独限时：后端 /api/status 有 4s 预算、/api/models 有 5s 预算，
 * 前端再多留 1s 兜住网络抖动。超过就按「拿不到」降级渲染，不能让加载态无限转圈。
 */
const STATUS_TIMEOUT_MS = 5_000;
const MODELS_TIMEOUT_MS = 6_000;

const get = async <T>(
  url: string,
  params?: Record<string, unknown>,
  timeout?: number,
): Promise<T> => (await http.get<T>(url, {params, ...(timeout ? {timeout} : {})})).data;
const post = async <T>(url: string, body?: unknown, params?: Record<string, unknown>): Promise<T> =>
  (await http.post<T>(url, body, {params})).data;
const patch = async <T>(url: string, body?: unknown): Promise<T> =>
  (await http.patch<T>(url, body)).data;
const put = async <T>(url: string, body?: unknown): Promise<T> =>
  (await http.put<T>(url, body)).data;
const del = async <T>(url: string): Promise<T> => (await http.delete<T>(url)).data;

/* ── 服务商注册表 ────────────────────────────────────── */
export const providerApi = {
  list: () => get<ProviderInfo[]>('/api/providers'),
  update: (
    id: string,
    body: {
      enabled?: boolean;
      base_url?: string;
      label?: string;
      api_key?: string;
      options?: Record<string, unknown>;
    },
  ) => patch<ProviderInfo>(`/api/providers/${encodeURIComponent(id)}`, body),
  /** 定时健康检测：只读观测，不改路由/启用状态 */
  health: (provider?: string) =>
    get<ProviderHealth>('/api/providers/health', provider ? {provider} : undefined),
  /** 手动跑一轮检测；传 provider 时只测这一家（侧车可达性靠它，别拿 readiness 反推） */
  runHealth: (provider?: string) =>
    post<ProviderHealth>('/api/providers/health/run', undefined, provider ? {provider} : undefined),
};

/* ── 运行参数（这里只用到健康检测总开关） ─────────────── */
export type RuntimeConfigPayload = {
  ok: boolean;
  items: {key: string; value: unknown}[];
  saved?: string[];
};

export const runtimeConfigApi = {
  get: () => get<RuntimeConfigPayload>('/api/settings/runtime-config'),
  save: (patch: Record<string, unknown>) =>
    post<RuntimeConfigPayload>('/api/settings/runtime-config', patch),
};

/* ── 网关鉴权开关 ─────────────────────────────────────── */
export const gatewayApi = {
  get: () => get<{allow_anonymous: boolean; configured: boolean}>('/api/settings/gateway'),
  update: (allow_anonymous: boolean) =>
    post<{allow_anonymous: boolean; configured: boolean}>('/api/settings/gateway', {allow_anonymous}),
};

/* ── 鉴权 ───────────────────────────────────────────── */
export type LoginResult =
  | {ok: true; username: string; role: string}
  | {ok: false; need_totp: true; challenge: string; expires_in: number};

export type TotpStatus = {enabled: boolean; recovery_codes_left: number; period: number};

export const authApi = {
  me: () => get<Me>('/api/me'),
  login: (username: string, password: string) =>
    post<LoginResult>('/api/login', {username, password}),
  /** 2FA 第二步：用一次性 challenge + 验证码（或恢复码）换会话 */
  totpVerify: (challenge: string, code?: string, recoveryCode?: string) =>
    post<{ok: boolean; username: string; role: string}>('/api/auth/totp/verify', {
      challenge,
      code: code || null,
      recovery_code: recoveryCode || null,
    }),
  totpStatus: () => get<TotpStatus>('/api/auth/totp/status'),
  totpSetup: () =>
    post<{secret: string; otpauth_uri: string; algorithm: string; digits: number; period: number}>(
      '/api/auth/totp/setup',
    ),
  totpEnable: (code: string) =>
    post<{ok: boolean; enabled: boolean; recovery_codes: string[]}>('/api/auth/totp/enable', {code}),
  totpDisable: (password: string, code: string) =>
    post<{ok: boolean; enabled: boolean}>('/api/auth/totp/disable', {password, code}),
  refresh: () => post<{ok: boolean; username: string; role: string; expires_in: number}>('/api/auth/refresh'),
  logout: () => post<{ok: boolean}>('/api/logout'),
  /** 吊销当前用户的**全部**会话（含本机）——服务端递增会话版本，所有 cookie 立即失效。 */
  revokeSessions: () => post<{ok: boolean; relogin_required: boolean}>('/api/sessions/revoke'),
};

/* ── 账号 ───────────────────────────────────────────── */
export const accountApi = {
  list: () => get<AccountsResponse>('/api/accounts'),
  /** 发起扫码登录；realm 决定国内版 / 国际版端点 */
  start: (realm: Realm = 'cn') =>
    post<{state: string; authUrl: string; realm: Realm}>('/api/auth/start', {realm}),
  /** 轮询扫码结果。region 仅国际版需要（新号必须先做地区注册） */
  poll: (state: string, realm?: Realm, region?: string) =>
    get<{
      status: 'waiting' | 'success' | 'expired' | 'invalid' | 'realm_mismatch';
      uid?: string;
      nickname?: string;
      updated?: boolean;
      realm?: Realm;
      region_note?: string;
      expected?: Realm;
      got?: Realm;
    }>('/api/auth/poll', {state, realm, region}),
  remove: (file: string) => del<{success: boolean}>(`/api/accounts/${encodeURIComponent(file)}`),
  /** 临时禁用 / 启用账号（issue #21）：改文件名 + 触发上游重载。 */
  setDisabled: (file: string, disabled: boolean) =>
    post<{ok: boolean; file: string; disabled: boolean; changed: boolean;
          reload_triggered: boolean; message: string}>(
      `/api/accounts/${encodeURIComponent(file)}/disabled`, {disabled}),
  checkin: (file: string) =>
    post<{
      code: number;
      message: string;
      /** 今天已经签过：后端没打上游请求，直接就地返回。提示语要与「刚签上」分开 */
      already?: boolean;
      credits?: number | null;
    }>(`/api/accounts/${encodeURIComponent(file)}/checkin`),
  /** 查询全部账号积分；force=false 时 60 秒内命中服务端缓存 */
  refreshCredits: (force = true) =>
    post<{
      total: number;
      succeeded: number;
      credits: Record<string, number | null>;
      meta: Record<string, CreditsMeta>;
      failed: string[];
    }>('/api/accounts/refresh-credits' + (force ? '?force=true' : '?force=false')),
  checkinAll: () =>
    post<{
      /**
       * 只统计**本次真正发起签到**的账号：国际版（不适用）与今天已签到的都不进
       * 分母，分别见 skipped / already。原先 total 里混着「今天已签过」的账号，
       * 界面会把「无需重复」说成「刚签成功」。
       */
      total: number;
      succeeded: number;
      /** 今天已签到、本次被跳过的账号数（没打上游请求） */
      already: number;
      /** 不适用的账号数（国际版没有签到体系）。它既不算成功也不算失败 */
      skipped: number;
      results: {
        nickname: string; ok: boolean; message: string;
        code?: number; skipped?: boolean; already?: boolean;
      }[];
    }>('/api/accounts/checkin-all'),
  test: (file: string) =>
    post<{ok: boolean; message: string}>(`/api/accounts/${encodeURIComponent(file)}/test`),
  refresh: (file: string) =>
    post<{ok: boolean; message: string}>(`/api/accounts/${encodeURIComponent(file)}/refresh`),
  /** 强制清除账号级冷却、熔断/降权与模型级限流（会重启一次上游）。 */
  clearCooling: (file: string) =>
    post<{ok: boolean; message: string; uid?: string; backup?: string}>(
      `/api/accounts/${encodeURIComponent(file)}/clear-cooling`,
    ),
  /**
   * 给账号写备注（issue #67）。传空串 = 清除备注。
   * 存的是本端库、按 uid 关联——临时停用（改文件名）不会丢。
   */
  setNote: (file: string, note: string) =>
    put<{ok: boolean; uid: string; note: string}>(
      `/api/accounts/${encodeURIComponent(file)}/note`,
      {note},
    ),
  restart: () => post<{ok: boolean; message: string}>('/api/accounts/restart'),
  /**
   * 批量操作（admin-only）：整批恒 200，逐条结果在 results 里。
   * 不支持该动作的 provider 会逐条回 error，界面必须原样展示，不能吞成「部分失败」。
   */
  batch: (
    action: 'disable' | 'delete' | 'restart',
    files: string[],
    disabled = true,
  ) =>
    post<{
      ok: boolean;
      action: string;
      results: {id: string; ok: boolean; error?: string}[];
    }>('/api/accounts/batch', {action, files, disabled}),
};

/* ── 上游状态 ───────────────────────────────────────── */
export const upstreamApi = {
  status: () => get<UpstreamStatus>('/api/status', undefined, STATUS_TIMEOUT_MS),
  /** 上游模型简表；realm 非空时只返回该版本的条目 */
  models: (realm?: Realm) => get<ModelListResponse>('/api/models', {realm}, MODELS_TIMEOUT_MS),
};

/* ── API 密钥 ───────────────────────────────────────── */
export const keyApi = {
  list: () => get<ApiKey[]>('/api/keys'),
  /** 创建成功时响应里带一次明文 key，此后服务端只存哈希 */
  create: (body: {name: string}) => post<ApiKey>('/api/keys', body),
  update: (id: number, body: {name?: string; enabled?: boolean}) =>
    patch<ApiKey>(`/api/keys/${id}`, body),
  remove: (id: number) => del<{ok: boolean}>(`/api/keys/${id}`),
  /** 批量启停/删除：后端逐条回执，单条失败不影响其余（ids 以字符串传给后端） */
  batch: (action: 'enable' | 'disable' | 'delete', ids: number[]) =>
    post<BatchKeyResponse>('/api/keys/batch', {action, ids: ids.map(String)}),
};

/* ── 首次启动向导（状态落库，前端只做展示）──────────────── */
export type OnboardingState = {
  completed: boolean;
  current_step: number;
  skipped_steps: string[];
  updated_at: number | null;
};

export const onboardingApi = {
  get: () => get<OnboardingState>('/api/onboarding'),
  save: (patch: Partial<Pick<OnboardingState, 'completed' | 'current_step' | 'skipped_steps'>>) =>
    post<OnboardingState>('/api/onboarding', patch),
  /** 仅用于「重新走一遍向导」；完成后标志在库内，重建库也会自然重置 */
  reset: () => post<OnboardingState>('/api/onboarding/reset'),
};

/* ── 访问令牌（管理面作用域化 API Token）──────────────────
 * 与 keyApi 是两套：那个是给下游调模型的网关密钥，这个授权管理接口。
 * 明文只在创建时返回一次。 */
export const tokenApi = {
  list: () => get<ApiToken[]>('/api/tokens'),
  /** 后端只接受只读作用域与不设到期（admin / 定时到期会被 400 拒绝） */
  create: (body: {name: string; scope?: 'readonly'; expires_at?: null}) =>
    post<CreatedApiToken>('/api/tokens', body),
  update: (id: number, body: {
    name?: string;
    scope?: 'readonly';
    enabled?: boolean;
    expires_at?: null;
  }) => patch<ApiToken>(`/api/tokens/${id}`, body),
  remove: (id: number) => del<{ok: boolean}>(`/api/tokens/${id}`),
  batch: (action: 'enable' | 'disable' | 'delete', ids: number[]) =>
    post<BatchKeyResponse>('/api/tokens/batch', {action, ids: ids.map(String)}),
};

/* ── 日志 ───────────────────────────────────────────── */
/** 列表与导出共用同一组筛选键，后端 logs._where 是同一套语义 */
export type LogQuery = {
  key_id?: string;
  provider?: string;
  model?: string;
  status?: string;
  days?: number;
};

export const logApi = {
  /** 形参键须与后端 logs.list_logs 的 query 参数同名，contract_check 逐键对账 */
  list: (params: {
    page?: number;
    size?: number;
    key_id?: string;
    provider?: string;
    model?: string;
    status?: string;
    days?: number;
  }) => get<Page<RequestLog>>('/api/logs', params),
  clear: () => post<{ok: boolean}>('/api/logs/clear'),
  /** 标记/撤销「已排查」：只动 request_logs 的 triage 列，不碰 billing_ledger */
  resolve: (id: number) =>
    post<{ok: boolean; id: number; resolved: boolean; resolved_at: number; resolved_by: string}>(
      `/api/logs/${id}/resolve`,
    ),
  unresolve: async (id: number) =>
    (await http.delete<{ok: boolean; id: number; resolved: boolean; resolved_at: number; resolved_by: string}>(
      `/api/logs/${id}/resolve`,
    )).data,
  /** 导出 CSV：筛选键与列表同源；错误体是 Blob，文案要先用 downloadErrText 解码 */
  exportCsv: async (params: LogQuery) => {
    const res = await http.get('/api/logs/export', {params, responseType: 'blob'});
    return {blob: res.data as Blob, filename: filenameOf(res.headers['content-disposition'])};
  },
};

/** 从 Content-Disposition 取文件名；后端给的已是 ASCII */
const CSV_SUFFIX = '.csv';

function filenameOf(header: unknown, fallback: string = `logs${CSV_SUFFIX}`): string {
  const match = typeof header === 'string' ? header.match(/filename="?([^";]+)"?/) : null;
  // 文件名不是界面文案；后缀单独拼，免得被 i18n 键扫描当成翻译键
  return match ? match[1] : fallback;
}

/**
 * 二进制下载失败时 axios 拿到的是 Blob：先读回 JSON 才能复用 errText 的短语表，
 * 否则只会显示一句没有信息量的「请求失败」。
 */
export async function downloadErrText(e: unknown): Promise<string> {
  const blob = (e as AxiosError<Blob>)?.response?.data;
  if (!(blob instanceof Blob)) return errText(e);
  try {
    const payload = JSON.parse(await blob.text()) as {detail?: unknown};
    const raw = typeof payload.detail === 'string' ? payload.detail : '';
    return raw ? tp(raw) : errText(e);
  } catch {
    return errText(e);
  }
}

/* ── sub2api 配额域（订阅分组 / 账号池 / 流水）────────── */
export const sub2Api = {
  groups: () => get<Sub2apiGroup[]>('/api/sub2api/groups'),
  createGroup: (body: {id: string; name: string; platform: string; quota_limit: number}) =>
    post<Sub2apiGroup>('/api/sub2api/groups', body),
  patchGroup: (
    id: string,
    body: {name?: string; platform?: string; quota_limit?: number; enabled?: boolean; reset_quota?: boolean},
  ) => patch<Sub2apiGroup>(`/api/sub2api/groups/${encodeURIComponent(id)}`, body),
  removeGroup: (id: string) => del<{ok: boolean}>(`/api/sub2api/groups/${encodeURIComponent(id)}`),
  accounts: (group_id?: string) => get<Sub2apiAccount[]>('/api/sub2api/accounts', {group_id}),
  createAccount: (body: {
    group_id: string;
    name: string;
    upstream_base: string;
    api_key: string;
    weight: number;
    quota_limit: number;
  }) => post<Sub2apiAccount>('/api/sub2api/accounts', body),
  patchAccount: (
    id: number,
    body: {
      name?: string;
      upstream_base?: string;
      api_key?: string;
      weight?: number;
      quota_limit?: number;
      enabled?: boolean;
      clear_cooldown?: boolean;
    },
  ) => patch<Sub2apiAccount>(`/api/sub2api/accounts/${id}`, body),
  removeAccount: (id: number) => del<{ok: boolean}>(`/api/sub2api/accounts/${id}`),
  resetAccount: (id: number) => post<Sub2apiAccount>(`/api/sub2api/accounts/${id}/reset`),
  ledger: (group_id?: string, limit = 50) =>
    get<Sub2apiLedgerEntry[]>('/api/sub2api/ledger', {group_id, limit}),
};

/* ── 定价（price_books / resolve / import）────────────── */
export const pricingApi = {
  books: (params: {provider?: string; model?: string; token_class?: string; limit?: number} = {}) =>
    get<PriceBookRow[]>('/api/pricing/books', params),
  create: (body: {
    model: string;
    token_class: string;
    price_micro_per_1m: number;
    provider?: string;
    effective_from?: number;
    note?: string;
  }) => post<PriceBookRow>('/api/pricing/books', body),
  resolve: (params: {provider: string; model: string; at?: number}) =>
    get<PriceResolveResult>('/api/pricing/resolve', params),
  // 后端的 dry_run 是查询参数（FastAPI 简单类型不读 body），放 body 里会被静默当成 false
  // 后端返回的是 inserted/skipped/total（无逐条 errors），这里统一成界面用的 added/skipped/errors。
  // 少一层归一会让 preview.errors 变成 undefined，渲染 .length 时整页进 Error Boundary。
  importBooks: async (dry_run = false) => {
    const r = await post<{
      inserted?: number;
      skipped?: number;
      total?: number;
      unpriced?: string[];
      path?: string;
    }>(`/api/pricing/import?dry_run=${dry_run}`);
    return {
      added: Number(r?.inserted ?? 0),
      skipped: Number(r?.skipped ?? 0),
      errors: [] as string[],
      path: r?.path,
    };
  },
};

/* ── 用量统计 ───────────────────────────────────────── */
export const statsApi = {
  /** realm 非空时只统计该版本（界面按版本切换时传） */
  summary: (realm?: Realm) => get<StatsSummary>('/api/stats/summary', {realm}),
  daily: (days = 30, realm?: Realm) => get<UsagePoint[]>('/api/stats/daily', {days, realm}),
};

/* ── 安全 / 审计（只读：地址守卫、登录失败、审计事件）───────── */
export const securityApi = {
  /** 上游地址守卫配置（net_guard 的封禁清单与开关），只读 */
  guard: () => get<GuardInfo>('/api/security/guard'),
  /** 登录失败节流状态 + 历史失败统计 */
  loginFailures: (limit = 50) => get<LoginFailuresResponse>('/api/security/login-failures', {limit}),
  /** 管理端审计事件（登录/改密码/清日志等），limit/offset 分页 */
  audit: (params: {limit?: number; offset?: number; action?: string; actor?: string} = {}) =>
    get<AuditLogPage>('/api/audit-logs', params),
  /**
   * 定向清除登录失败计数（admin-only）：ip / username 至少给一个，没有 all 通配。
   * 无匹配时后端回 200 + 全 0，界面按「无可清理项」处理，不是错误。
   */
  clearLoginFailures: (body: {ip?: string; username?: string}) =>
    post<{ok: boolean; cleared: {throttle_keys: number; users: number}}>(
      '/api/security/login-failures/clear',
      {ip: body.ip ?? '', username: body.username ?? ''},
    ),
  /** 凭据落库加密现状（admin）：只回计数与开关，不含任何明文/密文片段 */
  secretAtRest: () => get<SecretAtRest>('/api/security/secret-at-rest'),
};

export type SecretAtRest = {
  credentials_total: number;
  credentials_sealed: number;
  credentials_plaintext: number;
  api_keys_total: number;
  api_keys_sealed: number;
  api_keys_plaintext: number;
  undecryptable: number;
  sessions_plaintext: number;
  key_configured: boolean;
  key_state: 'ok' | 'missing' | 'invalid';
  /** UNIFIED_SECRET_AT_REST_REQUIRED 当前是否为真（改后需重启才影响启动行为） */
  required: boolean;
  mode: 'encrypted' | 'plaintext';
};

/* ── 运行时指标（限流配置的唯一只读出口，admin）────────────── */
export type RateLimitSnapshot = {
  window_sec: number;
  requests_limit: number;
  concurrency_limit: number;
  key_rejected: number;
  concurrency_rejected: number;
};

export type MetricsSnapshot = {
  runtime?: {
    inflight?: number;
    rate_limit?: RateLimitSnapshot;
  };
};

export const metricsApi = {
  /** 只取 runtime 段；window_seconds 给最小值，避免为了看配置去扫大窗口 */
  json: (windowSeconds = 60) =>
    get<MetricsSnapshot>('/api/metrics/json', {window_seconds: windowSeconds}),
};

/* ── qoder_sidecar 会话搬运（原字节转发，响应保持 sidecar 自有 JSON 壳）── */
export interface RawSidecarReply {
  status: number;
  ok: boolean;
  contentType: string;
  body: string;
}

/** 原样转发：不改写请求体、不包装响应，非 2xx 也要把 sidecar 的原文带回来 */
async function postRaw(url: string, body: unknown): Promise<RawSidecarReply> {
  const r = await http.post(url, body, {
    responseType: 'text',
    transformResponse: [(d: string) => d],
    validateStatus: () => true,
  });
  return {
    status: r.status,
    ok: r.status >= 200 && r.status < 300,
    contentType: String(r.headers['content-type'] || ''),
    body: typeof r.data === 'string' ? r.data : JSON.stringify(r.data),
  };
}

/* ── 服务商管理台（只读快照 + 显式运维动作） ─────────── */
export const providerConsoleApi = {
  snapshot: (id: string, window = '7d') =>
    get<ProviderConsole>(`/api/providers/${encodeURIComponent(id)}/console`, {window}),
  action: (id: string, action: string, extra?: {task?: string; task_id?: string; force?: boolean}) =>
    post<ConsoleActionReply>(`/api/providers/${encodeURIComponent(id)}/console/actions`, {action, ...extra}),
  media: (id: string, kind: 'files' | 'editable' = 'files') =>
    get<ConsoleMediaInventory>(`/api/providers/${encodeURIComponent(id)}/console/media`, {kind}),
  codebuddyTasks: (limit = 20) =>
    get<CodebuddyTaskPage>('/api/providers/codebuddy/tasks', {limit}),
  runCodebuddyTask: (task: string, force = false) =>
    post<CodebuddyTaskReply>('/api/providers/codebuddy/tasks', {task, force}),
};

export const qoderSessionsApi = {
  /** 包写到 sidecar 主机的 outPath，本仓不产出可下载文件 */
  export: (body: {outPath: string; uid?: string; sessionIds?: string[]; region?: string}) =>
    postRaw('/api/providers/qoder_sidecar/sessions/export', body),
  /** 加法写：重复导入同一会话会产生副本 */
  import: (body: {path: string; uid?: string; region?: string}) =>
    postRaw('/api/providers/qoder_sidecar/sessions/import', body),
};

/* ── 模型映射批量导入 / 导出 / 回滚 ───────────────────── */
export type ModelMapImportMode = 'merge' | 'overwrite';

export interface ModelMapEntryResult {
  alias: string;
  target?: string;
  /** applied / skipped / failed；整体被拒时后端另给 reason=aborted_by_invalid_entries */
  status?: string;
  /** added / updated / removed / unchanged；被拒或校验失败时为 null */
  action?: string | null;
  previous?: string | null;
  reason?: string | null;
  message?: string | null;
}

export interface ModelMapImportReply {
  ok: boolean;
  mode?: string;
  changed?: boolean;
  snapshot_taken?: boolean;
  total?: number;
  applied?: number;
  skipped?: number;
  removed?: number;
  failed?: number;
  results: ModelMapEntryResult[];
  error?: {type: string; message: string; errors?: ModelMapEntryResult[]};
}

export interface ModelMapExport {
  kind: string;
  version: number;
  /** epoch 秒 */
  exported_at: number;
  mappings: Record<string, string>;
}

/* ── 上游配置 / 用户 ───────────────────────────────── */
export const settingsApi = {
  upstream: () => get<UpstreamConfig>('/api/settings/upstream'),
  saveUpstream: (body: Record<string, unknown>) =>
    post<UpstreamConfig>('/api/settings/upstream', body),
  /** 测试 Upstash 连通性；token 留空表示使用已保存的值 */
  testUpstash: (url: string, token?: string) =>
    post<{ok: boolean; message: string}>('/api/settings/upstash/test', {url, token}),
  modelMap: () => get<Record<string, string>>('/api/settings/model-map'),
  saveModelMap: (body: Record<string, string>) =>
    post<Record<string, string>>('/api/settings/model-map', body),
  exportModelMap: () => get<ModelMapExport>('/api/settings/model-map/export'),
  /** 关键：400 与 500 的响应体也带 results，必须连状态码一起交回界面逐条展示 */
  importModelMap: async (mode: ModelMapImportMode, mappings: Record<string, unknown>) => {
    const r = await http.post<ModelMapImportReply>(
      '/api/settings/model-map/import',
      {mode, mappings},
      {validateStatus: () => true},
    );
    return {status: r.status, data: r.data};
  },
  rollbackModelMap: () =>
    post<{ok: boolean; restored: Record<string, string>; snapshot_taken_at?: number}>(
      '/api/settings/model-map/rollback',
    ),
  users: () => get<UserItem[]>('/api/users'),
  addUser: (body: {username: string; password: string; role: string}) =>
    post<UserItem>('/api/users', body),
  /** 改密码 / 改角色；会吊销该用户的既有会话（改自己则需重新登录） */
  updateUser: (username: string, body: {password?: string; role?: string}) =>
    patch<UserItem & {sessions_revoked?: boolean; relogin_required?: boolean}>(
      `/api/users/${encodeURIComponent(username)}`, body),
  removeUser: (username: string) => del<{ok: boolean}>(`/api/users/${encodeURIComponent(username)}`),
};

export type {Account};

/* ── 系统维护（一键更新）──────────────────────────────── */
export const systemApi = {
  updateStatus: () => get<UpdateStatus>('/api/system/update-status'),
  /** 主动检测远端版本（普通用户也可触发），返回刷新后的更新状态 */
  checkUpdates: () => post<UpdateStatus>('/api/system/update/check'),
  /** 执行在线更新；confirm_version 必须与 latest_version 逐字一致（admin） */
  applyUpdate: (confirmVersion: string) =>
    post<UpdateStatus>('/api/system/update/apply', {confirm_version: confirmVersion}),
  /** 回滚到上一个版本（admin） */
  rollbackUpdate: () => post<UpdateStatus>('/api/system/update/rollback'),
  /** 运行时版本信息：管理栏读取当前部署版本 */
  versions: () => get<Versions>('/api/system/versions'),
  /** 更新日志（解析仓库根目录 CHANGELOG.md，离线可用） */
  changelog: () => get<Changelog>('/api/system/changelog'),
  /** 备份归档清单（admin） */
  backups: () => get<BackupList>('/api/system/backups'),
  /** 立即备份一次；已有备份/恢复在跑时后端返 409 */
  createBackup: () => post<BackupCreated>('/api/system/backup'),
  /** 用指定归档覆盖当前库；confirm 必须显式 true，后端会先自动存一份当前快照 */
  restoreBackup: (name: string) =>
    post<BackupRestored>('/api/system/restore', {name, confirm: true}),
  /** 远端备份连接参数与非敏感状态；密钥不会出现在响应里 */
  remoteConfig: () => get<RemoteBackupConfig>('/api/system/backup-remote'),
  /** 只保存连接参数，不带任何密钥 */
  saveRemoteConfig: (body: {
    endpoint: string;
    bucket: string;
    region: string;
    prefix: string;
    path_style: boolean;
  }) => post<RemoteBackupConfig>('/api/system/backup-remote', body),
  /** 远端对象列表；502 时 body 是 detail 包装，逐层解析交给调用方 */
  remoteBackups: async () => {
    const r = await http.get<RemoteBackupList | RemoteBackupListFailure>(
      '/api/system/backups/remote',
      {validateStatus: () => true},
    );
    return {status: r.status, data: r.data};
  },
  /** 手动上传当前最新本地归档；缺密钥/未配置返回 409 */
  uploadRemoteBackup: () => post<RemoteBackupUploaded>('/api/system/backups/upload'),
  /** 从远端恢复；confirm 必须与 name 逐字一致 */
  restoreRemoteBackup: (name: string) =>
    post<RemoteBackupRestored>('/api/system/backups/restore-remote', {name, confirm: name}),
  /** 删除远端一份及其旁注；name 是完整对象 key（含 prefix） */
  deleteRemoteBackup: (name: string) =>
    del<{ok: boolean}>(`/api/system/backups/remote/${encodeURIComponent(name)}`),
};

/* ── 服务商凭据（统一凭据库，secret 只回掩码）────────────── */
export const credentialApi = {
  list: (provider?: string) =>
    get<ProviderCredential[]>('/api/provider-credentials', provider ? {provider} : undefined),
  create: (body: ProviderCredentialInput) =>
    post<ProviderCredential>('/api/provider-credentials', body),
  update: (
    id: number,
    body: Omit<ProviderCredentialInput, 'provider'>,
  ) => patch<ProviderCredential>(`/api/provider-credentials/${id}`, body),
  remove: (id: number) => del<{ok: boolean; id: number}>(`/api/provider-credentials/${id}`),
  validate: (provider: string, body: {token?: string; credential_id?: number}) =>
    post<ProviderTokenCheck>(
      `/api/providers/${encodeURIComponent(provider)}/token/validate`,
      body,
    ),
};

/* ── Grok 注册任务（读写全 admin，见 tests/test_security_matrix.py）── */
export const grokRegisterApi = {
  list: (limit = 50) => get<GrokRegisterTask[]>('/api/grok/register/tasks', {limit}),
  create: (body: GrokRegisterTaskInput) => post<GrokRegisterTask>('/api/grok/register/tasks', body),
  cancel: (id: number) => post<GrokRegisterTask>(`/api/grok/register/tasks/${id}/cancel`),
  retry: (id: number) => post<GrokRegisterTask>(`/api/grok/register/tasks/${id}/retry`),
  /** 状态机步骤 + 时间线（含失败原文），仅 admin */
  detail: (id: number) => get<GrokRegisterDetail>(`/api/grok/register/tasks/${id}/detail`),
  /** 队列计数 + 执行器是否已配置（任何登录用户可读） */
  worker: () => get<GrokRegisterWorkerStats>('/api/grok/register/worker'),
  /** 导出该任务的账号与登录凭据：文件含明文 cookie/token，仅 admin；错误体是 Blob */
  exportCredentials: async (id: number, fmt: 'json' | 'csv' = 'json') => {
    const res = await http.get(`/api/grok/register/tasks/${id}/credentials`, {
      params: {fmt},
      responseType: 'blob',
    });
    return {
      blob: res.data as Blob,
      filename: filenameOf(res.headers['content-disposition'], `grok-credentials-task-${id}.${fmt}`),
    };
  },
};

/* ── 提示词库（登录用户只读，admin 增删改）────────────── */
export const promptApi = {
  list: (params: {page?: number; size?: number; q?: string; category?: string}) =>
    get<PromptPage>('/api/prompts', params),
  categories: () => get<{items: string[]}>('/api/prompts/categories'),
  detail: (id: number) => get<PromptDetail>(`/api/prompts/${id}`),
  create: (body: PromptInput) => post<PromptDetail>('/api/prompts', body),
  update: (id: number, body: Partial<PromptInput>) =>
    patch<PromptDetail>(`/api/prompts/${id}`, body),
  remove: (id: number) => del<{ok: boolean}>(`/api/prompts/${id}`),
};

/* ── 配置与账号打包导入导出（admin）──────────────────── */
export type ConfigTransferEntry = {
  path: string;
  key: string;
  action: string;
  reason: string;
  diff: string[];
};

export type ConfigTransferIssue = {
  path: string;
  index: number;
  column: string;
  level: string;
  reason: string;
};

export type ConfigTransferReport = {
  mode: string;
  create: ConfigTransferEntry[];
  overwrite: ConfigTransferEntry[];
  skip: ConfigTransferEntry[];
  fail: ConfigTransferEntry[];
  issues: ConfigTransferIssue[];
  totals: {create: number; overwrite: number; skip: number; fail: number; issues: number};
  ok: boolean;
  written: number;
};

export const configTransferApi = {
  /** 导出：responseType=blob，前端自行触发下载；X-Contains-Secrets 头表示是否含明文 */
  download: (includeSecrets: boolean) =>
    http.get<Blob>('/api/config/export', {params: {include_secrets: includeSecrets}, responseType: 'blob'}),
  /** 预检（dry-run）：只出报告，不落库 */
  plan: (bundle: unknown, mode: 'merge' | 'overwrite') =>
    post<ConfigTransferReport>('/api/config/import/plan', {bundle, mode}),
  /** 真正写库；校验失败返 400，响应体带 report */
  apply: async (bundle: unknown, mode: 'merge' | 'overwrite') => {
    const r = await http.post<ConfigTransferReport>(
      '/api/config/import', {bundle, mode, confirm: true}, {validateStatus: () => true},
    );
    return {status: r.status, data: r.data};
  },
};
