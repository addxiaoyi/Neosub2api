'use client';

import {useEffect, useState} from 'react';
import {providerApi} from '@/lib/api';
import type {ProviderInfo} from '@/lib/providers';

let cache: ProviderInfo[] | null = null;
let inflight: Promise<ProviderInfo[]> | null = null;

/** 多个页面共用一份 provider 清单，避免每次切页都重复请求。 */
export async function loadProviders(force = false): Promise<ProviderInfo[]> {
  if (!force && cache) return cache;
  if (!inflight) {
    inflight = providerApi
      .list()
      .then((items) => {
        cache = items;
        return items;
      })
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
}

export function useProviders() {
  const [providers, setProviders] = useState<ProviderInfo[]>(cache ?? []);
  const [loading, setLoading] = useState(!cache);

  useEffect(() => {
    let alive = true;
    loadProviders()
      .then((items) => {
        if (alive) setProviders(items);
      })
      .catch(() => {
        /* 拉不到就保持空清单，页面按无 provider 降级 */
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, []);

  return {providers, loading, reload: () => loadProviders(true)};
}
