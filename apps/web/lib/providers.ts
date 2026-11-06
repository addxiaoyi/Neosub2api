/**
 * provider（服务商）的前端描述层。
 *
 * 账号/设置页里原本写死的 CodeBuddy 字段（签到、积分、OAuth、版本 realm）都收在这里，
 * 页面只按能力位决定「显示什么、能点什么」，新增 provider 时不需要再改页面。
 */

export interface ProviderCapabilities {
  chat: boolean;
  messages: boolean;
  models: boolean;
  responses: boolean;
  search: boolean;
  oauth: boolean;
  checkin: boolean;
  credits: boolean;
  register: boolean;
  sidecar: boolean;
  // 扩展位：后端普遍输出，但也有手写 dict 的 sidecar 只输出自己声明的子集，故为可选
  embeddings?: boolean;
  images?: boolean;
  videos?: boolean;
  media?: boolean;
  rerank?: boolean;
  usage?: boolean;
  count_tokens?: boolean;
}

export type ProviderStatus = 'implemented' | 'stub' | 'sidecar';

/** 就绪状态：ready = 填好密钥/地址即可调用；needs_config = 还需补齐 requires 里列出的项。 */
export type ProviderReadiness = 'ready' | 'needs_config';

/** 本地 JWT 判定结果（不验签、不发起外呼，明文不回显）。 */
export interface ProviderTokenCheck {
  state: 'valid' | 'expired' | 'invalid' | 'missing';
  expires_at: number | null;
  expires_in: number | null;
  reason: string;
  next_step: string;
}

/** 统一凭据库里的一条记录（secret 只回掩码，明文永不下发）。 */
export interface ProviderCredential {
  id: number;
  provider: string;
  label: string;
  base_url: string;
  secret_masked: string;
  has_secret: boolean;
  enabled: boolean;
  sort_order: number;
  note: string;
  created_at: number;
  updated_at: number;
}

export interface ProviderCredentialInput {
  provider: string;
  label?: string;
  base_url?: string;
  secret?: string;
  enabled?: boolean;
  sort_order?: number;
  note?: string;
}

/** 许可证/合规风险等级：safe = MIT/Apache/BSD 可自由合入；warn = GPL/LGPL 需传染性隔离；danger = 无 LICENSE 或非商用禁止，仅 sidecar 评估。 */
export type RiskLevel = 'safe' | 'warn' | 'danger';

/** 健康检测三档：ok = 探测通过；failed = 探测失败（如实记录）；unsupported = 该 provider 没有可探测的能力，不算失败。 */
export type ProviderHealthStatus = 'ok' | 'failed' | 'unsupported';

/** 某家 provider 的最近一次检测结果；checked_at 为空即「未检测」。 */
export interface ProviderHealthEntry {
  provider: string;
  status: ProviderHealthStatus | null;
  checked_at: number | null;
  latency_ms: number | null;
  error: string;
}

export interface ProviderHealthCheck {
  id: number;
  provider: string;
  status: ProviderHealthStatus;
  started_at: number;
  finished_at: number;
  latency_ms: number;
  error: string;
}

export interface ProviderHealth {
  ok: boolean;
  enabled: boolean;
  interval_sec: number;
  providers: ProviderHealthEntry[];
  history: ProviderHealthCheck[];
}

/** 凭据状态：valid = 探活通过；missing = 没配；invalidated = 上游明确拒了/本地确认过期；unknown = 还没有证据。 */
export type ProviderCredentialState = 'valid' | 'missing' | 'invalidated' | 'unknown';

/** 这家能判到什么程度；note 非空说明为什么只能给「未知」。 */
export type ProviderCredentialStateSupport =
  | 'auth_probe'
  | 'token_expiry'
  | 'request_log'
  | 'unavailable';

export interface ProviderInfo {
  id: string;
  name: string;
  label: string;
  vendor: string;
  license: string;
  runtime: string;
  status: ProviderStatus;
  description: string;
  base_url: string;
  default_base_url: string;
  enabled: boolean;
  capabilities: ProviderCapabilities;
  protocols: string[];
  has_api_key: boolean;
  options: Record<string, unknown>;
  risk_level: RiskLevel;
  risk_note: string;
  /** 能力限制：key 为受限能力（如 messages_stream），value 为限制说明 */
  limitations: Record<string, string>;
  readiness: ProviderReadiness;
  /** needs_config 时仍缺的项，给人看的短句 */
  requires: string[];
  /** 凭据状态由后端按库内探活/真实请求证据判定，前端只读展示，不自行推断 */
  credential_state: ProviderCredentialState;
  credential_state_reason: string;
  credential_state_checked_at: number | null;
  credential_state_evidence: 'probe' | 'request' | 'expiry' | 'format' | 'none';
  credential_state_support: ProviderCredentialStateSupport;
  /** support 为 request_log / unavailable 时说明为什么判不出，空串表示可判 */
  credential_state_note: string;
  /** sidecar 存活探测结果；非 sidecar 家没有这个字段 */
  sidecar_status?: {
    state: 'alive' | 'auth' | 'timeout' | 'refused' | 'http_error';
    port: number;
    base_url: string;
    path: string;
    http_status: number | null;
    latency_ms: number | null;
    checked_at: number;
    message: string;
  };
  base_url_source: string;
  /** 上游默认地址是否 loopback，决定本地地址能不能保存 */
  allows_local_base_url: boolean;
  /** 历史默认地址残留提示，空串表示无异常 */
  base_url_warning: string;
  /** 命中历史默认值的凭据行提示，命中为空数组 */
  credential_base_warnings: {
    provider: string;
    id: number;
    label: string;
    base_url: string;
    suggested_base_url: string;
    message: string;
  }[];
  credential_count: number;
  credentials: {
    id: number;
    label: string;
    base_url: string;
    secret_masked: string;
    sort_order: number;
  }[];
}

