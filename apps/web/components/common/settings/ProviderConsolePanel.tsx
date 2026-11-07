'use client';

import {useCallback, useEffect, useMemo, useState} from 'react';
import {
  Activity,
  AlertTriangle,
  BadgeCheck,
  ClipboardList,
  Database,
  KeyRound,
  Loader2,
  Play,
  RefreshCw,
  ShieldCheck,
  TerminalSquare,
  TimerReset,
  Zap,
} from 'lucide-react';
import {
  errText,
  providerConsoleApi,
} from '@/lib/api';
import type {
  CodebuddyTaskPage,
  ConsoleMediaInventory,
  ConsoleRotationEntry,
  ProviderConsole,
} from '@/lib/provider-console';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';

type Tab = 'overview' | 'connections' | 'runtime' | 'tasks' | 'usage';
type Window = '24h' | '7d' | '30d';

const WINDOW_LABEL: Record<Window, string> = {
  '24h': 'providerConsole.window24h',
  '7d': 'providerConsole.window7d',
  '30d': 'providerConsole.window30d',
};

function stamp(ts: number | null | undefined): string {
  return ts ? new Date(ts * 1000).toLocaleString() : '';
}

function Stat({label, value, tone}: {label: string; value: string; tone?: 'bad' | 'good'}) {
  return (
    <div className="rounded-xl bg-muted/60 px-2.5 py-2">
      <div className="text-[10px] text-muted-foreground">{label}</div>
      <div
        className={`mt-0.5 text-[13px] font-semibold tabular-nums ${
          tone === 'bad' ? 'text-destructive' : tone === 'good' ? 'text-emerald-600 dark:text-emerald-400' : ''
        }`}
      >
        {value}
      </div>
    </div>
  );
}

function Row({label, children}: {label: string; children: React.ReactNode}) {
  return (
    <div className="flex items-start justify-between gap-3 border-b border-border/30 py-1.5 last:border-0">
      <span className="shrink-0 text-[10px] text-muted-foreground">{label}</span>
      <span className="min-w-0 text-right text-[10px] leading-4">{children}</span>
    </div>
  );
}

