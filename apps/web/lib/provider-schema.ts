import type {ProviderCapabilities, ProviderStatus} from './providers';

/** 设置页服务商高级区的字段描述：只有已在界面上的字段才登记，不新增视觉。 */
export interface ProviderOptionField {
  key: string;
  label: string;
  type: 'text' | 'switch' | 'json';
}

const EXPOSED_OPTIONS: Record<string, ProviderOptionField[]> = {
  muse: [{key: 'timeout', label: 'providers.optTimeout', type: 'text'}],
  gptgrok: [
    {key: 'proxy', label: 'providers.optProxy', type: 'text'},
    {key: 'fingerprint', label: 'providers.optFingerprint', type: 'switch'},
    {key: 'extra_headers', label: 'providers.optExtraHeaders', type: 'json'},
  ],
  zcode: [{key: 'system_prompt_override', label: 'providers.optSystemOverride', type: 'text'}],
};

/** 状态文案映射。 */
export const STATUS_LABEL_KEY: Record<ProviderStatus, string> = {
  implemented: 'providers.statusImplemented',
  stub: 'providers.statusStub',
  sidecar: 'providers.statusSidecar',
};

/** 能力位文案映射，顺序决定表格里的徽章顺序。 */
export const CAPABILITY_LABELS: [keyof ProviderCapabilities, string][] = [
  ['chat', 'providers.capChat'],
  ['messages', 'providers.capMessages'],
  ['models', 'providers.capModels'],
  ['oauth', 'providers.capOAuth'],
  ['checkin', 'providers.capCheckin'],
  ['credits', 'providers.capCredits'],
  ['register', 'providers.capRegister'],
  ['sidecar', 'providers.capSidecar'],
];

export function exposedOptionFields(id: string): ProviderOptionField[] {
  return EXPOSED_OPTIONS[id] ?? [];
}
