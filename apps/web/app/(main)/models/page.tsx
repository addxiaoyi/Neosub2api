'use client';

import {useCallback, useEffect, useMemo, useState} from 'react';
import {Boxes, ChevronDown, ChevronRight, Layers, Loader2, Maximize2, RefreshCw, Search} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {upstreamApi, errText} from '@/lib/api';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {cn} from '@/lib/utils';
import type {ModelEntrySource, ModelInfo, ModelListResponse, ModelProviderState} from '@/lib/types';

/** 单条模型在某一家的真实来源 → 界面译文 */
const ENTRY_SOURCE_KEYS: Record<ModelEntrySource, string> = {
  upstream: 'models.entrySourceUpstream',
  declared: 'models.entrySourceDeclared',
  declared_fallback: 'models.entrySourceFallback',
};

/** 主归属 provider；老数据只有 owned_by 时回落到它 */
function primaryOf(m: ModelInfo): string | undefined {
  return m.provider ?? m.owned_by;
}

/** 全部归属 provider，保证主归属排在第一位 */
function providersOf(m: ModelInfo): string[] {
  const primary = primaryOf(m);
  const all = m.providers?.length ? [...m.providers] : primary ? [primary] : [];
  if (primary && !all.includes(primary)) all.unshift(primary);
  return all;
}

/** 主归属那家的就绪状态 */
function stateOf(m: ModelInfo): ModelProviderState | undefined {
  const pid = primaryOf(m);
  return pid ? m.provider_states?.[pid] : undefined;
}

/** 上下文窗口显示：131072 → 128K；1048576 → 1M；0 → — */
function fmtCtx(n?: number | null): string {
  if (!n || n <= 0) return '—';
  if (n >= 1024 * 1024) {
    const m = n / (1024 * 1024);
    return `${Number.isInteger(m) ? m : m.toFixed(1)}M`;
  }
  if (n >= 1024) return `${Math.round(n / 1024)}K`;
  return String(n);
}

/** 后端 /api/models 的 source 枚举 → 界面译文 */
const SOURCE_KEYS: Record<string, string> = {
  dynamic: 'models.sourceLive',
  static: 'models.sourceUpstream',
  unknown: 'models.sourceNone',
};

function sourceLabel(source: string, t: (key: string) => string): string {
  const key = SOURCE_KEYS[source];
  return key ? t(key) : t('models.sourceNone');
}

/** 一张统计卡 */
function StatCard({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: typeof Boxes;
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="rounded-[20px] bg-muted px-3.5 py-3">
      <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <Icon className="h-3.5 w-3.5" />
        {label}
      </div>
      <div className="mt-1.5 text-xl font-semibold tabular-nums">{value}</div>
      {hint && <div className="mt-0.5 text-[10px] text-muted-foreground">{hint}</div>}
    </div>
  );
}