export const CODEBUDDY_CAPS: ProviderCapabilities = {
  chat: true,
  messages: true,
  models: true,
  responses: false,
  search: false,
  oauth: true,
  checkin: true,
  credits: true,
  register: false,
  sidecar: false,
};

const NO_CAPS: ProviderCapabilities = {
  chat: false,
  messages: false,
  models: false,
  responses: false,
  search: false,
  oauth: false,
  checkin: false,
  credits: false,
  register: false,
  sidecar: false,
};

/** 账号没有 provider 字段时按 CodeBuddy 处理——存量账号全是它。 */
export function capabilitiesOf(providers: ProviderInfo[], providerId?: string): ProviderCapabilities {
  if (!providerId || providerId === 'codebuddy') return CODEBUDDY_CAPS;
  return providers.find((p) => p.id === providerId)?.capabilities ?? NO_CAPS;
}

export function providerLabel(providers: ProviderInfo[], providerId?: string): string {
  if (!providerId || providerId === 'codebuddy') return 'CodeBuddy';
  return providers.find((p) => p.id === providerId)?.label || providerId;
}

type Translate = (key: string, params?: Record<string, string | number>) => string;

/** sidecar 状态由后端给出结构化字段，文案在前端组装——避免把中文运行时消息直接透传到其他语言界面。 */
export function sidecarMessage(
  status: NonNullable<ProviderInfo['sidecar_status']>,
  t: Translate,
): string {
  const port = status.port;
  switch (status.state) {
    case 'alive':
      return t('providers.sidecar.alive', {port, ms: status.latency_ms ?? 0});
    case 'auth':
      return t('providers.sidecar.auth', {
        port,
        path: status.path,
        code: status.http_status ?? '',
      });
    case 'timeout':
      return t('providers.sidecar.timeout', {port});
    case 'refused':
      return t('providers.sidecar.refused', {port});
    case 'http_error':
      return status.http_status
        ? t('providers.sidecar.httpError', {
            port,
            path: status.path,
            code: status.http_status,
          })
        : t('providers.sidecar.probeFailed', {port});
  }
}

/**
 * 上游数据里的短句（requires / limitations / risk_note / vendor）是服务端中文原文，
 * 显示前统一过 phrases；未收录的新句子回退原文，不会显示英文键名。
 */
export function localizedRequires(p: ProviderInfo, t: Translate, tp: Translate): string[] {
  if (p.sidecar_status && p.sidecar_status.state !== 'alive') {
    return [sidecarMessage(p.sidecar_status, t)];
  }
  return p.requires.map((item) => tp(item));
}

/** 后端拼好的「前缀 + 详情」中文串，只能按前缀拆开重拼，否则整句永远对不上短语表。 */
const REASON_PREFIXES: {prefix: string; key: string}[] = [
  {prefix: '凭据已失效，需要重新粘贴：', key: 'providers.credentialReasonInvalidated'},
  {prefix: '凭据不可用，需要重新粘贴：', key: 'providers.credentialReasonUnusable'},
  {prefix: '上次探活失败：', key: 'providers.credentialReasonProbeFailed'},
];

function reasonDetail(detail: string, t: Translate): string {
  if (detail === '上游判定凭据被拒') return t('providers.upstreamRejected');
  const status = /^上游返回 (\d+)$/.exec(detail);
  if (status) return t('providers.upstreamStatus', {code: status[1]});
  return detail;
}

/** 凭据说明本地化：固定句查短语表，带动态详情的前缀句用结构化字段重拼。 */
export function credentialNote(p: ProviderInfo, t: Translate, tp: Translate): string {
  const raw = p.credential_state_note || p.credential_state_reason;
  if (!raw) return '';
  for (const item of REASON_PREFIXES) {
    if (raw.startsWith(item.prefix)) {
      return t(item.key, {detail: reasonDetail(raw.slice(item.prefix.length), t)});
    }
  }
  return tp(raw);
}

/** 历史默认地址提示是后端拼好的中文串，这里只用它当信号，正文按结构化字段重拼。 */
export function legacyBaseWarning(p: ProviderInfo, t: Translate): string {
  return p.base_url_warning ? t('providers.legacyBaseWarning', {url: p.base_url}) : '';
}