function RotationTable({rotation}: {rotation: ProviderConsole['rotation']}) {
  const t = useT();
  if (!rotation || !rotation.credentials.length) {
    return <div className="text-[10px] text-muted-foreground">{t('providerConsole.rotation.none')}</div>;
  }
  const stateKey: Record<string, string> = {
    ready: 'providerConsole.rotation.state.ready',
    cooling: 'providerConsole.rotation.state.cooling',
    breaker: 'providerConsole.rotation.state.breaker',
    degraded: 'providerConsole.rotation.state.degraded',
  };
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full text-[10px]">
        <thead className="text-muted-foreground">
          <tr className="text-left">
            <th className="py-1 pr-3 font-normal">{t('providerConsole.rotation.id')}</th>
            <th className="py-1 pr-3 font-normal">{t('providerConsole.rotation.state')}</th>
            <th className="py-1 pr-3 font-normal">{t('providerConsole.rotation.cooldown')}</th>
            <th className="py-1 pr-3 font-normal">{t('providerConsole.rotation.failures')}</th>
            <th className="py-1 font-normal">{t('providerConsole.rotation.weight')}</th>
          </tr>
        </thead>
        <tbody>
          {rotation.credentials.map((entry: ConsoleRotationEntry) => (
            <tr key={entry.id} className="border-t border-border/30">
              <td className="py-1 pr-3 font-mono">{entry.id}</td>
              <td className="py-1 pr-3">
                <Badge variant={entry.state === 'ready' ? 'secondary' : 'destructive'} className="rounded-full">
                  {t(stateKey[entry.state] || 'providerConsole.rotation.state.ready')}
                </Badge>
              </td>
              <td className="py-1 pr-3 tabular-nums">{entry.cooldown_remaining}s</td>
              <td className="py-1 pr-3 tabular-nums">{entry.consecutive_failures}</td>
              <td className="py-1 tabular-nums">{entry.weight}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ProviderConsolePanel({
  providerId,
  providerLabel,
}: {
  providerId: string;
  providerLabel: string;
}) {
  const t = useT();
  const [data, setData] = useState<ProviderConsole | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [tab, setTab] = useState<Tab>('overview');
  const [win, setWin] = useState<Window>('7d');
  const [busy, setBusy] = useState('');
  const [note, setNote] = useState('');
  const [tasks, setTasks] = useState<CodebuddyTaskPage | null>(null);
  const [media, setMedia] = useState<ConsoleMediaInventory | null>(null);

  const isAdmin = data ? !data.readonly : false;
  const showTasks = providerId === 'codebuddy' || providerId === 'gptgrok';

  const load = useCallback(async () => {
    try {
      const snapshot = await providerConsoleApi.snapshot(providerId, win);
      setData(snapshot);
      setError('');
    } catch (e) {
      setError(errText(e));
    }
  }, [providerId, win]);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    load().finally(() => {
      if (alive) setLoading(false);
    });
    return () => {
      alive = false;
    };
  }, [load]);

  const loadTasks = useCallback(async () => {
    try {
      const page = await providerConsoleApi.codebuddyTasks(20);
      setTasks(page);
    } catch {
      // 任务台账是管理台附加信息，拿不到时保留概览，不干扰主面板
    }
  }, []);

  useEffect(() => {
    if (data && !data.readonly && providerId === 'codebuddy') void loadTasks();
  }, [data, providerId, loadTasks]);

  const actions = useMemo(() => new Set(data?.actions || []), [data]);
  const canProbe = actions.has('probe');

  async function runAction(action: string, extra?: {task?: string; task_id?: string; force?: boolean}) {
    if (busy) return;
    setBusy(action);
    setNote('');
    try {
      const reply = await providerConsoleApi.action(providerId, action, extra);
      setNote(reply.message);
      if (reply.ok) {
        notify.ok(t('providerConsole.actionOk'));
      } else {
        notify.err(reply.message || t('providerConsole.actionFailed'));
      }
      await load();
      if (providerId === 'codebuddy') await loadTasks();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  async function loadMedia(kind: 'files' | 'editable') {
    if (busy) return;
    setBusy(kind);
    try {
      const reply = await providerConsoleApi.media('gptgrok', kind);
      setMedia(reply);
      if (!reply.configured) notify.info(t('providerConsole.runtime.mediaNotConfigured'));
      else if (!reply.available) notify.err(reply.message || t('providerConsole.runtime.mediaUnavailable'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy('');
    }
  }

  const tabs: {id: Tab; label: string}[] = [
    {id: 'overview', label: t('providerConsole.tabs.overview')},
    {id: 'connections', label: t('providerConsole.tabs.connections')},
    {id: 'runtime', label: t('providerConsole.tabs.runtime')},
    ...(showTasks ? [{id: 'tasks' as Tab, label: t('providerConsole.tabs.tasks')}] : []),
    {id: 'usage', label: t('providerConsole.tabs.usage')},
  ];

  if (loading && !data) {
    return (
      <div className="mt-3 flex items-center gap-2 rounded-2xl bg-background/60 p-3 text-[11px] text-muted-foreground">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        {t('providerConsole.loading')}
      </div>
    );
  }

  if (!data) {
    return (
      <div className="mt-3 rounded-2xl bg-background/60 p-3">
        <div className="flex items-center gap-2 text-[11px] text-destructive">
          <AlertTriangle className="h-3.5 w-3.5" />
          {error || t('providerConsole.loadFailed')}
        </div>
        <Button size="sm" variant="outline" className="mt-2 rounded-full text-[11px]" onClick={() => void load()}>
          {t('providerConsole.retry')}
        </Button>
      </div>
    );
  }

  const {provider, health, rotation, accounts, credentials, config, usage, logs, features} = data;
  const statusTone = provider.ready ? 'secondary' : 'destructive';

  return (
    <div className="mt-3 rounded-2xl bg-background/60 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <TerminalSquare className="h-4 w-4 text-muted-foreground" />
        <span className="text-[11px] font-medium">{t('providerConsole.title', {name: providerLabel})}</span>
        <Badge variant={statusTone} className="rounded-full">
          {provider.ready ? t('providerConsole.readiness.ready') : t('providerConsole.readiness.notReady')}
        </Badge>
        <Badge variant="outline" className="rounded-full">
          {provider.runtime}
        </Badge>
        {data.readonly && (
          <Badge variant="outline" className="rounded-full">
            {t('providerConsole.readonly')}
          </Badge>
        )}
        <div className="ml-auto flex items-center gap-1">
          {(['24h', '7d', '30d'] as Window[]).map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => setWin(item)}
              className={`rounded-full px-2 py-0.5 text-[10px] ${
                win === item ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'
              }`}
            >
              {t(WINDOW_LABEL[item])}
            </button>
          ))}
          <Button
            size="sm"
            variant="ghost"
            className="h-6 rounded-full px-2 text-[10px]"
            disabled={busy !== ''}
            onClick={() => void load()}
          >
            <RefreshCw className={`mr-1 h-3 w-3 ${busy === 'refresh' ? 'animate-spin' : ''}`} />
            {t('providerConsole.refresh')}
          </Button>
        </div>
      </div>

      <div className="mt-1 text-[10px] leading-4 text-muted-foreground">{t('providerConsole.hint')}</div>

      <div className="mt-2 flex flex-wrap gap-1">
        {tabs.map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => setTab(item.id)}
            className={`rounded-full px-2.5 py-1 text-[10px] ${
              tab === item.id ? 'bg-primary text-primary-foreground' : 'bg-muted text-muted-foreground'
            }`}
          >
            {item.label}
          </button>
        ))}
      </div>

      {tab === 'overview' && (
        <div className="mt-3 space-y-3">
          <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
            <Stat
              label={t('providerConsole.overview.health')}
              value={health.status ? health.status : t('providerConsole.overview.never')}
              tone={health.status === 'ok' ? 'good' : health.status ? 'bad' : undefined}
            />
            <Stat
              label={t('providerConsole.overview.available')}
              value={rotation ? `${rotation.available}/${rotation.credentials.length}` : '—'}
            />
            <Stat label={t('providerConsole.overview.cooling')} value={rotation ? String(rotation.cooling) : '—'} tone={rotation && rotation.cooling ? 'bad' : undefined} />
            <Stat
              label={t('providerConsole.overview.credentials')}
              value={`${credentials.length}`}
            />
          </div>

          <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
            <Stat label={t('providerConsole.overview.requests')} value={usage.requests.toLocaleString()} />
            <Stat label={t('providerConsole.overview.success')} value={usage.success.toLocaleString()} tone="good" />
            <Stat label={t('providerConsole.overview.failed')} value={usage.failed.toLocaleString()} tone={usage.failed ? 'bad' : undefined} />
            <Stat label={t('providerConsole.overview.tokens')} value={usage.tokens.toLocaleString()} />
          </div>

          <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
            <div className="rounded-xl bg-muted/60 p-2.5">
              <div className="text-[10px] font-medium">{t('providerConsole.overview.provider')}</div>
              <Row label={t('providerConsole.overview.status')}>{provider.status || '—'}</Row>
              <Row label={t('providerConsole.overview.vendor')}>{provider.vendor || '—'}</Row>
              <Row label={t('providerConsole.overview.risk')}>{provider.risk_level || '—'}</Row>
              <Row label={t('providerConsole.overview.lastCheck')}>
                {health.checked_at ? stamp(health.checked_at) : t('providerConsole.overview.never')}
                {health.latency_ms !== null ? ` · ${health.latency_ms}ms` : ''}
              </Row>
              {health.error ? <Row label={t('providerConsole.overview.healthError')}>{health.error}</Row> : null}
            </div>

            <div className="rounded-xl bg-muted/60 p-2.5">
              <div className="text-[10px] font-medium">{t('providerConsole.overview.accounts')}</div>
              {accounts ? (
                <>
                  <Row label={t('providerConsole.overview.total')}>{accounts.total}</Row>
                  <Row label={t('providerConsole.overview.disabled')}>{accounts.disabled}</Row>
                  <Row label={t('providerConsole.overview.expired')}>{accounts.expired}</Row>
                  <Row label={t('providerConsole.overview.credits')}>{accounts.credits}</Row>
                </>
              ) : (
                <div className="mt-1 text-[10px] text-muted-foreground">{t('providerConsole.overview.noAccounts')}</div>
              )}
            </div>
          </div>
        </div>
      )}

      {tab === 'connections' && (
        <div className="mt-3 space-y-3">
          <div className="rounded-xl bg-muted/60 p-2.5">
            <div className="mb-1 flex items-center gap-1.5 text-[10px] font-medium">
              <KeyRound className="h-3.5 w-3.5" />
              {t('providerConsole.credentials.title', {count: credentials.length})}
            </div>
            {credentials.length === 0 ? (
              <div className="text-[10px] text-muted-foreground">{t('providerConsole.credentials.none')}</div>
            ) : (
              <div className="space-y-1">
                {credentials.map((item) => (
                  <div key={item.id} className="flex flex-wrap items-center gap-2 border-b border-border/30 pb-1 text-[10px] last:border-0">
                    <Badge variant={item.enabled ? 'secondary' : 'outline'} className="rounded-full">
                      {item.enabled ? t('providerConsole.common.enabled') : t('providerConsole.common.disabled')}
                    </Badge>
                    <span>{item.label || `#${item.id}`}</span>
                    <span className="font-mono text-muted-foreground">{item.secret_masked}</span>
                    <span className="text-muted-foreground">
                      {t('providerConsole.credentials.sortOrder', {order: item.sort_order})}
                    </span>
                    <span className="text-muted-foreground">
                      {t('providerConsole.credentials.baseUrl', {
                        value: item.has_base_url ? t('providerConsole.common.configured') : t('providerConsole.common.notConfigured'),
                      })}
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="rounded-xl bg-muted/60 p-2.5">
            <div className="mb-1 flex items-center gap-1.5 text-[10px] font-medium">
              <ShieldCheck className="h-3.5 w-3.5" />
              {t('providerConsole.rotation.title')}
            </div>
            <div className="mb-2 flex flex-wrap gap-2 text-[10px] text-muted-foreground">
              <span>{t('providerConsole.rotation.rule')}: {rotation?.rule || '—'}</span>
              {rotation?.group ? <span>{t('providerConsole.rotation.group')}: {rotation.group}</span> : null}
            </div>
            <RotationTable rotation={rotation} />
          </div>
        </div>
      )}

      {tab === 'runtime' && (
        <div className="mt-3 space-y-3">
          <div className="rounded-xl bg-muted/60 p-2.5">
            <div className="mb-1 flex items-center gap-1.5 text-[10px] font-medium">
              <Activity className="h-3.5 w-3.5" />
              {t('providerConsole.runtime.title')}
            </div>
            <Row label={t('providerConsole.runtime.models')}>
              {config.model_count ? `${config.model_count}` : t('providerConsole.common.notConfigured')}
            </Row>
            {config.channel ? <Row label={t('providerConsole.runtime.channel')}>{config.channel}</Row> : null}
            {config.work_mode ? <Row label={t('providerConsole.runtime.workMode')}>{config.work_mode}</Row> : null}
            {config.region ? <Row label={t('providerConsole.runtime.region')}>{config.region}</Row> : null}
            {config.product ? <Row label={t('providerConsole.runtime.product')}>{config.product}</Row> : null}
            {config.extra_keys !== undefined ? <Row label={t('providerConsole.runtime.extraKeys')}>{config.extra_keys}</Row> : null}
            <Row label={t('providerConsole.runtime.proxy')}>
              {config.proxy.enabled
                ? t('providerConsole.runtime.proxyValue', {mode: config.proxy.mode, size: config.proxy.size})
                : t('providerConsole.runtime.direct')}
            </Row>
            {config.schedule_policy ? (
              <Row label={t('providerConsole.runtime.schedulePolicy')}>{config.schedule_policy}</Row>
            ) : null}
            {config.refresh !== undefined ? (
              <Row label={t('providerConsole.runtime.refresh')}>
                {config.refresh ? t('providerConsole.common.enabled') : t('providerConsole.common.disabled')}
              </Row>
            ) : null}
            {config.allow_external_refresh !== undefined ? (
              <Row label={t('providerConsole.runtime.externalRefresh')}>
                {config.allow_external_refresh ? t('providerConsole.common.enabled') : t('providerConsole.common.disabled')}
              </Row>
            ) : null}
            {config.restricted_codes?.length ? (
              <Row label={t('providerConsole.runtime.restrictedCodes')}>{config.restricted_codes.join(', ')}</Row>
            ) : null}
            {config.media ? (
              <>
                <Row label={t('providerConsole.runtime.mediaBase')}>
                  {config.media.base_configured ? t('providerConsole.common.configured') : t('providerConsole.common.notConfigured')}
                </Row>
                <Row label={t('providerConsole.runtime.mediaKey')}>
                  {config.media.key_configured ? t('providerConsole.common.configured') : t('providerConsole.common.notConfigured')}
                </Row>
              </>
            ) : null}
          </div>

          {config.session ? (
            <div className="rounded-xl bg-muted/60 p-2.5">
              <div className="mb-1 flex items-center gap-1.5 text-[10px] font-medium">
                <TimerReset className="h-3.5 w-3.5" />
                {t('providerConsole.runtime.sessions')}
              </div>
              <Row label={t('providerConsole.runtime.sessionEntries')}>{features.sessions?.entries ?? config.session.cookie_pool}</Row>
              <Row label={t('providerConsole.runtime.sessionDue')}>{features.sessions?.due ?? 0}</Row>
              <Row label={t('providerConsole.runtime.sessionUpdated')}>
                {features.sessions?.updated_at ? stamp(features.sessions.updated_at) : t('providerConsole.overview.never')}
              </Row>
              <Row label={t('providerConsole.runtime.keepalive')}>
                {config.session.keepalive ? t('providerConsole.common.enabled') : t('providerConsole.common.disabled')}
              </Row>
              {actions.has('keepalive') ? (
                <Button
                  size="sm"
                  variant="outline"
                  className="mt-2 rounded-full text-[10px]"
                  disabled={busy !== ''}
                  onClick={() => void runAction('keepalive')}
                >
                  {busy === 'keepalive' ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : <Play className="mr-1 h-3 w-3" />}
                  {t('providerConsole.runtime.keepaliveRun')}
                </Button>
              ) : null}
            </div>
          ) : null}

          {features.claim ? (
            <div className="rounded-xl bg-muted/60 p-2.5">
              <div className="mb-1 flex items-center gap-1.5 text-[10px] font-medium">
                <Zap className="h-3.5 w-3.5" />
                {t('providerConsole.runtime.claim')}
              </div>
              <Row label={t('providerConsole.runtime.claimEnabled')}>
                {features.claim.enabled ? t('providerConsole.common.enabled') : t('providerConsole.common.disabled')}
              </Row>
              <Row label={t('providerConsole.runtime.claimPath')}>
                {features.claim.path_configured ? t('providerConsole.common.configured') : t('providerConsole.common.notConfigured')}
              </Row>
              <Row label={t('providerConsole.runtime.claimDue')}>
                {features.claim.due ? t('providerConsole.runtime.claimDueYes') : t('providerConsole.runtime.claimDueNo')}
              </Row>
              <Row label={t('providerConsole.runtime.claimedCount')}>{features.claim.claimed_count}</Row>
              {features.claim.last_week ? (
                <Row label={t('providerConsole.runtime.lastWeek')}>{features.claim.last_week}</Row>
              ) : null}
              {actions.has('claim') ? (
                <Button
                  size="sm"
                  className="mt-2 rounded-full text-[10px]"
                  disabled={busy !== '' || !features.claim.enabled || !features.claim.path_configured}
                  onClick={() => void runAction('claim')}
                >
                  {busy === 'claim' ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : <Play className="mr-1 h-3 w-3" />}
                  {t('providerConsole.runtime.runClaim')}
                </Button>
              ) : null}
            </div>
          ) : null}

          {features.proxy_pool ? (
            <div className="rounded-xl bg-muted/60 p-2.5">
              <div className="mb-1 flex items-center gap-1.5 text-[10px] font-medium">
                <Database className="h-3.5 w-3.5" />
                {t('providerConsole.runtime.proxyPool')}
              </div>
              <Row label={t('providerConsole.runtime.proxy')}>
                {features.proxy_pool.enabled
                  ? t('providerConsole.runtime.proxyValue', {mode: features.proxy_pool.mode, size: features.proxy_pool.size})
                  : t('providerConsole.runtime.proxyPoolDisabled')}
              </Row>
              {features.failure_actions ? (
                <>
                  <Row label={t('providerConsole.runtime.schedulePolicy')}>{features.failure_actions.schedule_policy || '—'}</Row>
                  <Row label={t('providerConsole.runtime.refresh')}>
                    {features.failure_actions.refresh ? t('providerConsole.common.enabled') : t('providerConsole.common.disabled')}
                  </Row>
                  <Row label={t('providerConsole.runtime.externalRefresh')}>
                    {features.failure_actions.allow_external_refresh
                      ? t('providerConsole.common.enabled')
                      : t('providerConsole.common.disabled')}
                  </Row>
                </>
              ) : null}
            </div>
          ) : null}

          {features.session_cache ? (
            <div className="rounded-xl bg-muted/60 p-2.5">
              <div className="mb-1 flex items-center gap-1.5 text-[10px] font-medium">
                <Database className="h-3.5 w-3.5" />
                {t('providerConsole.runtime.sessionCache')}
              </div>
              <Row label={t('providerConsole.runtime.sessionEntries')}>{features.session_cache.entries}</Row>
              <Row label={t('providerConsole.runtime.sessionKeys')}>
                {features.session_cache.keys.join(' / ') || '—'}
              </Row>
            </div>
          ) : null}

          {providerId === 'gptgrok' && isAdmin ? (
            <div className="rounded-xl bg-muted/60 p-2.5">
              <div className="mb-1 flex items-center gap-1.5 text-[10px] font-medium">
                <ClipboardList className="h-3.5 w-3.5" />
                {t('providerConsole.runtime.mediaInventory')}
              </div>
              <div className="flex flex-wrap gap-2">
                <Button
                  size="sm"
                  variant="outline"
                  className="rounded-full text-[10px]"
                  disabled={busy !== ''}
                  onClick={() => void loadMedia('files')}
                >
                  {busy === 'files' ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
                  {t('providerConsole.runtime.loadFiles')}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  className="rounded-full text-[10px]"
                  disabled={busy !== ''}
                  onClick={() => void loadMedia('editable')}
                >
                  {busy === 'editable' ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
                  {t('providerConsole.runtime.loadEditable')}
                </Button>
              </div>
              {media ? (
                !media.configured ? (
                  <div className="mt-2 text-[10px] text-muted-foreground">
                    {t('providerConsole.runtime.mediaNotConfigured')}
                  </div>
                ) : !media.available ? (
                  <div className="mt-2 text-[10px] text-destructive">{media.message || t('providerConsole.runtime.mediaUnavailable')}</div>
                ) : media.items.length === 0 ? (
                  <div className="mt-2 text-[10px] text-muted-foreground">{t('providerConsole.common.empty')}</div>
                ) : (
                  <div className="mt-2 overflow-x-auto">
                    <table className="min-w-full text-[10px]">
                      <thead className="text-muted-foreground">
                        <tr className="text-left">
                          <th className="py-1 pr-3 font-normal">{t('providerConsole.runtime.fileName')}</th>
                          <th className="py-1 pr-3 font-normal">{t('providerConsole.runtime.fileBytes')}</th>
                          <th className="py-1 pr-3 font-normal">{t('providerConsole.runtime.fileStatus')}</th>
                          <th className="py-1 font-normal">{t('providerConsole.runtime.fileCreated')}</th>
                        </tr>
                      </thead>
                      <tbody>
                        {media.items.map((item, index) => (
                          <tr key={`${item.id ?? index}`} className="border-t border-border/30">
                            <td className="py-1 pr-3 font-mono">{item.filename || item.id || '—'}</td>
                            <td className="py-1 pr-3 tabular-nums">{item.bytes ?? '—'}</td>
                            <td className="py-1 pr-3">{item.status || item.purpose || item.type || '—'}</td>
                            <td className="py-1">{item.created_at ? stamp(item.created_at) : '—'}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )
              ) : null}
            </div>
          ) : null}

          {canProbe ? (
            <div className="rounded-xl bg-muted/60 p-2.5">
              <div className="flex flex-wrap items-center gap-2">
                <Button
                  size="sm"
                  className="rounded-full text-[10px]"
                  disabled={busy !== ''}
                  onClick={() => void runAction('probe')}
                >
                  {busy === 'probe' ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : <RefreshCw className="mr-1 h-3 w-3" />}
                  {t('providerConsole.runtime.probe')}
                </Button>
                <span className="text-[10px] text-muted-foreground">
                  {busy === 'probe' ? t('providerConsole.runtime.probing') : t('providerConsole.runtime.probeHint')}
                </span>
              </div>
              {note ? <div className="mt-1 text-[10px] text-destructive">{note}</div> : null}
            </div>
          ) : null}
        </div>
      )}

      {tab === 'tasks' && showTasks && (
        <div className="mt-3 space-y-3">
          {providerId === 'codebuddy' ? (
            <>
              <div className="rounded-xl bg-muted/60 p-2.5">
                <div className="mb-1 flex items-center gap-1.5 text-[10px] font-medium">
                  <Play className="h-3.5 w-3.5" />
                  {t('providerConsole.tasks.title')}
                </div>
                <div className="text-[10px] leading-4 text-muted-foreground">{t('providerConsole.tasks.codebuddyHint')}</div>
                {actions.has('run_task') ? (
                  <div className="mt-2 flex flex-wrap gap-2">
                    <Button
                      size="sm"
                      className="rounded-full text-[10px]"
                      disabled={busy !== ''}
                      onClick={() => void runAction('run_task', {task: 'checkin_all'})}
                    >
                      {busy === 'run_task' ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : <Play className="mr-1 h-3 w-3" />}
                      {t('providerConsole.tasks.checkinAll')}
                    </Button>
                    <Button
                      size="sm"
                      variant="outline"
                      className="rounded-full text-[10px]"
                      disabled={busy !== ''}
                      onClick={() => void runAction('run_task', {task: 'refresh_credits', force: true})}
                    >
                      {busy === 'run_task' ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : null}
                      {t('providerConsole.tasks.refreshCredits')}
                    </Button>
                  </div>
                ) : null}
              </div>

              <div className="rounded-xl bg-muted/60 p-2.5">
                <div className="mb-1 flex items-center gap-1.5 text-[10px] font-medium">
                  <ClipboardList className="h-3.5 w-3.5" />
                  {t('providerConsole.tasks.history')}
                </div>
                {!tasks || tasks.items.length === 0 ? (
                  <div className="text-[10px] text-muted-foreground">{t('providerConsole.tasks.noRuns')}</div>
                ) : (
                  <div className="space-y-1">
                    {tasks.items.map((run) => (
                      <div key={run.id} className="rounded-lg bg-background/60 px-2 py-1.5 text-[10px]">
                        <div className="flex flex-wrap items-center gap-2">
                          <Badge variant={run.status === 'succeeded' ? 'secondary' : 'destructive'} className="rounded-full">
                            {run.task}
                          </Badge>
                          <span className="text-muted-foreground">{stamp(run.finished_at)}</span>
                          <span className="text-muted-foreground">{run.actor}</span>
                          {Object.entries(run.stats).map(([key, value]) => (
                            <span key={key} className="tabular-nums text-muted-foreground">
                              {key}={String(value)}
                            </span>
                          ))}
                        </div>
                        {run.message ? <div className="mt-0.5 break-all text-destructive">{run.message}</div> : null}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </>
          ) : null}

          {providerId === 'gptgrok' ? (
            <>
              <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
                <Stat label={t('providerConsole.tasks.total')} value={String(features.queue?.total ?? 0)} />
                <Stat label={t('providerConsole.tasks.runningCount')} value={String(features.queue?.running ?? 0)} />
                <Stat label={t('providerConsole.tasks.completed')} value={String(features.queue?.completed ?? 0)} />
                <Stat label={t('providerConsole.tasks.failed')} value={String(features.queue?.failed ?? 0)} tone={features.queue?.failed ? 'bad' : undefined} />
              </div>

              <div className="rounded-xl bg-muted/60 p-2.5">
                <div className="mb-1 flex items-center gap-1.5 text-[10px] font-medium">
                  <ClipboardList className="h-3.5 w-3.5" />
                  {t('providerConsole.tasks.mediaTasks')}
                </div>
                {!features.media_tasks || features.media_tasks.length === 0 ? (
                  <div className="text-[10px] text-muted-foreground">{t('providerConsole.tasks.noTasks')}</div>
                ) : (
                  <div className="overflow-x-auto">
                    <table className="min-w-full text-[10px]">
                      <thead className="text-muted-foreground">
                        <tr className="text-left">
                          <th className="py-1 pr-3 font-normal">{t('providerConsole.tasks.kind')}</th>
                          <th className="py-1 pr-3 font-normal">{t('providerConsole.tasks.status')}</th>
                          <th className="py-1 pr-3 font-normal">{t('providerConsole.tasks.owner')}</th>
                          <th className="py-1 pr-3 font-normal">{t('providerConsole.tasks.startedAt')}</th>
                          <th className="py-1 font-normal" />
                        </tr>
                      </thead>
                      <tbody>
                        {features.media_tasks.map((task) => (
                          <tr key={task.id} className="border-t border-border/30">
                            <td className="py-1 pr-3 font-mono">{task.kind}</td>
                            <td className="py-1 pr-3">
                              <Badge variant={task.status === 'completed' ? 'secondary' : task.status === 'failed' ? 'destructive' : 'outline'} className="rounded-full">
                                {task.status}
                              </Badge>
                            </td>
                            <td className="py-1 pr-3 font-mono">{task.owner}</td>
                            <td className="py-1 pr-3">{stamp(task.created_at)}</td>
                            <td className="py-1 text-right">
                              {actions.has('cancel_task') && (task.status === 'queued' || task.status === 'running') ? (
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  className="h-6 rounded-full px-2 text-[10px]"
                                  disabled={busy !== ''}
                                  onClick={() => void runAction('cancel_task', {task_id: task.id})}
                                >
                                  {t('providerConsole.tasks.cancel')}
                                </Button>
                              ) : null}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>

              <div className="rounded-xl bg-muted/60 p-2.5">
                <div className="mb-1 flex items-center gap-1.5 text-[10px] font-medium">
                  <TimerReset className="h-3.5 w-3.5" />
                  {t('providerConsole.tasks.videoTasks')}
                </div>
                {!features.video_tasks || features.video_tasks.length === 0 ? (
                  <div className="text-[10px] text-muted-foreground">{t('providerConsole.tasks.noTasks')}</div>
                ) : (
                  <div className="space-y-1">
                    {features.video_tasks.map((task) => (
                      <div key={task.id} className="flex flex-wrap items-center gap-2 border-b border-border/30 pb-1 text-[10px] last:border-0">
                        <Badge variant="outline" className="rounded-full">
                          {task.status}
                        </Badge>
                        <span className="font-mono">{task.id}</span>
                        <span>{task.model}</span>
                        <span className="text-muted-foreground">{task.seconds}s</span>
                        <span className="text-muted-foreground">${(task.billed_micro / 1e6).toFixed(4)}</span>
                        <span className="text-muted-foreground">{stamp(task.created_at)}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </>
          ) : null}
        </div>
      )}

      {tab === 'usage' && (
        <div className="mt-3 space-y-3">
          <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
            <Stat label={t('providerConsole.overview.requests')} value={usage.requests.toLocaleString()} />
            <Stat label={t('providerConsole.overview.failed')} value={usage.failed.toLocaleString()} tone={usage.failed ? 'bad' : undefined} />
            <Stat label={t('providerConsole.overview.credit')} value={`$${usage.credit_usd.toFixed(4)}`} />
            <Stat label={t('providerConsole.overview.latency')} value={`${usage.latency_avg_ms}ms`} />
          </div>

          <div className="rounded-xl bg-muted/60 p-2.5">
            <div className="mb-2 text-[10px] font-medium">{t('providerConsole.usage.daily')}</div>
            <div className="flex h-16 items-end gap-1">
              {usage.daily.map((point) => {
                const peak = Math.max(1, ...usage.daily.map((item) => item.requests));
                const height = Math.max(2, Math.round((point.requests / peak) * 52));
                return (
                  <div key={point.day} className="flex flex-1 flex-col items-center gap-1">
                    <div
                      className={`w-full rounded-t ${point.failed ? 'bg-destructive/70' : 'bg-primary/70'}`}
                      style={{height}}
                      title={`${point.day} · ${point.requests}`}
                    />
                    <span className="text-[9px] text-muted-foreground">{point.day.slice(5)}</span>
                  </div>
                );
              })}
            </div>
          </div>

          <div className="rounded-xl bg-muted/60 p-2.5">
            <div className="mb-1 text-[10px] font-medium">{t('providerConsole.usage.topModels')}</div>
            {usage.top_models.length === 0 ? (
              <div className="text-[10px] text-muted-foreground">{t('providerConsole.usage.noTopModels')}</div>
            ) : (
              <div className="space-y-1">
                {usage.top_models.map((item) => (
                  <div key={item.model} className="flex items-center justify-between gap-2 text-[10px]">
                    <span className="truncate font-mono">{item.model}</span>
                    <span className="tabular-nums text-muted-foreground">{item.requests}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="rounded-xl bg-muted/60 p-2.5">
            <div className="mb-1 flex items-center gap-1.5 text-[10px] font-medium">
              <BadgeCheck className="h-3.5 w-3.5" />
              {t('providerConsole.usage.recentLogs')}
            </div>
            {logs.length === 0 ? (
              <div className="text-[10px] text-muted-foreground">{t('providerConsole.usage.noLogs')}</div>
            ) : (
              <div className="overflow-x-auto">
                <table className="min-w-full text-[10px]">
                  <thead className="text-muted-foreground">
                    <tr className="text-left">
                      <th className="py-1 pr-3 font-normal">{t('providerConsole.usage.time')}</th>
                      <th className="py-1 pr-3 font-normal">{t('providerConsole.usage.model')}</th>
                      <th className="py-1 pr-3 font-normal">{t('providerConsole.usage.statusCode')}</th>
                      <th className="py-1 pr-3 font-normal">{t('providerConsole.usage.latencyMs')}</th>
                      <th className="py-1 font-normal">{t('providerConsole.usage.error')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {logs.map((log) => (
                      <tr key={log.id} className="border-t border-border/30">
                        <td className="py-1 pr-3 whitespace-nowrap">{stamp(log.ts)}</td>
                        <td className="py-1 pr-3 font-mono">{log.model || '—'}</td>
                        <td className={`py-1 pr-3 tabular-nums ${log.status >= 400 ? 'text-destructive' : ''}`}>{log.status}</td>
                        <td className="py-1 pr-3 tabular-nums">{log.latency_ms}ms</td>
                        <td className="py-1 break-all text-muted-foreground">{log.error || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