export default function ModelsPage() {
  const t = useT();
  const [data, setData] = useState<ModelListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');

  const [q, setQ] = useState('');
  const [provider, setProvider] = useState('all');
  /** 多家共享的模型：展开后列出每一家的来源与就绪状态 */
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const toggleExpanded = useCallback((id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const load = useCallback(async () => {
    setRefreshing(true);
    setLoading(true);
    try {
      const res = await upstreamApi.models();
      setData(res);
      setError('');
    } catch (e) {
      setError(errText(e));
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const models = useMemo(() => data?.models ?? [], [data]);

  const providers = useMemo(() => {
    const ids = models.map(primaryOf).filter((v): v is string => !!v);
    return Array.from(new Set(ids)).sort();
  }, [models]);

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return models.filter((m) => {
      const primary = primaryOf(m);
      if (provider !== 'all' && !providersOf(m).includes(provider)) return false;
      if (!kw) return true;
      return (
        m.id.toLowerCase().includes(kw) ||
        (primary || '').toLowerCase().includes(kw) ||
        providersOf(m).some((pid) => pid.toLowerCase().includes(kw))
      );
    });
  }, [models, q, provider]);

  /** 最大上下文：只统计上游真的给出上下文字段的条目，避免把 0 当成结论 */
  const maxContext = useMemo(
    () =>
      models.reduce((max, m) => Math.max(max, m.context_length ?? m.context_window ?? 0), 0),
    [models],
  );

  const source = data?.source ?? 'unknown';

  /** 归属：主归属 +「+N 家」，展开后逐家列出真实来源与就绪状态 */
  function renderProvider(m: ModelInfo) {
    const primary = primaryOf(m);
    if (!primary) return <span className="text-xs text-muted-foreground">—</span>;
    const all = providersOf(m);
    const shared = all.length - 1;
    const open = expanded.has(m.id);
    // 表格单元格默认 whitespace-nowrap，展开区必须显式换行，否则长原因会把表格撑宽
    const panelId = `provider-detail-${m.id.replace(/[^\w.-]/g, '_')}`;
    return (
      <div className="min-w-0 max-w-[20rem] whitespace-normal">
        <div className="flex flex-wrap items-center gap-1.5">
          <span className="min-w-0 truncate font-mono text-xs">{primary}</span>
          {shared > 0 && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-5 rounded-md px-1.5 text-[10px]"
              aria-expanded={open}
              aria-controls={panelId}
              aria-label={t(open ? 'models.collapseProviders' : 'models.expandProviders', {n: shared})}
              onClick={() => toggleExpanded(m.id)}
            >
              {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
              {t('models.sharedMore', {n: shared})}
            </Button>
          )}
        </div>
        {open && shared > 0 && (
          <div
            id={panelId}
            role="region"
            aria-label={t('models.expandedRegion')}
            className="scroll-slim mt-1.5 max-h-40 min-w-0 space-y-1 overflow-y-auto border-l border-border/60 pl-2"
          >
            {all.map((pid) => {
              const st = m.provider_states?.[pid];
              const ready = st?.available ?? false;
              return (
                <div key={pid} className="min-w-0 text-[11px] leading-4">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <span
                      aria-hidden
                      className={cn(
                        'h-1.5 w-1.5 shrink-0 rounded-full',
                        ready ? 'bg-emerald-500' : 'bg-amber-500',
                      )}
                    />
                    <span className="min-w-0 truncate font-mono text-foreground/80" title={pid}>
                      {pid}
                    </span>
                    {st && (
                      <>
                        <span
                          className={cn(
                            'shrink-0 text-[10px] font-medium',
                            ready
                              ? 'text-emerald-600 dark:text-emerald-400'
                              : 'text-warning-text',
                          )}
                        >
                          {ready ? t('models.ready') : t('models.needsConfig')}
                        </span>
                        <span className="shrink-0 text-[10px] text-muted-foreground">
                          · {t(ENTRY_SOURCE_KEYS[st.source] ?? 'models.entrySourceDeclared')}
                        </span>
                      </>
                    )}
                  </div>
                  {st && !st.available && st.unavailable_reason && (
                    <div className="mt-0.5 whitespace-normal break-words pl-3 text-[10px] text-muted-foreground">
                      {t('models.unavailableReason', {reason: st.unavailable_reason})}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  }

  /** 主归属那家是否 ready；不 ready 时给出 requires / unavailable_reason */
  function renderReadiness(m: ModelInfo) {
    const st = stateOf(m);
    const readiness = st?.readiness ?? m.readiness;
    if (!readiness) return <span className="text-xs text-muted-foreground">—</span>;
    const available = st?.available ?? m.available;
    const enabled = st?.enabled ?? m.enabled;
    const ready = available ?? (readiness === 'ready' && enabled !== false);
    const label = ready
      ? t('models.ready')
      : readiness === 'ready' && enabled === false
        ? t('models.notEnabled')
        : t('models.needsConfig');
    const requires = st?.requires?.length ? st.requires : (m.requires ?? []);
    const reason = ready
      ? ''
      : st?.unavailable_reason ??
        m.unavailable_reason ??
        (requires.length ? requires.join(t('common.listSeparator')) : '');
    return (
      <div className="max-w-[15rem]">
        <span
          className={cn(
            'text-[11px] font-medium',
            ready ? 'text-emerald-600 dark:text-emerald-400' : 'text-warning-text',
          )}
        >
          {label}
        </span>
        {reason && (
          <div className="mt-0.5 line-clamp-2 text-[10px] leading-4 text-muted-foreground" title={reason}>
            {reason}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title={t('models.title')}
        description={t('models.descriptionApi')}
        actions={
          <Button
            variant="outline"
            size="sm"
            className="rounded-full"
            disabled={refreshing}
            onClick={() => {
              load();
              notify.info(t('models.refetching'));
            }}
          >
            {refreshing ? <Loader2 className="animate-spin" /> : <RefreshCw />}
            {t('models.refetch')}
          </Button>
        }
      />

      {/* 来源说明：如实标注，不把上游未知来源说成实时数据 */}
      {data && !loading && (
        <div
          className={cn(
            'flex flex-wrap items-center gap-x-2 gap-y-1 rounded-[16px] border px-3.5 py-2.5 text-[11px]',
            source === 'dynamic'
              ? 'border-border bg-muted/60 text-muted-foreground'
              : 'border-amber-500/30 bg-amber-500/10 text-warning-text',
          )}
        >
          <span className="font-medium">
            {t('models.source', {label: sourceLabel(source, t)})}
          </span>
          {source !== 'dynamic' && (
            <span className="basis-full text-[10px] leading-4 opacity-90">
              {source === 'static' ? t('models.fallbackNote') : t('models.emptyNote')}
            </span>
          )}
        </div>
      )}

      {/* 统计卡：只统计后端确实返回的字段 */}
      {data && !loading && (
        <div className="grid grid-cols-2 gap-3">
          <StatCard icon={Boxes} label={t('models.available')} value={String(data.count)} />
          <StatCard icon={Maximize2} label={t('models.maxContext')} value={fmtCtx(maxContext)} />
        </div>
      )}

      {/* 搜索与筛选 */}
      <section className="rounded-[20px] bg-muted p-3.5">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
            <Input
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t('models.searchPlaceholderApi')}
              className="h-9 bg-background pl-8"
            />
          </div>
          <div className="-mx-0.5 flex flex-wrap items-center gap-1.5 overflow-x-auto px-0.5 pb-0.5">
            <span className="flex shrink-0 items-center gap-1 text-[11px] text-muted-foreground">
              <Layers className="h-3.5 w-3.5" />
              {t('playground.providerLabel')}
            </span>
            <Button
              variant={provider === 'all' ? 'default' : 'outline'}
              size="sm"
              className="h-7 shrink-0 rounded-full px-2.5 text-[11px]"
              onClick={() => setProvider('all')}
            >
              {t('common.all')}
            </Button>
            {providers.map((p) => (
              <Button
                key={p}
                variant={provider === p ? 'default' : 'outline'}
                size="sm"
                className="h-7 shrink-0 rounded-full px-2.5 text-[11px]"
                onClick={() => setProvider(p)}
              >
                {p}
              </Button>
            ))}
          </div>
        </div>
      </section>

      {/* 列表 */}
      <section className="overflow-hidden rounded-[20px] bg-muted">
        {loading ? (
          <div className="flex items-center justify-center gap-2 py-16 text-xs text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            {t('models.loadingList')}
          </div>
        ) : error ? (
          <EmptyState icon={Boxes} title={t('models.loadFailed')} description={error} />
        ) : models.length === 0 ? (
          <EmptyState
            icon={Boxes}
            title={t('models.noModels')}
            description={t('models.noModelsDesc')}
          />
        ) : filtered.length === 0 ? (
          <EmptyState icon={Search} title={t('models.noMatch')} description={t('models.noMatchDesc')} />
        ) : (
          <>
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow className="border-b border-border/60 hover:bg-transparent">
                    <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('models.colModel')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('models.colProvider')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('models.colReadiness')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('models.colContext')}</TableHead>
                    <TableHead className="pr-4 text-[11px] text-muted-foreground">{t('models.colMaxOutput')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {filtered.map((m: ModelInfo) => (
                    <TableRow key={m.id} className="border-b border-border/40">
                      <TableCell className="pl-4">
                        <span className="font-mono text-xs font-medium">{m.id}</span>
                      </TableCell>
                      <TableCell>{renderProvider(m)}</TableCell>
                      <TableCell>{renderReadiness(m)}</TableCell>
                      <TableCell className="text-xs tabular-nums text-muted-foreground">
                        {fmtCtx(m.context_length ?? m.context_window)}
                      </TableCell>
                      <TableCell className="pr-4 text-xs tabular-nums text-muted-foreground">
                        {fmtCtx(m.max_output_tokens)}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
            {(filtered.length !== models.length || q.trim() || provider !== 'all') && (
              <div className="border-t border-border/40 px-4 py-2 text-[11px] text-muted-foreground">
                {t('models.filteredCount', {n: filtered.length, total: models.length})}
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
