'use client';

/**
 * 「账号从哪来」的形态说明。
 *
 * 账号页过去只有两种表达：有「导入账号」或一句「未启用」。实际上后端把
 * 账号来源分成三类（managed）：
 *   local   unified / external / quota —— 凭证能在本页落库并参与转发
 *   sidecar 账号生命周期在整个进程外，本页永远没有入口
 *   demo    内置种子数据，只读展示
 * 这里把三类在页面上分开讲，并把 sidecar 的进程、地址与可达性摆在明面上。
 */

import type {AccountManaged, ProviderAccountOps} from './account-ops';
import type {ProviderHealthEntry, ProviderInfo} from '@/lib/providers';
import {useT} from '@/lib/i18n/provider';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {
  Check,
  ExternalLink,
  RefreshCw,
  TriangleAlert,
  Info,
} from 'lucide-react';

export type AccountSourceKind = 'local' | 'sidecar' | 'demo';

/** managed 是后端口径；local 含 unified / external / quota 三种可导入形态 */
export function sourceKindOf(managed?: AccountManaged): AccountSourceKind {
  if (managed === 'sidecar') return 'sidecar';
  if (managed === 'demo') return 'demo';
  return 'local';
}

const KIND_LABEL: Record<AccountSourceKind, string> = {
  local: 'accounts.sourceLocalTag',
  sidecar: 'accounts.sourceExternalTag',
  demo: 'accounts.sourceDemoTag',
};

const KIND_TAG_CLASS: Record<AccountSourceKind, string> = {
  local: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400',
  sidecar: 'bg-sky-500/10 text-sky-700 dark:text-sky-400',
  demo: 'bg-slate-500/10 text-slate-600 dark:text-slate-300',
};

interface Props {
  filter: string;
  items: ProviderAccountOps[];
  providers: ProviderInfo[];
  health: Record<string, ProviderHealthEntry>;
  /** 正在检测的服务商 id；`*` 表示整轮检测 */
  probing: string | null;
  isAdmin: boolean;
  onProbe: (id: string) => void;
  onProbeAllSidecars: () => void;
}

function addressOf(provider?: ProviderInfo): string {
  if (!provider) return '';
  return provider.base_url || provider.default_base_url || '';
}

/** 侧车可达性：没有检测记录时如实写「未检测」，不拿 enabled/readiness 反推 */
function HealthBadge({entry, probing}: {entry?: ProviderHealthEntry; probing: boolean}) {
  const t = useT();
  if (probing) {
    return (
      <Badge variant="secondary" className="rounded-full bg-muted px-1.5 text-[10px] text-muted-foreground">
        <RefreshCw className="mr-1 h-3 w-3 animate-spin" />
        {t('accounts.sourceSidecarProbing')}
      </Badge>
    );
  }
  if (!entry || entry.status === null) {
    return (
      <Badge variant="secondary" className="rounded-full bg-muted px-1.5 text-[10px] text-muted-foreground">
        <Info className="mr-1 h-3 w-3" />
        {t('accounts.sourceSidecarUnprobed')}
      </Badge>
    );
  }
  if (entry.status === 'ok') {
    return (
      <Badge variant="secondary" className="rounded-full bg-emerald-500/10 px-1.5 text-[10px] text-emerald-700 dark:text-emerald-400">
        <Check className="mr-1 h-3 w-3" />
        {t('accounts.sourceSidecarReachable')}
        {entry.latency_ms != null ? ` · ${entry.latency_ms}ms` : ''}
      </Badge>
    );
  }
  if (entry.status === 'unsupported') {
    return (
      <Badge variant="secondary" className="rounded-full bg-muted px-1.5 text-[10px] text-muted-foreground">
        <Info className="mr-1 h-3 w-3" />
        {t('accounts.sourceSidecarUnsupported')}
      </Badge>
    );
  }
  return (
    <Badge variant="secondary" className="rounded-full bg-amber-500/10 px-1.5 text-[10px] text-amber-700 dark:text-amber-400">
      <TriangleAlert className="mr-1 h-3 w-3" />
      {t('accounts.sourceSidecarUnreachable')}
    </Badge>
  );
}

function RequiresLine({p}: {p: ProviderAccountOps}) {
  const t = useT();
  return (
    <div className="mt-1 text-[10px] leading-4 text-muted-foreground">
      {p.readiness === 'ready'
        ? t('accounts.providerNoRequires')
        : t('accounts.sourceRequiresLabel', {
            requires: p.requires.join(t('common.listSeparator')),
          })}
    </div>
  );
}

