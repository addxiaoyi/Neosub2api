'use client';

import {Fragment, useCallback, useEffect, useRef, useState} from 'react';
import {ScrollText, Search, Trash2, ChevronLeft, ChevronRight, Download, Loader2, AlertCircle, RotateCcw, Check} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {notify} from '@/lib/toast';
import {keyApi, logApi, providerApi, pricingApi, errText, downloadErrText, type LogQuery} from '@/lib/api';
import type {ApiKey, RequestLog, CreditStatus} from '@/lib/types';
import type {ProviderInfo} from '@/lib/providers';
import {fmtDateTime, fmtDateTimeMarked, fmtLatency, fmtNumber, fmtMicroUsd} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {useAuth} from '@/lib/auth-context';
import {Button} from '@/components/ui/button';
import {CopyButton} from '@/components/ui/copy-button';
import {useT} from '@/lib/i18n/provider';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Drawer,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

const PAGE_SIZE = 20;

function isRecord(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

/**
 * 配额/限流拒绝时后端把整个 denial 写进 error 列。详情抽屉按字段拆开展示，
 * 原始串收进可展开区域：既不让 used/limit 的 micro 整数直接糊在脸上，
 * 也保留原文供排障。
 */
function parseErrorJson(raw: string | null | undefined): Record<string, unknown> | null {
  if (!raw) return null;
  const body = raw.trim();
  if (!body.startsWith('{')) return null;
  try {
    const parsed: unknown = JSON.parse(body);
    return isRecord(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

/** 拒绝详情里优先展示的字段（其余按原顺序追加） */
const ERROR_FIELD_ORDER = [
  'type', 'message', 'window', 'metric', 'used', 'limit', 'unit',
  'reset_at', 'reset_after', 'window_sec', 'policy_id', 'key_id', 'layer',
] as const;

/** 时间类字段：后端下发 epoch 秒 */
const EPOCH_FIELDS = new Set(['reset_at', 'period_start', 'period_end']);

/**
 * denial 里 unit 的实际取值只有这两种（apps/control/app/billing.py:437 与 :503）：
 * micro_usd = micro-USD 金额，requests = 纯计数。没见过的值一律不套货币符号。
 */
const MONEY_UNITS = new Set(['micro_usd']);
const COUNT_UNITS = new Set(['requests']);

/** 金额按 micro-USD 换算；计数直接给数；其余单位把原文单位缀在数值后 */
function quotaAmount(value: number, unit: string): string {
  if (MONEY_UNITS.has(unit)) return fmtMicroUsd(value);
  if (!unit || COUNT_UNITS.has(unit)) return fmtNumber(value);
  return `${fmtNumber(value)} ${unit}`;
}

function errorFieldValue(key: string, value: unknown, unit: unknown): string {
  if (EPOCH_FIELDS.has(key) && typeof value === 'number') {
    return value > 0 ? fmtDateTime(value) : '—';
  }
  if (key === 'reset_after' && typeof value === 'number') return `${value}s`;
  if (typeof value === 'number') {
    if (key.endsWith('_micro')) return fmtMicroUsd(value);
    // 只有 used/limit 才受 unit 影响；其余数值（window_sec、policy_id…）按普通数字显示
    if (key === 'used' || key === 'limit') return quotaAmount(value, typeof unit === 'string' ? unit : '');
    return fmtNumber(value);
  }
  if (typeof value === 'string') return value;
  return JSON.stringify(value) ?? '—';
}

function errorRows(json: Record<string, unknown>): [string, string][] {
  const unit = json.unit;
  const rest = Object.keys(json).filter((k) => !(ERROR_FIELD_ORDER as readonly string[]).includes(k));
  return [...ERROR_FIELD_ORDER.filter((k) => k in json), ...rest]
    .map((k) => [k, errorFieldValue(k, json[k], unit)] as [string, string]);
}

/** 记账口径 → 徽标短文案（键五语同名） */
const CREDIT_LABEL: Record<CreditStatus, string> = {
  billed: 'logs.creditBilled',
  unpriced: 'logs.creditUnpriced',
  usage_missing: 'logs.creditUsageMissing',
  not_billed: 'logs.creditNotBilled',
  unlinked: 'logs.creditUnlinked',
};

/** 记账口径 → 展开后的解释句 */
const CREDIT_NOTE: Record<CreditStatus, string> = {
  billed: 'logs.creditBilledNote',
  unpriced: 'logs.creditUnpricedNote',
  usage_missing: 'logs.creditUsageMissingNote',
  not_billed: 'logs.creditNotBilledNote',
  unlinked: 'logs.creditUnlinkedNote',
};

/** 语气色只复用本页已有的三档：绿=扣过、琥珀=定价存疑、红=失败类，其余走 secondary 底色 */
const CREDIT_TONE: Record<CreditStatus, string> = {
  billed: 'text-emerald-700 dark:text-emerald-400',
  unpriced: 'text-warning-text',
  usage_missing: 'text-muted-foreground',
  not_billed: 'text-destructive-text',
  unlinked: 'text-muted-foreground',
};

export default function LogsPage() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [logs, setLogs] = useState<RequestLog[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  /** 列表拉取失败：与「真的没有日志」分开展示，并给重试入口 */
  const [loadFailed, setLoadFailed] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [detail, setDetail] = useState<RequestLog | null>(null);
  const detailTriggerRef = useRef<HTMLElement | null>(null);

  /** 展开口径说明的那一行；unpriced 还会现场问价格表缺了哪个类别 */
  const [openCredit, setOpenCredit] = useState<RequestLog | null>(null);
  const [priceLook, setPriceLook] = useState<{loading: boolean; missing: string[] | null; error: string | null}>({
    loading: false,
    missing: null,
    error: null,
  });

  function toggleCredit(row: RequestLog) {
    if (openCredit?.id === row.id) {
      setOpenCredit(null);
      return;
    }
    setOpenCredit(row);
    if (row.credit_status !== 'unpriced') {
      setPriceLook({loading: false, missing: null, error: null});
      return;
    }
    setPriceLook({loading: true, missing: null, error: null});
    pricingApi
      .resolve({provider: row.provider || '*', model: row.model || ''})
      .then((r) => setPriceLook({loading: false, missing: r.missing, error: null}))
      .catch((e: unknown) => setPriceLook({loading: false, missing: null, error: errText(e)}));
  }

  const [keyId, setKeyId] = useState('all');
  const [provider, setProvider] = useState('all');
  const [model, setModel] = useState('');
  const [status, setStatus] = useState('all');
  const [days, setDays] = useState('7');
  const [triageBusyId, setTriageBusyId] = useState<number | null>(null);

  const filterParams = useCallback(
    (): LogQuery => ({
      key_id: keyId === 'all' ? undefined : keyId,
      provider: provider === 'all' ? undefined : provider,
      model: model || undefined,
      status: status === 'all' ? undefined : status,
      days: Number(days) || undefined,
    }),
    [keyId, provider, model, status, days],
  );

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await logApi.list({page, size: PAGE_SIZE, ...filterParams()});
      setLogs(res.items);
      setTotal(res.total);
      setLoadFailed(false);
    } catch {
      // 错误由页内错误块承载（走 i18n），不再弹英文 "Network Error" 的瞬时 toast
      setLoadFailed(true);
    } finally {
      setLoading(false);
    }
  }, [page, filterParams]);

  useEffect(() => {
    load();
    // 只让「翻页 / 时间范围」自动重查；其余筛选由「筛选」按钮显式触发，
    // 避免边打字边发请求。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, days]);

  // 新请求会不断写入日志；心跳刷新只更新当前筛选下的列表，不会重置筛选条件
  useHeartbeat(load, 60000);

  async function toggleResolved(row: RequestLog) {
    setTriageBusyId(row.id);
    try {
      if (row.resolved) await logApi.unresolve(row.id);
      else await logApi.resolve(row.id);
      notify.ok(t(row.resolved ? 'logs.unresolved' : 'logs.resolved'));
      load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setTriageBusyId(null);
    }
  }

  useEffect(() => {
    keyApi.list().then(setKeys).catch(() => undefined);
    providerApi.list().then(setProviders).catch(() => undefined);
  }, []);

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  // 拒绝详情按字段拆开渲染；原始串仍保留在可展开区域里
  const errorJson = detail ? parseErrorJson(detail.error) : null;
  const errorRowsList = errorJson ? errorRows(errorJson) : [];
  const quotaReset = errorJson
    ? typeof errorJson.reset_at === 'number'
      ? errorJson.reset_at
      : typeof errorJson.reset_after === 'number'
        ? Date.now() / 1000 + errorJson.reset_after
        : null
    : null;
  const quotaSummary =
    errorJson?.type === 'quota_exceeded'
      ? t('logs.quotaExceeded', {
          used: errorFieldValue('used', errorJson.used, errorJson.unit),
          limit: errorFieldValue('limit', errorJson.limit, errorJson.unit),
          reset: quotaReset ? fmtDateTime(quotaReset) : '—',
        })
      : null;

  function applyFilters() {
    setPage(1);
    load();
  }

  /** 导出走当前筛选条件；失败体是 Blob，必须解码后才能给出可见原因 */
  async function exportCsv() {
    setExporting(true);
    try {
      const {blob, filename} = await logApi.exportCsv(filterParams());
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      notify.err(await downloadErrText(e));
    } finally {
      setExporting(false);
    }
  }

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title={t('logs.title')}
        description={t('logs.description')}
        actions={
          <>
            {isAdmin && (
              <Button
                variant="outline"
                size="sm"
                className="rounded-full"
                disabled={exporting}
                onClick={exportCsv}
              >
                {exporting ? <Loader2 className="animate-spin" /> : <Download />}
                {t(exporting ? 'logs.exporting' : 'logs.export')}
              </Button>
            )}
            {isAdmin && (
              <ConfirmDialog
                title={t('logs.clearTitle')}
                description={t('logs.clearDesc')}
                confirmText={t('common.clear')}
                destructive
                onConfirm={async () => {
                  await logApi.clear();
                  notify.ok(t('logs.cleared'));
                  setPage(1);
                  load();
                }}
                trigger={
                  <Button variant="outline" size="sm" className="rounded-full border-destructive/30 text-destructive-text hover:border-destructive/50 hover:text-destructive-text">
                    <Trash2 />
                    {t('common.clear')}
                  </Button>
                }
              />
            )}
          </>
        }
      />

      {/* 拉取失败单独成块：不能退化成「暂无日志」，那会让人以为日志被清空了 */}
      {loadFailed && !loading && (
        <section className="flex flex-wrap items-start justify-between gap-3 rounded-[20px] border border-destructive/30 bg-destructive/5 px-4 py-3.5">
          <div className="flex min-w-0 items-start gap-2.5">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive-text" />
            <div className="min-w-0">
              <div className="text-sm font-medium text-destructive-text">{t('logs.loadFailedTitle')}</div>
              <div className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                {t('logs.loadFailedDesc')}
              </div>
            </div>
          </div>
          <Button size="sm" variant="outline" className="shrink-0 rounded-full" onClick={load} disabled={loading}>
            <RotateCcw className="mr-1.5 h-3 w-3" />
            {t('common.retry')}
          </Button>
        </section>
      )}

      <section className="rounded-[20px] bg-muted p-4">
        <div className="grid grid-cols-2 items-end gap-3 md:grid-cols-6">
          <div className="space-y-1.5">
            <Label htmlFor="logs-days" className="text-[11px] text-muted-foreground">{t('logs.filterRange')}</Label>
            <Select value={days} onValueChange={(v) => { setDays(v); setPage(1); }}>
              <SelectTrigger id="logs-days" className="bg-background"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="1">{t('logs.last24h')}</SelectItem>
                <SelectItem value="7">{t('stats.last7')}</SelectItem>
                <SelectItem value="30">{t('stats.last30')}</SelectItem>
                <SelectItem value="90">{t('stats.last90')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="logs-key" className="text-[11px] text-muted-foreground">{t('logs.filterKey')}</Label>
            <Select value={keyId} onValueChange={(v) => { setKeyId(v); setPage(1); }}>
              <SelectTrigger id="logs-key" className="bg-background"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t('logs.allKeys')}</SelectItem>
                {keys.map((k) => (
                  <SelectItem key={k.id} value={String(k.id)}>{k.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="logs-provider" className="text-[11px] text-muted-foreground">{t('logs.filterProvider')}</Label>
            <Select value={provider} onValueChange={(v) => { setProvider(v); setPage(1); }}>
              <SelectTrigger id="logs-provider" className="bg-background"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t('common.all')}</SelectItem>
                {providers.map((p) => (
                  <SelectItem key={p.id} value={p.id}>{p.label || p.id}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="logs-status" className="text-[11px] text-muted-foreground">{t('accounts.colStatus')}</Label>
            <Select value={status} onValueChange={(v) => { setStatus(v); setPage(1); }}>
              <SelectTrigger id="logs-status" className="bg-background"><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t('common.all')}</SelectItem>
                <SelectItem value="ok">{t('common.success')}</SelectItem>
                <SelectItem value="error">{t('common.failure')}</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="logs-model" className="text-[11px] text-muted-foreground">{t('nav.models')}</Label>
            <Input id="logs-model" value={model} onChange={(e) => setModel(e.target.value)} placeholder={t('common.all')} className="bg-background" />
          </div>
          <Button className="rounded-full" onClick={applyFilters}>
            <Search />
            {t('logs.filter')}
          </Button>
        </div>
      </section>

      <section className="overflow-hidden rounded-[20px] bg-muted">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead scope="col" className="min-w-[160px] pl-4 text-xs text-muted-foreground">{t('logs.colTime')}</TableHead>
              <TableHead scope="col" className="hidden text-[11px] text-muted-foreground md:table-cell">{t('nav.keys')}</TableHead>
              <TableHead scope="col" className="hidden text-[11px] text-muted-foreground md:table-cell">{t('logs.colProvider')}</TableHead>
              <TableHead scope="col" className="text-[11px] text-muted-foreground">{t('nav.models')}</TableHead>
              <TableHead scope="col" className="text-[11px] text-muted-foreground">{t('accounts.colStatus')}</TableHead>
              <TableHead scope="col" className="hidden text-[11px] text-muted-foreground md:table-cell">{t('logs.colCredit')}</TableHead>
              <TableHead scope="col" className="hidden text-[11px] text-muted-foreground md:table-cell">{t('logs.colLatency')}</TableHead>
              <TableHead scope="col" className="hidden pr-4 text-[11px] text-muted-foreground md:table-cell">{t('logs.colTokens')}</TableHead>
              {isAdmin && (
                <TableHead scope="col" className="pr-4 text-[11px] text-muted-foreground">
                  <span className="sr-only">{t('logs.colTriage')}</span>
                </TableHead>
              )}
            </TableRow>
          </TableHeader>
          <TableBody>
            {logs.map((l) => (
              <Fragment key={l.id}>
              <TableRow
                className="cursor-pointer border-b border-border/40"
                onClick={() => setDetail(l)}
              >
                <TableCell className="min-w-[160px] pl-4 text-xs">
                  {/* 行点击只服务鼠标；键盘用户需要一个真正可聚焦、可用 Enter/Space 触发的控件 */}
                  <button
                    type="button"
                    aria-label={t('logs.detailTitle', {id: l.id})}
                    className="rounded-sm text-left text-muted-foreground hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    onClick={(event) => {
                      event.stopPropagation();
                      detailTriggerRef.current = event.currentTarget;
                      setDetail(l);
                    }}
                  >
                    {fmtDateTimeMarked(l.ts)}
                  </button>
                </TableCell>
                <TableCell className="hidden text-xs md:table-cell">{l.key_name || '—'}</TableCell>
                <TableCell className="hidden text-xs md:table-cell">
                  <Badge variant="secondary" className="rounded-full">{l.provider || '—'}</Badge>
                </TableCell>
                <TableCell className="text-xs">{l.model || '—'}</TableCell>
                <TableCell>
                  {l.status >= 200 && l.status < 300 ? (
                    <Badge variant="secondary" className="rounded-full text-emerald-700 dark:text-emerald-400">{l.status}</Badge>
                  ) : l.status >= 400 && l.status < 500 ? (
                    <Badge variant="secondary" className="rounded-full bg-amber-500/12 text-warning-text">
                      {l.status || 'ERR'}
                    </Badge>
                  ) : (
                    <Badge variant="destructive" className="rounded-full">{l.status || 'ERR'}</Badge>
                  )}
                  {l.resolved && (
                    <Badge variant="outline" className="ml-1.5 rounded-full text-[10px]">
                      {t('logs.resolvedBadge')}
                    </Badge>
                  )}
                  {/* 窄屏没有金额列，金额与口径压成一行，null 只显示「—」 */}
                  <div className="mt-1 flex items-center gap-1.5 text-[10px] leading-none md:hidden">
                    <span className="tabular-nums text-muted-foreground">{fmtMicroUsd(l.credit)}</span>
                    <button
                      type="button"
                      aria-expanded={openCredit?.id === l.id}
                      aria-label={t('logs.creditNoteLabel')}
                      className={`rounded-sm hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${CREDIT_TONE[l.credit_status]}`}
                      onClick={(event) => {
                        event.stopPropagation();
                        toggleCredit(l);
                      }}
                    >
                      {t(CREDIT_LABEL[l.credit_status])}
                    </button>
                  </div>
                </TableCell>
                <TableCell className="hidden text-xs md:table-cell">
                  <div className="flex flex-col items-start gap-1">
                    <span className={l.credit === null ? 'tabular-nums text-muted-foreground' : 'tabular-nums'}>
                      {fmtMicroUsd(l.credit)}
                    </span>
                    <button
                      type="button"
                      aria-expanded={openCredit?.id === l.id}
                      aria-label={t('logs.creditNoteLabel')}
                      title={t(CREDIT_NOTE[l.credit_status])}
                      className="rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                      onClick={(event) => {
                        event.stopPropagation();
                        toggleCredit(l);
                      }}
                    >
                      <Badge variant="secondary" className={`rounded-full ${CREDIT_TONE[l.credit_status]}`}>
                        {t(CREDIT_LABEL[l.credit_status])}
                      </Badge>
                    </button>
                  </div>
                </TableCell>
                <TableCell className="hidden text-xs tabular-nums text-muted-foreground md:table-cell">
                  {fmtLatency(l.latency_ms)}
                </TableCell>
                <TableCell className="hidden pr-4 text-xs tabular-nums md:table-cell">
                  {l.prompt_tokens + l.completion_tokens > 0 ? (
                    fmtNumber(l.prompt_tokens + l.completion_tokens)
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                  {l.stream && <span className="ml-1 text-xs text-muted-foreground">{t('logs.streamShort')}</span>}
                </TableCell>
                {isAdmin && (
                  <TableCell className="pr-4">
                    {l.status >= 400 || l.error ? (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 rounded-full px-2 text-[11px]"
                        disabled={triageBusyId === l.id}
                        aria-label={t(l.resolved ? 'logs.unresolveAction' : 'logs.resolveAction')}
                        onClick={(event) => {
                          event.stopPropagation();
                          toggleResolved(l);
                        }}
                      >
                        {l.resolved ? <RotateCcw className="h-3.5 w-3.5" /> : <Check className="h-3.5 w-3.5" />}
                        {t(l.resolved ? 'logs.unresolveAction' : 'logs.resolveAction')}
                      </Button>
                    ) : null}
                  </TableCell>
                )}
              </TableRow>
              {openCredit?.id === l.id && (
                <TableRow className="border-b border-border/40 hover:bg-transparent">
                  <TableCell colSpan={9} className="whitespace-normal bg-background/60 px-4 py-2 text-[11px] leading-relaxed text-muted-foreground">
                    <span className="text-foreground/80">{t(CREDIT_NOTE[l.credit_status])}</span>
                    {l.credit_status === 'unpriced' && (
                      <span className="ml-2">
                        {priceLook.loading && t('common.loading')}
                        {priceLook.error && priceLook.error}
                        {!priceLook.loading && !priceLook.error && priceLook.missing && (
                          priceLook.missing.length
                            ? t('logs.creditUnpricedMissing', {
                                model: l.model || '—',
                                classes: priceLook.missing.join(t('common.listSeparator')),
                              })
                            : t('logs.creditUnpricedPricedNow', {model: l.model || '—'})
                        )}
                      </span>
                    )}
                  </TableCell>
                </TableRow>
              )}
              </Fragment>
            ))}
          </TableBody>
        </Table>

        {!logs.length && !loading && !loadFailed && (
          <EmptyState
            icon={ScrollText}
            title={t('logs.emptyTitle')}
            description={t('logs.emptyDesc')}
            className="flex flex-col items-center justify-center py-16 text-center"
          />
        )}

        <div className="flex items-center justify-between px-4 py-3">
          <div className="text-[11px] text-muted-foreground">
            {t('logs.pageInfo', {total: fmtNumber(total), page, pages})}
          </div>
          <div className="flex gap-1">
            <Button
              variant="outline"
              size="icon"
              aria-label={t('common.prevPage')}
              className="h-7 w-7 rounded-md"
              disabled={page <= 1}
              onClick={() => setPage((p) => Math.max(1, p - 1))}
            >
              <ChevronLeft className="h-3.5 w-3.5" />
            </Button>
            <Button
              variant="outline"
              size="icon"
              aria-label={t('common.nextPage')}
              className="h-7 w-7 rounded-md"
              disabled={page >= pages}
              onClick={() => setPage((p) => Math.min(pages, p + 1))}
            >
              <ChevronRight className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      </section>

      <Drawer autoFocus open={!!detail} onOpenChange={(v) => !v && setDetail(null)}>
        <DrawerContent
          onCloseAutoFocus={(event) => {
            // Drawer 用的是受控 open，没有 DialogTrigger；Radix 默认把焦点丢给 body
            event.preventDefault();
            if (detailTriggerRef.current?.isConnected) detailTriggerRef.current.focus();
          }}
        >
          <DrawerHeader>
            <DrawerTitle>{t('logs.detailTitle', {id: detail?.id ?? ''})}</DrawerTitle>
            <DrawerDescription>{detail ? fmtDateTime(detail.ts) : ''}</DrawerDescription>
          </DrawerHeader>
          {detail && (
            <div className="space-y-4 px-4 pb-8 text-xs">
              <Table className="text-xs">
                <TableBody>
                  {([
                    ['provider', t('logs.colProvider'), detail.provider || '—'],
                    ['key', t('logs.rowKey'), detail.key_name || '—'],
                    ['model', t('logs.rowModel'), detail.model || '—'],
                    ['status', t('logs.rowStatus'), String(detail.status)],
                    ['latency', t('logs.rowLatency'), fmtLatency(detail.latency_ms)],
                    ['promptTokens', t('logs.rowPromptTokens'), fmtNumber(detail.prompt_tokens)],
                    ['completionTokens', t('logs.rowCompletionTokens'), fmtNumber(detail.completion_tokens)],
                    ['stream', t('logs.rowStream'), detail.stream ? t('common.yes') : t('common.no')],
                    // credit 是后端实采值：null 显示「—」，不写成 US$0.00（那会被读成"记了 0 元"）
                    ['credit', t('logs.rowCredit'), fmtMicroUsd(detail.credit)],
                    ['creditStatus', t('logs.rowCreditStatus'), t(CREDIT_LABEL[detail.credit_status])],
                    ['creditNote', t('logs.rowCreditNote'), t(CREDIT_NOTE[detail.credit_status])],
                    // 以下字段后端 request_logs 表没有对应列，一律标注「未采集」，
                    // 不显示成 0 或空白（那会被误读成"这次请求没产生"）。
                    ['ip', t('logs.rowIp'), t('logs.notCollected')],
                    ['ua', t('logs.rowUserAgent'), t('logs.notCollected')],
                    ['firstToken', t('logs.rowFirstToken'), t('logs.notCollected')],
                    ['cacheHit', t('logs.rowCacheHit'), t('logs.notCollected')],
                    ['cacheMiss', t('logs.rowCacheMiss'), t('logs.notCollected')],
                    ['cacheWrite', t('logs.rowCacheWrite'), t('logs.notCollected')],
                    ['account', t('logs.rowAccount'), t('logs.notCollected')],
                    ['mapped', t('logs.rowMappedModel'), t('logs.notCollected')],
                  ] as [string, string, string][]).map(([id, k, v]) => (
                    <TableRow key={id} className="border-border/40 hover:bg-transparent">
                      <TableCell className="w-32 text-muted-foreground">{k}</TableCell>
                      <TableCell className="whitespace-normal break-all font-mono">{v}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>

              <div className="space-y-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="text-muted-foreground">{t('logs.rowError')}</span>
                  {detail.error && (
                    <CopyButton value={detail.error} title={t('logs.copyField', {field: t('logs.rowError')})} />
                  )}
                </div>
                {errorJson ? (
                  <>
                    {quotaSummary && <p className="whitespace-normal break-words">{quotaSummary}</p>}
                    <Table className="text-xs">
                      <TableBody>
                        {errorRowsList.map(([k, v]) => (
                          <TableRow key={k} className="border-border/40 hover:bg-transparent">
                            <TableCell className="w-32 font-mono text-muted-foreground">{k}</TableCell>
                            <TableCell className="whitespace-normal break-all">{v}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                    <details className="rounded-xl border border-border/60 bg-background/60 px-3 py-2">
                      <summary className="cursor-pointer text-muted-foreground">{t('logs.rawJson')}</summary>
                      <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all font-mono text-[11px]">
                        {JSON.stringify(errorJson, null, 2)}
                      </pre>
                    </details>
                  </>
                ) : (
                  <div className="whitespace-normal break-all font-mono">{detail.error || '—'}</div>
                )}
              </div>
            </div>
          )}
        </DrawerContent>
      </Drawer>
    </div>
  );
}
