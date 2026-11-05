/**
 * 账号页的真实能力与聚合写操作。
 *
 * 为什么单独放在这里：`lib/api.ts` 里的 accountApi 是 CodeBuddy 单家的旧契约，
 * 不知道 provider 参数；而账号页现在要按**后端真实覆写的方法**决定画哪些按钮。
 * 直接复用导出的 axios 实例，鉴权、basePath、401 跳转都保持同一套。
 */
import {http} from '@/lib/api';
import type {Account, CreditsMeta} from '@/lib/types';

export interface AccountOps {
  list: boolean;
  oauth: boolean;
  checkin: boolean;
  credits: boolean;
  test: boolean;
  refresh: boolean;
  clear_cooling: boolean;
  disabled: boolean;
  delete: boolean;
  note: boolean;
  restart: boolean;
}

/** unified = 本页管理；sidecar / quota / external = 账号在别处管理；demo = 演示种子数据，仅展示 */
export type AccountManaged = 'unified' | 'sidecar' | 'quota' | 'external' | 'demo';

export interface ProviderAccountOps {
  id: string;
  label: string;
  enabled: boolean;
  status: 'implemented' | 'stub' | 'sidecar';
  readiness: 'ready' | 'needs_config';
  requires: string[];
  managed: AccountManaged;
  ops: AccountOps;
}

export const NO_ACCOUNT_OPS: AccountOps = {
  list: false,
  oauth: false,
  checkin: false,
  credits: false,
  test: false,
  refresh: false,
  clear_cooling: false,
  disabled: false,
  delete: false,
  note: true,
  restart: false,
};

export interface CreditsRefreshResult {
  total: number;
  succeeded: number;
  credits: Record<string, number | null>;
  meta: Record<string, CreditsMeta>;
  failed: string[];
  message?: string;
}

export interface CheckinAllResult {
  total: number;
  succeeded: number;
  already: number;
  skipped: number;
  results: {nickname: string; ok: boolean; message: string; code?: number; skipped?: boolean; already?: boolean}[];
  message?: string;
}

/** 导入凭证类型（与后端 IMPORT_CREDENTIAL_TYPES 一一对应） */
export type ImportCredentialType = 'api_key' | 'bearer' | 'cookie' | 'json' | 'env';

export interface AccountImportInput {
  provider: string;
  label: string;
  credential_type: ImportCredentialType;
  credential: string;
  base_url?: string;
  note?: string;
}

export interface AccountImportProbeResult {
  status: 'ok' | 'fail' | 'unsupported';
  connected: boolean | null;
  message: string;
  upstream_status: number | null;
}

/** 单个服务商的强制同步结果；note 非空即该家上游失败（后端已脱敏） */
export interface SyncProviderResult {
  synced: number;
  pool_available: boolean;
  note: string;
}

/** 只有本仓能真正落库并消费凭证的账号类型给导入入口；sidecar/demo 给了就是假按钮 */
export function canImportManaged(managed?: AccountManaged): boolean {
  return managed === 'unified' || managed === 'external' || managed === 'quota';
}

export const accountOpsApi = {
  capabilities: async (): Promise<ProviderAccountOps[]> =>
    (await http.get<{providers: ProviderAccountOps[]}>('/api/accounts/capabilities')).data.providers,
  refreshCredits: async (force: boolean, provider?: string): Promise<CreditsRefreshResult> =>
    (
      await http.post<CreditsRefreshResult>('/api/accounts/refresh-credits', null, {
        params: {force, provider},
      })
    ).data,
  checkinAll: async (provider?: string): Promise<CheckinAllResult> =>
    (await http.post<CheckinAllResult>('/api/accounts/checkin-all', null, {params: {provider}})).data,
  restart: async (provider?: string): Promise<{ok: boolean; message?: string}> =>
    (await http.post<{ok: boolean; message?: string}>('/api/accounts/restart', null, {params: {provider}})).data,
  /** 贴凭证导入一条账号；返回体与 GET /api/accounts 单条同构 */
  importAccount: async (body: AccountImportInput): Promise<Account> =>
    (await http.post<Account>('/api/accounts/import', body)).data,
  /** 保存前连通性探测；失败不阻断保存，只回结构化结果 */
  probeImport: async (body: AccountImportInput): Promise<AccountImportProbeResult> =>
    (await http.post<AccountImportProbeResult>('/api/accounts/import/probe', body)).data,
  /** 手动强制同步（绕过 30 秒节流）；按 provider 分组返回，admin-only */
  syncNow: async (provider?: string): Promise<Record<string, SyncProviderResult>> =>
    (await http.post<Record<string, SyncProviderResult>>('/api/accounts/sync', null, {params: {provider}})).data,
};
