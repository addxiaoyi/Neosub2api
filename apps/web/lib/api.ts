import axios, {AxiosError} from 'axios';
import {BASE_PATH} from './base-path';
import {tp} from './i18n';
import type {Realm} from './realm-context';
import type {ProviderCredential, ProviderCredentialInput, ProviderInfo} from './providers';
import type {
  Account,
  CreditExpiry,
  CreditsMeta,
  AccountsResponse,
  ApiKey,
  ApiToken,
  CreatedApiToken,
  Me,
  AuditLogPage,
  GuardInfo,
  LoginFailuresResponse,
  ModelListResponse,
  Page,
  ReloadState,
  RequestLog,
  Changelog,
  StatsSummary,
  UpstreamConfig,
  UpstreamStatus,
  UpdateCheck,
  UpdateStatus,
  UsagePoint,
  UserItem,
  Versions,
  Sub2apiGroup,
  Sub2apiAccount,
  Sub2apiLedgerEntry,
  PriceBookRow,
  PriceResolveResult,
} from './types';

export const http = axios.create({
  // 子路径部署时接口也挂在前缀下（反向代理剥掉前缀再转发给本服务）。
  // 这里必须用 basePath，否则所有请求都会打到域名根路径的 /api 上。
  baseURL: BASE_PATH,
  withCredentials: true,
  timeout: 60000,
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

const get = async <T>(url: string, params?: Record<string, unknown>): Promise<T> =>
  (await http.get<T>(url, {params})).data;
const post = async <T>(url: string, body?: unknown): Promise<T> =>
  (await http.post<T>(url, body)).data;
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
};

/* ── 网关鉴权开关 ─────────────────────────────────────── */
export const gatewayApi = {
  get: () => get<{allow_anonymous: boolean; configured: boolean}>('/api/settings/gateway'),
  update: (allow_anonymous: boolean) =>
    post<{allow_anonymous: boolean; configured: boolean}>('/api/settings/gateway', {allow_anonymous}),
};

/* ── 鉴权 ───────────────────────────────────────────── */
export const authApi = {
  me: () => get<Me>('/api/me'),
  login: (username: string, password: string) =>
    post<{ok: boolean; username: string; role: string}>('/api/login', {username, password}),
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
  /** 单个账号的实时积分（直接向腾讯查询） */
  credits: (file: string) =>
    get<{
      ok: boolean;
      credits: number | null;
      message: string;
      cached: boolean;
      cache_age: number | null;
      expiries?: CreditExpiry[];
    }>(`/api/accounts/${encodeURIComponent(file)}/credits`),
  /** 并发刷新所有账号的实时积分 */
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
};

/* ── 上游状态 ───────────────────────────────────────── */
export const upstreamApi = {
  status: () => get<UpstreamStatus>('/api/status'),
  /** 上游模型简表；realm 非空时只返回该版本的条目 */
  models: (realm?: Realm) => get<ModelListResponse>('/api/models', {realm}),
};

/* ── API 密钥 ───────────────────────────────────────── */
export const keyApi = {
  list: () => get<ApiKey[]>('/api/keys'),
  /** 创建成功时响应里带一次明文 key，此后服务端只存哈希 */
  create: (body: {name: string}) => post<ApiKey>('/api/keys', body),
  update: (id: number, body: {name?: string; enabled?: boolean}) =>
    patch<ApiKey>(`/api/keys/${id}`, body),
  remove: (id: number) => del<{ok: boolean}>(`/api/keys/${id}`),
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
};

/* ── 日志 ───────────────────────────────────────────── */
export const logApi = {
  list: (params: Record<string, unknown>) => get<Page<RequestLog>>('/api/logs', params),
  clear: () => post<{ok: boolean}>('/api/logs/clear'),
};

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
  importBooks: (dry_run = false) =>
    post<{added: number; skipped: number; errors: string[]; path?: string}>(
      `/api/pricing/import?dry_run=${dry_run}`,
    ),
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
};

/* ── 上游配置 / 用户 ───────────────────────────────── */
export const settingsApi = {
  upstream: () => get<UpstreamConfig>('/api/settings/upstream'),
  saveUpstream: (body: Record<string, unknown>) =>
    post<UpstreamConfig>('/api/settings/upstream', body),
  /** 测试 Upstash 连通性；token 留空表示使用已保存的值 */
  testUpstash: (url: string, token?: string) =>
    post<{ok: boolean; message: string}>('/api/settings/upstash/test', {url, token}),
  /** 上游重载状态（保存配置后自动重启） */
  reloadState: () => get<ReloadState>('/api/upstream/reload-state'),
  /** 立即重启上游（一般无需手动调用，保存配置会自动重载） */
  reloadUpstream: () =>
    post<{ok: boolean; message: string}>('/api/settings/upstash/reload'),
  modelMap: () => get<Record<string, string>>('/api/settings/model-map'),
  saveModelMap: (body: Record<string, string>) =>
    post<Record<string, string>>('/api/settings/model-map', body),
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
  /** 检测新版本；force=true 绕过 6 小时缓存 */
  checkUpdate: (force = false) => get<UpdateCheck>('/api/system/check-update', {force}),
  versions: () => get<Versions>('/api/system/versions'),
  /** 更新日志（解析仓库根目录 CHANGELOG.md，离线可用） */
  changelog: () => get<Changelog>('/api/system/changelog'),
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
};
