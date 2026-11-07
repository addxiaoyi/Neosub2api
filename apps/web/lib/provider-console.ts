/** 服务商管理台快照类型：字段与 apps/control/app/routers/provider_console.py 对齐。 */

export interface ConsoleCapabilities {
  chat?: boolean;
  image?: boolean;
  video?: boolean;
  files?: boolean;
  tools?: boolean;
}

export interface ConsoleProvider {
  id: string;
  label: string;
  vendor: string;
  runtime: string;
  status: string;
  risk_level: string;
  capabilities: ConsoleCapabilities;
  requires: string[];
  ready: boolean;
  readiness: string;
  enabled: boolean;
}

export interface ConsoleHealth {
  status: string | null;
  checked_at: number | null;
  latency_ms: number | null;
  error: string;
  enabled: boolean;
  interval_sec: number | null;
}

export interface ConsoleCredential {
  id: number;
  label: string;
  secret_masked: string;
  enabled: boolean;
  sort_order: number;
  has_base_url: boolean;
}

export interface ConsoleRotationEntry {
  id: string;
  state: string;
  cooldown_remaining: number;
  consecutive_failures: number;
  last_used_sec_ago: number | null;
  weight: number;
}

export interface ConsoleRotation {
  rule: string;
  available: number;
  cooling: number;
  model_cooling: number;
  group?: string;
  group_active?: string;
  credentials: ConsoleRotationEntry[];
}

export interface ConsoleAccounts {
  total: number;
  disabled: number;
  expired: number;
  credits: number;
}

export interface ConsoleUsagePoint {
  day: string;
  requests: number;
  failed: number;
  tokens: number;
}

export interface ConsoleUsage {
  window: string;
  requests: number;
  success: number;
  failed: number;
  tokens: number;
  credit_usd: number;
  latency_avg_ms: number;
  top_models: {model: string; requests: number}[];
  daily: ConsoleUsagePoint[];
}

export interface ConsoleLog {
  id: number;
  ts: number;
  model: string;
  status: number;
  latency_ms: number;
  stream: boolean;
  tokens: number;
  error: string;
}

export interface ConsoleProxy {
  enabled: boolean;
  mode: string;
  size: number;
}

export interface ConsoleConfig {
  models: string[];
  model_count: number;
  proxy: ConsoleProxy;
  channel?: string;
  work_mode?: string;
  extra_keys?: number;
  region?: string;
  product?: string;
  session?: {cookie_pool: number; keepalive: boolean; keepalive_interval: number | null};
  media?: {base_configured: boolean; key_configured: boolean};
  claim?: {enabled: boolean; path_configured: boolean; window: number | null; hours: number | null};
  schedule_policy?: string;
  refresh?: boolean;
  allow_external_refresh?: boolean;
  restricted_codes?: number[];
}

export interface ConsoleTaskRun {
  id: string;
  provider: string;
  task: string;
  status: string;
  actor: string;
  stats: Record<string, unknown>;
  message: string;
  started_at: number;
  finished_at: number;
}

export interface ConsoleMediaTask {
  id: string;
  kind: string;
  status: string;
  owner: string;
  created_at: number;
  updated_at: number;
}

export interface ConsoleVideoTask {
  id: string;
  status: string;
  model: string;
  seconds: number;
  billed_micro: number;
  created_at: number;
}

export interface ConsoleSessionPool {
  entries: number;
  due: number;
  updated_at: number;
  fingerprints: string[];
}

export interface ConsoleClaim {
  enabled: boolean;
  path_configured: boolean;
  due: boolean;
  claimed_count: number;
  last_week: string;
  entries: {fingerprint: string; week: string}[];
}

export interface ConsoleFailureActions {
  schedule_policy: string;
  refresh: boolean;
  allow_external_refresh: boolean;
}

export interface ConsoleFeatures {
  queue?: Record<string, number>;
  media_tasks?: ConsoleMediaTask[];
  video_tasks?: ConsoleVideoTask[];
  sessions?: ConsoleSessionPool;
  claim?: ConsoleClaim;
  proxy_pool?: ConsoleProxy;
  failure_actions?: ConsoleFailureActions;
  task_runs?: ConsoleTaskRun[];
  session_cache?: {entries: number; keys: string[]};
}

export interface ProviderConsole {
  provider: ConsoleProvider;
  health: ConsoleHealth;
  rotation: ConsoleRotation | null;
  accounts: ConsoleAccounts | null;
  credentials: ConsoleCredential[];
  config: ConsoleConfig;
  usage: ConsoleUsage;
  logs: ConsoleLog[];
  features: ConsoleFeatures;
  readonly: boolean;
  actions: string[];
}

export interface ConsoleActionReply {
  ok: boolean;
  action: string;
  result: Record<string, unknown>;
  message: string;
}

export interface CodebuddyTaskPage {
  items: ConsoleTaskRun[];
  task_types: string[];
}

export interface CodebuddyTaskReply {
  ok: boolean;
  run: ConsoleTaskRun;
  message: string;
}

export interface ConsoleMediaItem {
  id?: string | number;
  filename?: string;
  bytes?: number;
  created_at?: number;
  status?: string;
  purpose?: string;
  type?: string;
}

export interface ConsoleMediaInventory {
  configured: boolean;
  available: boolean;
  items: ConsoleMediaItem[];
  message: string;
}
