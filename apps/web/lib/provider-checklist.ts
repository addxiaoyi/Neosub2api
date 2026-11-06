import type {ProviderInfo} from './providers';

export type ChecklistStep = {
  id: string;
  kind: 'enable' | 'credential' | 'option' | 'sidecar' | 'quota' | 'health' | 'request';
  /** 该步骤的说明文本（来自 provider.requires 或静态映射，中文内容） */
  detail: string;
  /** 缺省动作；可选命令行提示 */
  action?: 'enable' | 'credential' | 'option' | 'health' | 'console' | 'quota' | 'playground';
  cli?: string;
};

export type ChecklistState = {steps: ChecklistStep[]; done: boolean[]; remaining: number; current: number};

type PresetStep = Omit<ChecklistStep, 'id'> & {
  /** detail 优先取 provider.requires 拼串，后端没给时退回静态短句 */
  fromRequires?: boolean;
  /** option 步的完成判定：这些 options 字段全部非空才算完成 */
  optionKeys?: string[];
};

/** 各家的前置步骤；启用开关独立，不计入清单和剩余步数。 */
const PRESETS: Record<string, PresetStep[]> = {
  codebuddy: [
    {kind: 'sidecar', detail: '启动 7864 管理端（127.0.0.1:7864）', action: 'console'},
    {kind: 'credential', detail: '填 wbt_ token', action: 'credential'},
  ],
  muse: [
    {kind: 'credential', detail: '填官方 OpenAI 兼容入口的 api_key', action: 'credential', fromRequires: true},
  ],
  'grok-register': [
    {
      kind: 'option',
      detail: '配置浏览器执行器地址（GROK_REGISTER_EXECUTOR_URL）',
      action: 'console',
      fromRequires: true,
    },
  ],
  opencode: [
    {kind: 'credential', detail: '填 OpenCode Zen/Go 密钥（zen_keys / go_keys）', action: 'credential', fromRequires: true},
  ],
  trae: [{kind: 'credential', detail: '填 Trae 登录后的 Cloud-IDE JWT', action: 'credential', fromRequires: true}],
  codar: [{kind: 'credential', detail: '填 Codar 账号 JWT', action: 'credential', fromRequires: true}],
  gptgrok: [
    {kind: 'credential', detail: '填 GPT/Grok 账号凭据（api_key 或 extra_keys）', action: 'credential'},
    {kind: 'option', detail: '填 options.sentinel_token', action: 'option', optionKeys: ['sentinel_token']},
  ],
  zcode: [
    {kind: 'credential', detail: '填 ZCode 密钥（api_key 或 extra_keys）', action: 'credential'},
    {kind: 'option', detail: '填 options.official_system', action: 'option', optionKeys: ['official_system']},
  ],
  qoder: [
    {kind: 'credential', detail: '填 Qoder access token（api_key）', action: 'credential'},
    {
      kind: 'option',
      detail: '填 options.uid 与 options.refresh_token',
      action: 'option',
      optionKeys: ['uid', 'refresh_token'],
    },
  ],
  sub2api: [
    {kind: 'quota', detail: '建配额分组', action: 'quota'},
    {kind: 'quota', detail: '导入订阅账号', action: 'quota'},
  ],
  trae_sidecar: [
    {
      kind: 'sidecar',
      detail: '启动 sidecar 进程（trae2api，默认端口 7871）',
      action: 'console',
      cli: 'go run ./cmd/server -config config.example.json',
      fromRequires: true,
    },
  ],
  deepseek_sidecar: [
    {
      kind: 'sidecar',
      detail: '启动 sidecar 进程（Deepseek2Go，默认端口 5001）',
      action: 'console',
      cli: 'go run ./cmd/server',
      fromRequires: true,
    },
  ],
  qoder_sidecar: [
    {
      kind: 'sidecar',
      detail: '启动 sidecar 进程（buddy-switch-gateway，默认端口 7873）',
      action: 'console',
      fromRequires: true,
    },
  ],
  codar_sidecar: [
    {
      kind: 'sidecar',
      detail: '启动 sidecar 进程（codar2api，默认端口 8801）',
      action: 'console',
      fromRequires: true,
    },
    {kind: 'option', detail: '在面板完成登录', action: 'console'},
  ],
  demo_local: [],
};

/** 前置步骤之后固定追加：连通性检查 + 真实请求。 */
const TAIL: PresetStep[] = [
  {kind: 'health', detail: 'providers.checklist.healthDetail', action: 'health'},
  {kind: 'request', detail: 'providers.checklist.requestDetail', action: 'playground'},
];

function stepDone(step: PresetStep, p: ProviderInfo, healthStatus: string | null): boolean {
  switch (step.kind) {
    case 'enable':
      return p.enabled;
    case 'credential':
      return p.has_api_key || p.credential_count > 0;
    case 'option':
      // 没声明 optionKeys 的步骤无法只读判定（如 codar_sidecar 的面板登录），一律算未完成
      return (step.optionKeys ?? []).length > 0 && (step.optionKeys ?? []).every((key) => Boolean(p.options[key]));
    case 'sidecar':
      // codebuddy 没有 sidecar_status，7864 管理端是否在线只能靠 health 探测结果
      return p.sidecar_status?.state === 'alive' || (p.id === 'codebuddy' && healthStatus === 'ok');
    case 'quota':
      return false;
    case 'health':
      return healthStatus === 'ok';
    case 'request':
      return false;
  }
}

export function buildChecklist(p: ProviderInfo, healthStatus: string | null): ChecklistState {
  const fallback: PresetStep[] = [
    {kind: 'credential', detail: p.requires.join('；') || '补齐上游所需配置', action: 'credential'},
  ];
  const pre = (PRESETS[p.id] ?? fallback).map((step) => ({
    ...step,
    detail: step.fromRequires && p.requires.length ? p.requires.join('；') : step.detail,
  }));
  const raw = [...pre, ...TAIL];

  const steps: ChecklistStep[] = raw.map((step, i) => ({
    id: `pre-${i}`,
    kind: step.kind,
    detail: step.detail,
    ...(step.action ? {action: step.action} : {}),
    ...(step.cli ? {cli: step.cli} : {}),
  }));

  if (p.readiness === 'ready') {
    return {steps, done: steps.map(() => true), remaining: 0, current: -1};
  }

  const done = raw.map((step) => stepDone(step, p, healthStatus));
  // 启用是独立开关，不参与剩余步数与「当前卡住」判定
  const active = steps.map((step, i) => (step.kind === 'enable' ? true : done[i]));
  const current = active.indexOf(false);
  return {
    steps,
    done,
    remaining: active.filter((value) => !value).length,
    current: current === -1 ? -1 : current,
  };
}
