import type {ProviderCapabilities, ProviderStatus, RiskLevel} from './providers';

/**
 * Provider descriptor：新增一家 2api 时，适配器负责协议，descriptor 负责界面。
 *
 * 字段值来自 apps/control/app/adapters/*.py 的 spec 与 configure/options 读取逻辑，
 * 不编造。configFields 的 scope=connection 对应 base_url / api_key；
 * scope=option 对应 settings 表里 provider_config.{id} 的 options。
 *
 * exposed 用于保持现有服务商页的高级区字段集合不变：只有迁移时已经在界面上的
 * 字段才标记 true，其余字段照样登记在 descriptor 里，供详情抽屉与后续页面使用。
 */

export type ProviderFieldType = 'text' | 'password' | 'number' | 'switch' | 'select' | 'json';

export interface ProviderConfigField {
  key: string;
  /** i18n 键或直接中文：t() 对未知键会原样返回，两种写法都能显示。 */
  label: string;
  type: ProviderFieldType;
  scope: 'connection' | 'option';
  required?: boolean;
  /** 敏感字段：表单给密码框，列表/详情不回显明文。 */
  sensitive?: boolean;
  description?: string;
  /** 是否在现有服务商表格的高级区渲染。 */
  exposed?: boolean;
  options?: {value: string; label: string}[];
  placeholder?: string;
}

export interface ProviderDescriptor {
  id: string;
  displayName: string;
  vendor: string;
  license: string;
  runtime: string;
  status: ProviderStatus;
  description: string;
  defaultBaseUrl: string;
  protocols: string[];
  capabilities: ProviderCapabilities;
  riskLevel: RiskLevel;
  riskNote: string;
  configFields: ProviderConfigField[];
}

type CapabilityKey = keyof ProviderCapabilities;

