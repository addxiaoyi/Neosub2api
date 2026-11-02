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
  oauth: boolean;
  checkin: boolean;
  credits: boolean;
  register: boolean;
  sidecar: boolean;
}

export type ProviderStatus = 'implemented' | 'stub' | 'sidecar';

/** 许可证/合规风险等级：safe = MIT/Apache/BSD 可自由合入；warn = GPL/LGPL 需传染性隔离；danger = 无 LICENSE 或非商用禁止，仅 sidecar 评估。 */
export type RiskLevel = 'safe' | 'warn' | 'danger';

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
}

export const CODEBUDDY_CAPS: ProviderCapabilities = {
  chat: true,
  messages: true,
  models: true,
  responses: false,
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