function ProviderRow({
  p,
  provider,
  entry,
  probing,
  isAdmin,
  showProbe,
  onProbe,
}: {
  p: ProviderAccountOps;
  provider?: ProviderInfo;
  entry?: ProviderHealthEntry;
  probing: boolean;
  isAdmin: boolean;
  showProbe: boolean;
  onProbe: (id: string) => void;
}) {
  const t = useT();
  const kind = sourceKindOf(p.managed);
  const address = addressOf(provider);
  return (
    <div className="rounded-lg border border-border/40 bg-background/60 px-2.5 py-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-[11px] font-medium">{p.label}</span>
        <Badge variant="secondary" className={'rounded-full px-1.5 text-[10px] ' + KIND_TAG_CLASS[kind]}>
          {t(KIND_LABEL[kind])}
        </Badge>
        {/* readiness 与 enabled 各自独立，不合成一个词 */}
        <Badge
          variant="secondary"
          className={
            'rounded-full px-1.5 text-[10px] ' +
            (p.readiness === 'ready'
              ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
              : 'bg-amber-500/10 text-amber-700 dark:text-amber-400')
          }
        >
          {t(p.readiness === 'ready' ? 'accounts.readinessReady' : 'accounts.readinessNeedsConfig')}
        </Badge>
        <Badge variant="secondary" className="rounded-full bg-muted px-1.5 text-[10px] text-muted-foreground">
          {t(p.enabled ? 'accounts.enabledOn' : 'accounts.enabledOff')}
        </Badge>
        {kind === 'sidecar' && <HealthBadge entry={entry} probing={probing} />}
      </div>

      <RequiresLine p={p} />

      {kind === 'sidecar' && (
        <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px]">
          {address && (
            <span className="font-mono text-muted-foreground">
              {t('accounts.sourceSidecarAddress')} {address}
            </span>
          )}
          {address && /^https?:\/\//.test(address) && (
            <a
              href={address}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-0.5 text-sky-700 underline underline-offset-2 dark:text-sky-400"
            >
              {t('accounts.sourceSidecarOpen')}
              <ExternalLink className="h-3 w-3" />
            </a>
          )}
          {showProbe && isAdmin && (
            <Button
              size="sm"
              variant="ghost"
              className="h-5 rounded-full px-1.5 text-[10px]"
              disabled={probing}
              onClick={() => onProbe(p.id)}
            >
              <RefreshCw className={'mr-1 h-3 w-3 ' + (probing ? 'animate-spin' : '')} />
              {probing ? t('accounts.sourceSidecarProbing') : t('accounts.sourceSidecarProbe')}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

export function AccountSourceGuide({
  filter,
  items,
  providers,
  health,
  probing,
  isAdmin,
  onProbe,
  onProbeAllSidecars,
}: Props) {
  const t = useT();
  if (!items.length) return null;
  const selected = filter === 'all' ? undefined : items.find((p) => p.id === filter);

  const providerOf = (id: string) => providers.find((p) => p.id === id);

  if (filter !== 'all') {
    if (!selected) return null;
    const kind = sourceKindOf(selected.managed);
    const provider = providerOf(selected.id);
    const address = addressOf(provider);
    const groupDesc =
      kind === 'local'
        ? 'accounts.sourceGroupLocalDesc'
        : kind === 'sidecar'
          ? 'accounts.sourceGroupSidecarDesc'
          : 'accounts.sourceGroupDemoDesc';
    return (
      <section className="rounded-[20px] bg-muted px-4 py-3.5">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-semibold">{t('accounts.sourceTitle')}</span>
          <Badge variant="secondary" className={'rounded-full px-1.5 text-[10px] ' + KIND_TAG_CLASS[kind]}>
            {t(KIND_LABEL[kind])}
          </Badge>
          <Badge
            variant="secondary"
            className={
              'rounded-full px-1.5 text-[10px] ' +
              (selected.readiness === 'ready'
                ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
                : 'bg-amber-500/10 text-amber-700 dark:text-amber-400')
            }
          >
            {t(selected.readiness === 'ready' ? 'accounts.readinessReady' : 'accounts.readinessNeedsConfig')}
          </Badge>
          <Badge variant="secondary" className="rounded-full bg-muted px-1.5 text-[10px] text-muted-foreground">
            {t(selected.enabled ? 'accounts.enabledOn' : 'accounts.enabledOff')}
          </Badge>
        </div>

        <p className="mt-1 text-[11px] leading-relaxed text-muted-foreground">{t(groupDesc)}</p>

        <div className="mt-2 flex flex-col gap-1">
          <div className="text-[11px] leading-relaxed">
            <span className="text-muted-foreground">
              {t(selected.readiness === 'ready' ? 'accounts.providerNoRequires' : 'accounts.sourceRequiresLabel', {
                requires: selected.requires.join(t('common.listSeparator')),
              })}
            </span>
          </div>

          {kind === 'sidecar' && (
            <div className="flex flex-wrap items-center gap-2 text-[11px]">
              {address && (
                <span className="font-mono text-muted-foreground">
                  {t('accounts.sourceSidecarAddress')} {address}
                </span>
              )}
              <HealthBadge entry={health[selected.id]} probing={probing === selected.id} />
              {address && /^https?:\/\//.test(address) && (
                <a
                  href={address}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-0.5 text-sky-700 underline underline-offset-2 dark:text-sky-400"
                >
                  {t('accounts.sourceSidecarOpen')}
                  <ExternalLink className="h-3 w-3" />
                </a>
              )}
              {isAdmin && (
                <Button
                  size="sm"
                  variant="outline"
                  className="h-6 rounded-full px-2 text-[10px]"
                  disabled={probing === selected.id}
                  onClick={() => onProbe(selected.id)}
                >
                  <RefreshCw className={'mr-1 h-3 w-3 ' + (probing === selected.id ? 'animate-spin' : '')} />
                  {probing === selected.id
                    ? t('accounts.sourceSidecarProbing')
                    : health[selected.id]?.status
                      ? t('accounts.sourceSidecarReprobe')
                      : t('accounts.sourceSidecarProbe')}
                </Button>
              )}
            </div>
          )}

          {kind === 'sidecar' && (
            <p className="text-[10px] leading-4 text-muted-foreground">{t('accounts.sourceSidecarHowto')}</p>
          )}
          {kind === 'sidecar' && health[selected.id]?.status === 'failed' && (
            <p className="text-[10px] leading-4 text-amber-700 dark:text-amber-400">
              {t('accounts.sourceSidecarOfflineNote', {requires: selected.requires.join(t('common.listSeparator'))})}
            </p>
          )}
          {kind === 'demo' && (
            <p className="text-[10px] leading-4 text-muted-foreground">{t('accounts.sourceDemoLocked')}</p>
          )}
        </div>

        <p className="mt-2 border-t border-border/40 pt-1.5 text-[10px] leading-4 text-muted-foreground">
          {t('accounts.sourceIndependenceNote')}
        </p>
      </section>
    );
  }

  const groups: {kind: AccountSourceKind; title: string; desc: string; rows: ProviderAccountOps[]}[] = [
    {
      kind: 'local' as AccountSourceKind,
      title: t('accounts.sourceGroupLocalTitle'),
      desc: t('accounts.sourceGroupLocalDesc'),
      rows: items.filter((p) => sourceKindOf(p.managed) === 'local'),
    },
    {
      kind: 'sidecar' as AccountSourceKind,
      title: t('accounts.sourceGroupSidecarTitle'),
      desc: t('accounts.sourceGroupSidecarDesc'),
      rows: items.filter((p) => sourceKindOf(p.managed) === 'sidecar'),
    },
    {
      kind: 'demo' as AccountSourceKind,
      title: t('accounts.sourceGroupDemoTitle'),
      desc: t('accounts.sourceGroupDemoDesc'),
      rows: items.filter((p) => sourceKindOf(p.managed) === 'demo'),
    },
  ].filter((g) => g.rows.length > 0);

  const probingAll = probing === '*';

  return (
    <section className="rounded-[20px] bg-muted px-4 py-3.5">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-semibold">{t('accounts.sourceTitle')}</span>
        {isAdmin && groups.some((g) => g.kind === 'sidecar') && (
          <Button
            size="sm"
            variant="outline"
            className="h-6 rounded-full px-2 text-[10px]"
            disabled={probingAll}
            onClick={onProbeAllSidecars}
          >
            <RefreshCw className={'mr-1 h-3 w-3 ' + (probingAll ? 'animate-spin' : '')} />
            {probingAll ? t('accounts.sourceSidecarProbing') : t('accounts.sourceProbeAllSidecars')}
          </Button>
        )}
      </div>
      <p className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">{t('accounts.sourceIntroAll')}</p>

      <div className="mt-2.5 grid gap-2 md:grid-cols-3">
        {groups.map((g) => (
          <div key={g.kind} className="rounded-xl border border-border/50 bg-background/50 p-2.5">
            <div className="flex items-center justify-between gap-2">
              <span className="text-[11px] font-semibold">{g.title}</span>
              <span className="text-[10px] tabular-nums text-muted-foreground">{g.rows.length}</span>
            </div>
            <p className="mt-0.5 text-[10px] leading-4 text-muted-foreground">{g.desc}</p>
            <div className="mt-2 flex flex-col gap-1.5">
              {g.rows.map((p) => (
                <ProviderRow
                  key={p.id}
                  p={p}
                  provider={providerOf(p.id)}
                  entry={health[p.id]}
                  probing={probing === p.id || probingAll}
                  isAdmin={isAdmin}
                  showProbe={false}
                  onProbe={onProbe}
                />
              ))}
            </div>
          </div>
        ))}
      </div>

      <p className="mt-2 text-[10px] leading-4 text-muted-foreground">{t('accounts.sourceIndependenceNote')}</p>
    </section>
  );
}