function caps(...enabled: CapabilityKey[]): ProviderCapabilities {
  const value: ProviderCapabilities = {
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
  for (const key of enabled) value[key] = true;
  return value;
}

function baseUrl(defaultBaseUrl: string, description?: string): ProviderConfigField {
  return {
    key: 'base_url',
    label: '服务地址',
    type: 'text',
    scope: 'connection',
    description: description ?? `留空使用默认值 ${defaultBaseUrl || '（无）'}`,
    placeholder: defaultBaseUrl,
  };
}

function apiKey(required = false, description = '上游鉴权密钥'): ProviderConfigField {
  return {
    key: 'api_key',
    label: 'API Key',
    type: 'password',
    scope: 'connection',
    required,
    sensitive: true,
    description,
  };
}

function timeoutOption(): ProviderConfigField {
  return {
    key: 'timeout',
    label: 'providers.optTimeout',
    type: 'number',
    scope: 'option',
    description: '上游请求超时，单位秒；留空按适配器默认值',
    placeholder: '120',
  };
}

function proxyOption(): ProviderConfigField {
  return {
    key: 'proxy',
    label: 'providers.optProxy',
    type: 'text',
    scope: 'option',
    description: '显式出口代理，只作用于该服务商的上游请求',
    placeholder: 'http://127.0.0.1:7890',
  };
}

function extraHeadersOption(): ProviderConfigField {
  return {
    key: 'extra_headers',
    label: 'providers.optExtraHeaders',
    type: 'json',
    scope: 'option',
    description: '注入到上游请求的自定义头（JSON 对象）',
    placeholder: '{"User-Agent": "..."}',
  };
}

export const PROVIDER_DESCRIPTORS: ProviderDescriptor[] = [
  {
    id: 'codebuddy',
    displayName: 'CodeBuddy',
    vendor: '腾讯 CodeBuddy（addxiaoyi/codebuddy2api）',
    license: 'MIT',
    runtime: 'Python 管理端（7864）+ Go 数据面（workbuddy2api, 7863）',
    status: 'implemented',
    description: '腾讯 CodeBuddy 账号池反向代理，OpenAI Chat Completions / Anthropic Messages 双协议。',
    defaultBaseUrl: 'http://127.0.0.1:7864',
    protocols: ['openai.chat.completions', 'anthropic.messages'],
    capabilities: caps('chat', 'messages', 'models', 'oauth', 'checkin', 'credits'),
    riskLevel: 'safe',
    riskNote: '',
    configFields: [
      baseUrl('http://127.0.0.1:7864'),
      apiKey(false, 'CodeBuddy 管理端密钥；留空时回落到控制面配置'),
    ],
  },
  {
    id: 'muse',
    displayName: 'Muse2API',
    vendor: 'czg86389-hub/muse2api',
    license: 'MIT',
    runtime: '容器/二进制独立服务',
    status: 'implemented',
    description: 'muse 上游的 OpenAI 兼容代理，chat + models 直通转发。',
    defaultBaseUrl: 'http://127.0.0.1:7865',
    protocols: ['openai.chat.completions'],
    capabilities: caps('chat', 'models'),
    riskLevel: 'safe',
    riskNote: '',
    configFields: [
      baseUrl('http://127.0.0.1:7865'),
      apiKey(false, 'MUSE2API_KEY，以 Bearer 形式注入'),
      {...timeoutOption(), exposed: true},
      proxyOption(),
      extraHeadersOption(),
    ],
  },
  {
    id: 'grok-register',
    displayName: 'Grok Register',
    vendor: 'kaibush/grok-register（协议参考）',
    license: '本仓自研（MIT）',
    runtime: '控制面内置任务服务',
    status: 'implemented',
    description: '重写的注册任务编排：任务队列、并发控制、状态机与失败重试；浏览器执行由外部运行时承担。',
    defaultBaseUrl: '',
    protocols: [],
    capabilities: caps('register'),
    riskLevel: 'safe',
    riskNote: '',
    configFields: [],
  },
  {
    id: 'gptgrok',
    displayName: 'GPT/Grok 2API',
    vendor: 'addxiaoyi/gptGrok2api',
    license: 'MIT',
    runtime: '容器/二进制独立服务',
    status: 'implemented',
    description: 'GPT 与 Grok 上游的 OpenAI 兼容反代，chat + models 直通，支持代理与指纹头开关。',
    defaultBaseUrl: 'http://127.0.0.1:7867',
    protocols: ['openai.chat.completions'],
    capabilities: caps('chat', 'models'),
    riskLevel: 'safe',
    riskNote: '',
    configFields: [
      baseUrl('http://127.0.0.1:7867'),
      apiKey(),
      timeoutOption(),
      {...proxyOption(), exposed: true},
      {
        key: 'fingerprint',
        label: 'providers.optFingerprint',
        type: 'switch',
        scope: 'option',
        description: '为上游请求追加指纹头；上游自身已有指纹处理时保持关闭',
        exposed: true,
      },
      {...extraHeadersOption(), exposed: true},
    ],
  },
  {
    id: 'zcode',
    displayName: 'ZCode 2API',
    vendor: 'addxiaoyi/ZCode2api',
    license: 'MIT',
    runtime: 'Node 独立服务（zcgw-server.mjs）',
    status: 'implemented',
    description: 'ZCode 上游的 OpenAI/Anthropic 兼容反代，chat + messages + models 直通，system 改写可开关。',
    defaultBaseUrl: 'http://127.0.0.1:8787',
    protocols: ['openai.chat.completions', 'anthropic.messages'],
    capabilities: caps('chat', 'messages', 'models'),
    riskLevel: 'safe',
    riskNote: '',
    configFields: [
      baseUrl('http://127.0.0.1:8787'),
      apiKey(),
      timeoutOption(),
      proxyOption(),
      extraHeadersOption(),
      {
        key: 'system_prompt_override',
        label: 'providers.optSystemOverride',
        type: 'text',
        scope: 'option',
        description: '非空时作为 system 消息置顶注入；默认关闭，避免与上游改写叠加',
        exposed: true,
      },
    ],
  },
  {
    id: 'opencode',
    displayName: 'OpenCode 2API',
    vendor: 'jasonxu114514/opencode2api（协议参考）',
    license: '本仓自研（MIT）',
    runtime: '主仓内置（直连 Zen/Go）',
    status: 'implemented',
    description: '按公开协议契约重写的 Zen/Go 客户端：/v1/models 与 chat/messages 转发，SSE 直通。',
    defaultBaseUrl: 'https://opencode.ai/zen',
    protocols: ['openai.chat.completions', 'anthropic.messages'],
    capabilities: caps('chat', 'messages', 'models'),
    riskLevel: 'safe',
    riskNote: '',
    configFields: [
      baseUrl('https://opencode.ai/zen'),
      apiKey(true, 'Zen/Go API Key；多 key 轮换时主 key 放这里'),
      {
        key: 'tier',
        label: '套餐层级',
        type: 'select',
        scope: 'option',
        description: 'zen 走默认网关，go 自动追加 /go 前缀',
        options: [
          {value: 'zen', label: 'Zen'},
          {value: 'go', label: 'Go'},
        ],
      },
      {
        key: 'extra_keys',
        label: '备用 Key 列表',
        type: 'json',
        scope: 'option',
        description: '字符串数组；可重试错误发生时按顺序换 key',
        placeholder: '["key-2", "key-3"]',
      },
      {
        key: 'max_attempts',
        label: '最大尝试次数',
        type: 'number',
        scope: 'option',
        description: '默认 3，上限 10；实际次数还受 key 数量约束',
        placeholder: '3',
      },
      timeoutOption(),
      proxyOption(),
      extraHeadersOption(),
    ],
  },
  {
    id: 'sub2api',
    displayName: 'Sub2API',
    vendor: 'Wei-Shaw/sub2api（协议参考）',
    license: '本仓自研（MIT）',
    runtime: '主仓内置（配额分发）',
    status: 'implemented',
    description: '重写的订阅额度分发：分组、账号池、token 配额扣减，额度耗尽返回 429。',
    defaultBaseUrl: '',
    protocols: ['openai.chat.completions', 'anthropic.messages'],
    capabilities: caps('chat', 'messages', 'models'),
    riskLevel: 'safe',
    riskNote: '',
    configFields: [
      baseUrl('', '账号自带 upstream_base 时可不填'),
      apiKey(false, '兜底 API Key：账号没有自带 key 时使用'),
      {
        key: 'group',
        label: '分组',
        type: 'text',
        scope: 'option',
        description: '配额分组 ID，默认 default',
        placeholder: 'default',
      },
      timeoutOption(),
      proxyOption(),
    ],
  },
  {
    id: 'trae',
    displayName: 'Trae（Solo / Work）',
    vendor: 'Trae2API（协议参考）',
    license: '本仓自研（MIT）',
    runtime: '主仓内置（直连 Solo / Work）',
    status: 'implemented',
    description: '按协议契约重写的 Trae 客户端：Solo 单端点 chat + Work 有状态桥，积分不足自动降级 Solo。',
    defaultBaseUrl: 'https://trae-api-cn.mchost.guru',
    protocols: ['openai.chat.completions'],
    capabilities: caps('chat', 'models'),
    riskLevel: 'safe',
    riskNote: '',
    configFields: [
      baseUrl('https://trae-api-cn.mchost.guru'),
      apiKey(true, 'Trae JWT；设备头与设备 ID 由适配器按契约自行推导'),
      {
        key: 'uid',
        label: 'UID',
        type: 'text',
        scope: 'option',
        description: '留空从 JWT claims 推导',
      },
      {
        key: 'machine_id',
        label: '机器 ID',
        type: 'text',
        scope: 'option',
        description: '留空按 JWT 推导，固定值可复现同一设备',
      },
      {
        key: 'device_id',
        label: '设备 ID',
        type: 'text',
        scope: 'option',
        description: '留空按 JWT 推导',
      },
      timeoutOption(),
      proxyOption(),
      {
        key: 'work_host',
        label: 'Work 网关地址',
        type: 'text',
        scope: 'option',
        description: '默认 https://api5-normal.mchost.guru',
        placeholder: 'https://api5-normal.mchost.guru',
      },
    ],
  },
  {
    id: 'trae_sidecar',
    displayName: 'Trae Solo+Work (trae2api)',
    vendor: 'JeffHu0912/trae2api',
    license: 'MIT',
    runtime: '独立进程 sidecar（Go）',
    status: 'sidecar',
    description: 'Trae Solo 裸模型 + Work 积分通道的自动降级双通道。sidecar 自行管理账号池与 OAuth。',
    defaultBaseUrl: 'http://127.0.0.1:7871',
    protocols: ['openai.chat.completions'],
    capabilities: caps('chat', 'models', 'oauth', 'credits', 'sidecar'),
    riskLevel: 'safe',
    riskNote: '',
    configFields: [baseUrl('http://127.0.0.1:7871'), timeoutOption(), proxyOption(), extraHeadersOption()],
  },
  {
    id: 'deepseek_sidecar',
    displayName: 'DeepSeek (Deepseek2Go)',
    vendor: 'ColudAI/Deepseek2Go',
    license: '无 LICENSE（All Rights Reserved）',
    runtime: '独立进程 sidecar（Go + WASM PoW）',
    status: 'sidecar',
    description: 'DeepSeek 官方 chat.deepseek.com 代理，含 WASM PoW。sidecar 自行管理账号登录与 token 刷新。',
    defaultBaseUrl: 'http://127.0.0.1:7872',
    protocols: ['openai.chat.completions', 'anthropic.messages'],
    capabilities: caps('chat', 'messages', 'models', 'sidecar'),
    riskLevel: 'danger',
    riskNote: '上游无 LICENSE，版权全保留。本 provider 仅供个人评估，商用前必须移除 sidecar 及其适配器。',
    configFields: [baseUrl('http://127.0.0.1:7872'), timeoutOption(), proxyOption(), extraHeadersOption()],
  },
  {
    id: 'qoder',
    displayName: 'Qoder（COSY）',
    vendor: 'Qoder（协议参考）',
    license: '本仓自研（MIT）',
    runtime: '主仓内置（直连 COSY 网关）',
    status: 'implemented',
    description: 'COSY 签名网关：OpenAI / Anthropic / Responses 三套协议转换，多产品线 × 多地域路由与 fallback。',
    defaultBaseUrl: 'https://gateway.qoder.com.cn',
    protocols: ['openai.chat.completions', 'anthropic.messages', 'openai.responses'],
    capabilities: caps('chat', 'messages', 'models', 'responses'),
    riskLevel: 'safe',
    riskNote: '',
    configFields: [
      baseUrl('https://gateway.qoder.com.cn'),
      apiKey(true, 'Qoder OAuth Token；会话失效码 12153 会直接返回 429'),
      {
        key: 'region',
        label: '地域',
        type: 'select',
        scope: 'option',
        description: '模型名可用 cn: / global: 前缀覆盖单次请求',
        options: [
          {value: 'cn', label: 'cn（中国）'},
          {value: 'global', label: 'global（国际）'},
        ],
      },
      {
        key: 'product',
        label: '产品线',
        type: 'select',
        scope: 'option',
        description: '模型名可用 qoder/ / workbuddy/ / trae/ 前缀覆盖',
        options: [
          {value: 'qoder', label: 'qoder'},
          {value: 'workbuddy', label: 'workbuddy'},
          {value: 'trae', label: 'trae'},
        ],
      },
      {...proxyOption(), description: '地域代理；留空直连，错误信息会注明未配置代理'},
      {
        key: 'region_proxies',
        label: '分地域代理',
        type: 'json',
        scope: 'option',
        description: '{"cn": "...", "global": "..."}；优先于 proxy',
        placeholder: '{"global": "http://127.0.0.1:7890"}',
      },
      {
        key: 'gateway_base',
        label: '网关地址覆盖',
        type: 'text',
        scope: 'option',
        description: '只走该地址，不追加默认 fallback 链',
      },
      {
        key: 'workbuddy_base',
        label: 'WorkBuddy 地址',
        type: 'text',
        scope: 'option',
        description: 'workbuddy 产品线的上游地址覆盖',
      },
      {key: 'uid', label: 'UID', type: 'text', scope: 'option', description: '留空按 token 哈希推导'},
      {key: 'organization_id', label: '组织 ID', type: 'text', scope: 'option'},
      {key: 'organization_name', label: '组织名称', type: 'text', scope: 'option'},
      {
        key: 'user_type',
        label: '用户类型',
        type: 'text',
        scope: 'option',
        description: '默认 personal_professional_trial',
      },
      {
        key: 'refresh_token',
        label: '刷新令牌',
        type: 'password',
        scope: 'option',
        sensitive: true,
        description: '可选；适配器不会主动刷新，仅随会话上报',
      },
      {key: 'nickname', label: '昵称', type: 'text', scope: 'option'},
      {key: 'user_agent', label: 'User-Agent', type: 'text', scope: 'option'},
      timeoutOption(),
    ],
  },
  {
    id: 'qoder_sidecar',
    displayName: 'Qoder Switch (PolyForm-NC)',
    vendor: 'NextAgentX/qoder-trae-workbuddy-switch',
    license: 'PolyForm Noncommercial 1.0.0（禁止商用）',
    runtime: '独立进程 sidecar（Rust Axum gateway）',
    status: 'sidecar',
    description: '多产品线多地域账号池 + /v1/chat|messages|models|responses + 降级链。sidecar 自行管理账号切换。',
    defaultBaseUrl: 'http://127.0.0.1:7873',
    protocols: ['openai.chat.completions', 'anthropic.messages'],
    capabilities: caps('chat', 'messages', 'models', 'sidecar'),
    riskLevel: 'danger',
    riskNote: 'PolyForm Noncommercial 1.0.0 禁止任何商用（包括内部商业项目与托管服务）。作者书面授权前不得商用。',
    configFields: [baseUrl('http://127.0.0.1:7873'), timeoutOption(), proxyOption(), extraHeadersOption()],
  },
];

export function descriptorOf(id: string): ProviderDescriptor | undefined {
  return PROVIDER_DESCRIPTORS.find((item) => item.id === id);
}

/** 状态文案映射：新增 provider 复用三档状态，不需要在页面里加分支。 */
export const STATUS_LABEL_KEY: Record<ProviderStatus, string> = {
  implemented: 'providers.statusImplemented',
  stub: 'providers.statusStub',
  sidecar: 'providers.statusSidecar',
};

/** 能力位文案映射，顺序决定表格里的徽章顺序。 */
export const CAPABILITY_LABELS: [CapabilityKey, string][] = [
  ['chat', 'providers.capChat'],
  ['messages', 'providers.capMessages'],
  ['models', 'providers.capModels'],
  ['oauth', 'providers.capOAuth'],
  ['checkin', 'providers.capCheckin'],
  ['credits', 'providers.capCredits'],
  ['register', 'providers.capRegister'],
  ['sidecar', 'providers.capSidecar'],
];

/** 给现有服务商页高级区用的字段列表；只有 exposed=true 的 option 字段会渲染。 */
export function exposedOptionFields(id: string): ProviderConfigField[] {
  return (descriptorOf(id)?.configFields ?? []).filter(
    (field) => field.scope === 'option' && field.exposed,
  );
}
