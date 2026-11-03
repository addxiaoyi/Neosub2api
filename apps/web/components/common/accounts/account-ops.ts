/**
 * 账号页的真实能力与聚合写操作。
 *
 * 为什么单独放在这里：`lib/api.ts` 里的 accountApi 是 CodeBuddy 单家的旧契约，
 * 不知道 provider 参数；而账号页现在要按**后端真实覆写的方法**决定画哪些按钮。
 * 直接复用导出的 axios 实例，鉴权、basePath、401 跳转都保持同一套。
 */
import {http} from '@/lib/api';
import type {CreditsMeta} from '@/lib/types';

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

/** unified = 本页管理；sidecar / quota / external = 账号在别处管理 */
export type AccountManaged = 'unified' | 'sidecar' | 'quota' | 'external';

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
};
