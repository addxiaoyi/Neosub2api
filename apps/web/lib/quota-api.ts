/**
 * 配额策略与告警（/api/quota/*）。
 *
 * 单独成模块而不是塞进 lib/api.ts：这块是近期新增的额度能力，字段与
 * 其它域无交集，独立文件便于并行改动时避免和别人抢同一份导出清单。
 */
import {http} from './api';

export type QuotaWindow = 'day' | 'month';

export interface QuotaPolicy {
  id: number;
  key_id: number;
  key_name: string;
  provider: string;
  model: string;
  window: QuotaWindow;
  limit_micro: number;
  limit_usd: number;
  limit_requests: number;
  enabled: boolean;
  thresholds: number[];
  created_at: number;
  updated_at: number;
}

export interface QuotaAlert {
  id: number;
  policy_id: number;
  kind: 'quota' | 'provider_failure' | 'usage_missing_ratio' | string;
  threshold_percent: number;
  window: QuotaWindow;
  period_start: number;
  triggered_at: number;
  metric: 'micro' | 'requests' | string;
  used_micro: number;
  used_usd: number;
  limit_micro: number;
  used_requests: number;
  limit_requests: number;
  /** 读取端状态：closed 优先，静默期内为 silenced，到期自动回 open */
  status: 'open' | 'acked' | 'closed' | 'silenced';
  acked_at: number;
  closed_at: number;
  silenced_until: number;
  key_id: number | null;
  provider: string;
  model: string;
}

export interface QuotaStatusItem {
  policy: QuotaPolicy;
  period_start: number;
  period_end: number;
  reset_at: number;
  reset_after: number;
  used_micro: number;
  used_usd: number;
  used_requests: number;
  remaining_micro: number | null;
  remaining_requests: number | null;
  ratio_micro: number | null;
  ratio_requests: number | null;
  exceeded: boolean;
  alerts: QuotaAlert[];
}

export interface QuotaStatus {
  at: number;
  items: QuotaStatusItem[];
}

export interface QuotaAlertPage {
  total: number;
  items: QuotaAlert[];
}

export interface QuotaPolicyInput {
  key_id: number;
  window: QuotaWindow;
  provider: string;
  model: string;
  limit_micro: number;
  limit_requests: number;
  thresholds: number[];
  enabled: boolean;
}

const get = async <T>(url: string, params?: Record<string, unknown>): Promise<T> =>
  (await http.get<T>(url, {params})).data;

export const quotaApi = {
  policies: (keyId?: number) =>
    get<QuotaPolicy[]>('/api/quota/policies', {key_id: keyId}),

  /** 幂等：同一 (key, provider, model, window) 会更新原策略，阈值整体替换 */
  savePolicy: async (body: QuotaPolicyInput): Promise<QuotaPolicy> =>
    (await http.post<QuotaPolicy>('/api/quota/policies', body)).data,

  /** 删除策略与阈值；告警历史保留 */
  removePolicy: async (id: number): Promise<{ok: boolean; deleted: number}> =>
    (await http.delete<{ok: boolean; deleted: number}>(`/api/quota/policies/${id}`)).data,

  status: (keyId?: number, at?: number) =>
    get<QuotaStatus>('/api/quota/status', {key_id: keyId, at}),

  alerts: (params: {key_id?: number; policy_id?: number; limit?: number}) =>
    get<QuotaAlertPage>('/api/quota/alerts', params),

  /** 以下四个动作 admin-only；ack/close 幂等，静默到期由读取端自动回 open */
  ackAlert: async (id: number): Promise<{ok: boolean; item: QuotaAlert}> =>
    (await http.post<{ok: boolean; item: QuotaAlert}>(`/api/quota/alerts/${id}/ack`)).data,
  silenceAlert: async (id: number, seconds: number): Promise<{ok: boolean; item: QuotaAlert}> =>
    (await http.post<{ok: boolean; item: QuotaAlert}>(`/api/quota/alerts/${id}/silence`, {seconds}))
      .data,
  closeAlert: async (id: number): Promise<{ok: boolean; item: QuotaAlert}> =>
    (await http.post<{ok: boolean; item: QuotaAlert}>(`/api/quota/alerts/${id}/close`)).data,
  reopenAlert: async (id: number): Promise<{ok: boolean; item: QuotaAlert}> =>
    (await http.post<{ok: boolean; item: QuotaAlert}>(`/api/quota/alerts/${id}/reopen`)).data,
};
