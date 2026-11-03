'use client';

import {useCallback, useEffect, useState} from 'react';
import {ShieldCheck, LockKeyhole, FileClock, ChevronLeft, ChevronRight, Ban, Lock} from 'lucide-react';
import {useHeartbeat} from '@/lib/use-heartbeat';
import {notify} from '@/lib/toast';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {securityApi, errText} from '@/lib/api';
import type {AuditLog, GuardInfo, LoginFailuresResponse} from '@/lib/types';
import {fmtDateTime, fmtNumber} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

const AUDIT_PAGE_SIZE = 20;

/** 审计动作 → i18n 键（动作本身是后端固定的英文枚举，这里只做展示名映射） */
const AUDIT_LABEL_KEYS: Record<string, string> = {
  login: 'security.auditLogin',
  login_failed: 'security.auditLoginFailed',
  update_user: 'security.auditUpdateUser',
  delete_user: 'security.auditDeleteUser',
  add_user: 'security.auditAddUser',
  'logs.clear': 'security.auditLogsClear',
};

/**
 * 审计详情本地化。
 *
 * 后端把详情存成中文（如「角色=admin；来源 1.2.3.4」）——存储层保持语言中立
 * （否则同一条记录会因写入者语言不同而不一致），展示时按固定分词翻译已知标记，
 * 认不出的片段原样保留。
 */
function auditDetail(detail: string | null | undefined, t: (key: string, params?: Record<string, string>) => string): string {
  if (!detail) return '—';
  return detail
    .split('；')
    .map((part) => {
      const from = /^来源\s+(.+)$/.exec(part);
      if (from) return t('security.detailFrom', {ip: from[1]});
      if (part === '密码=已重置') return t('security.detailPasswordReset');
      const role = /^角色=(.+)$/.exec(part);
      if (role) return t('security.detailRole', {role: role[1]});
      const segment = /^段=(.*)$/.exec(part);
      if (segment) return t('security.detailSegment', {segments: segment[1]});
      const tokens = part.split('、').map((token) => {
        if (token === '改密码') return t('security.detailPasswordChanged');
        if (token === '改角色') return t('security.detailRoleChanged');
        return null;
      });
      if (tokens.every((v) => v !== null)) {
        return tokens.join(t('security.detailChangedSeparator'));
      }
      return part;
    })
    .join(t('security.detailSeparator'));
}

function AuditTable({rows}: {rows: AuditLog[]}) {
  const t = useT();
  return (
    <Table>
      <TableHeader>
        <TableRow className="border-b border-border/60 hover:bg-transparent">
          <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('logs.colTime')}</TableHead>
          <TableHead className="text-[11px] text-muted-foreground">{t('security.action')}</TableHead>
          <TableHead className="text-[11px] text-muted-foreground">{t('security.actor')}</TableHead>
          <TableHead className="text-[11px] text-muted-foreground">{t('security.target')}</TableHead>
          <TableHead className="pr-4 text-[11px] text-muted-foreground">{t('security.detail')}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((a) => (
          <TableRow key={a.id} className="border-b border-border/40">
            <TableCell className="pl-4 text-xs tabular-nums text-muted-foreground">
              {fmtDateTime(a.ts)}
            </TableCell>
            <TableCell>
              <Badge
                variant="secondary"
                className={
                  'rounded-full text-[10px] ' +
                  (a.action === 'login_failed'
                    ? 'bg-red-500/12 text-red-600 dark:text-red-400'
                    : a.action === 'login'
                      ? 'text-emerald-600 dark:text-emerald-400'
                      : '')
                }
              >
                {AUDIT_LABEL_KEYS[a.action] ? t(AUDIT_LABEL_KEYS[a.action]) : a.action}
              </Badge>
            </TableCell>
            {/* 登录失败时后端把「没填用户名」记成 (空) —— 这个标记也要跟着界面语言走 */}
            <TableCell className="text-xs">
              {a.actor === '(空)' ? t('security.actorEmpty') : a.actor || '—'}
            </TableCell>
            <TableCell className="text-xs text-muted-foreground">{a.target || '—'}</TableCell>
            <TableCell className="max-w-[420px] truncate pr-4 text-[11px] text-muted-foreground" title={a.detail}>
              {auditDetail(a.detail, t)}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export default function SecurityPage() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [guard, setGuard] = useState<GuardInfo | null>(null);
  const [failures, setFailures] = useState<LoginFailuresResponse | null>(null);
  const [audit, setAudit] = useState<AuditLog[]>([]);
  const [auditTotal, setAuditTotal] = useState(0);
  const [auditPage, setAuditPage] = useState(1);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    const [g, f, a] = await Promise.allSettled([
      securityApi.guard(),
      securityApi.loginFailures(50),
      // 审计日志接口对后端只放行 admin 的全量视图，非 admin 不必白跑一次 403
      isAdmin
        ? securityApi.audit({limit: AUDIT_PAGE_SIZE, offset: (auditPage - 1) * AUDIT_PAGE_SIZE})
        : Promise.resolve(null),
    ]);
    if (g.status === 'fulfilled') setGuard(g.value);
    if (f.status === 'fulfilled') setFailures(f.value);
    if (a.status === 'fulfilled' && a.value) {
      setAudit(a.value.items);
      setAuditTotal(a.value.total);
    }
    if (g.status === 'rejected' && f.status === 'rejected' && a.status === 'rejected') {
      notify.err(errText(g.reason));
    }
    setLoading(false);
  }, [auditPage, isAdmin]);

  useEffect(() => {
    load();
  }, [load]);

  // 登录节流状态随时间变化（锁定会自动解除），心跳保持同步
  useHeartbeat(load, 60000);

  const auditPages = Math.max(1, Math.ceil(auditTotal / AUDIT_PAGE_SIZE));

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title={t('security.title')}
        description={t('security.description')}
      />

      {/* 上游地址守卫：net_guard 拒绝环回/私网/链路本地/云元数据地址作为上游。
          清单是代码常量、开关来自环境变量，都不是库表 —— 本区块只读展示，
          不提供页面上改不了的假开关。 */}
      <section className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <ShieldCheck className="h-4 w-4" />
          <span className="text-sm font-medium">{t('security.guardTitle')}</span>
          <span className="text-[11px] text-muted-foreground">{t('security.guardHint')}</span>
        </div>
        {guard ? (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div className="space-y-3">
              <div className="flex items-center justify-between rounded-2xl bg-background/60 px-3 py-2.5">
                <span className="text-xs">{t('security.guardAllowPrivate')}</span>
                {guard.allow_private_upstreams ? (
                  <Badge variant="secondary" className="rounded-full bg-amber-500/12 text-amber-600 dark:text-amber-400">
                    {t('security.guardAllowed')}
                  </Badge>
                ) : (
                  <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">
                    {t('security.guardDenied')}
                  </Badge>
                )}
              </div>
              <div className="rounded-2xl bg-background/60 px-3 py-2.5">
                <div className="mb-1.5 text-[11px] text-muted-foreground">{t('security.guardSchemes')}</div>
                <div className="flex flex-wrap gap-1.5">
                  {guard.allowed_schemes.map((s) => (
                    <Badge key={s} variant="secondary" className="rounded-full font-mono text-[10px]">{s}</Badge>
                  ))}
                </div>
              </div>
              <div className="rounded-2xl bg-background/60 px-3 py-2.5">
                <div className="mb-1.5 text-[11px] text-muted-foreground">{t('security.guardCategories')}</div>
                <div className="flex flex-wrap gap-1.5">
                  {guard.blocked_categories.map((c) => (
                    <Badge key={c} variant="secondary" className="rounded-full font-mono text-[10px] text-red-600 dark:text-red-400">
                      <Ban className="h-3 w-3" />{c}
                    </Badge>
                  ))}
                </div>
              </div>
            </div>
            <div className="space-y-3">
              <div className="rounded-2xl bg-background/60 px-3 py-2.5">
                <div className="mb-1.5 text-[11px] text-muted-foreground">
                  {t('security.guardHostnames', {n: guard.blocked_hostnames.length})}
                </div>
                <div className="max-h-24 overflow-y-auto font-mono text-[11px] leading-5 text-muted-foreground">
                  {guard.blocked_hostnames.join('\n')}
                </div>
              </div>
              <div className="rounded-2xl bg-background/60 px-3 py-2.5">
                <div className="mb-1.5 text-[11px] text-muted-foreground">
                  {t('security.guardAddresses', {n: guard.blocked_addresses.length})}
                </div>
                <div className="max-h-24 overflow-y-auto font-mono text-[11px] leading-5 text-muted-foreground">
                  {guard.blocked_addresses.join('\n')}
                </div>
              </div>
            </div>
          </div>
        ) : (
          !loading && <div className="py-6 text-center text-xs text-muted-foreground">{t('security.guardLoadFailed')}</div>
        )}
      </section>

      {/* 登录失败/封禁状态：进程内存里的节流计数 + audit_logs 的历史统计。 */}
      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <LockKeyhole className="h-4 w-4" />
            <span className="text-sm font-medium">{t('security.loginTitle')}</span>
            {failures && (
              <span className="text-[11px] text-muted-foreground">
                {t('security.loginWindow', {sec: failures.window_sec, max: failures.max_failures})}
              </span>
            )}
          </div>
          {failures && (
            <Badge variant="secondary" className="shrink-0 rounded-full tabular-nums">
              {failures.scope === 'self'
                ? t('security.loginStatsSelf', {today: failures.stats.today, total: failures.stats.total})
                : t('security.loginStats', {today: failures.stats.today, total: failures.stats.total})}
            </Badge>
          )}
        </div>
        {failures?.scope === 'self' ? (
          // 非 admin：后端只给聚合数字，这里也就不渲染 IP / 用户名表
          <div className="grid grid-cols-1 gap-3 px-4 pb-4 md:grid-cols-2">
            <div className="rounded-2xl bg-background/60 px-3 py-2.5">
              <div className="mb-1.5 text-[11px] text-muted-foreground">{t('security.loginColFailures')}</div>
              <div className="text-sm tabular-nums">{failures.window_failures ?? 0}</div>
            </div>
            <div className="rounded-2xl bg-background/60 px-3 py-2.5">
              <div className="mb-1.5 text-[11px] text-muted-foreground">{t('security.loginColLast')}</div>
              <div className="text-sm tabular-nums text-muted-foreground">
                {failures.last_failed_ts ? fmtDateTime(failures.last_failed_ts) : '—'}
              </div>
            </div>
            <div className="text-[11px] text-muted-foreground md:col-span-2">{t('security.loginSelfHint')}</div>
          </div>
        ) : failures && failures.active.length > 0 ? (
          <Table>
            <TableHeader>
              <TableRow className="border-b border-border/60 hover:bg-transparent">
                <TableHead className="pl-4 text-[11px] text-muted-foreground">IP</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">{t('security.loginColUser')}</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">{t('security.loginColFailures')}</TableHead>
                <TableHead className="text-[11px] text-muted-foreground">{t('accounts.colStatus')}</TableHead>
                <TableHead className="pr-4 text-[11px] text-muted-foreground">{t('security.loginColLast')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {failures.active.map((f) => (
                <TableRow key={`${f.ip}|${f.username}`} className="border-b border-border/40">
                  <TableCell className="pl-4 font-mono text-xs">{f.ip}</TableCell>
                  <TableCell className="text-xs">{f.username || t('security.actorEmpty')}</TableCell>
                  <TableCell className="text-xs tabular-nums">{f.failures}</TableCell>
                  <TableCell>
                    {f.locked ? (
                      <Badge variant="destructive" className="rounded-full">
                        {t('security.loginLocked', {sec: f.retry_after})}
                      </Badge>
                    ) : (
                      <Badge variant="secondary" className="rounded-full text-amber-600 dark:text-amber-400">
                        {t('security.loginCounting')}
                      </Badge>
                    )}
                  </TableCell>
                  <TableCell className="pr-4 text-xs tabular-nums text-muted-foreground">
                    {fmtDateTime(f.last_ts)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        ) : (
          !loading && (
            <EmptyState
              icon={LockKeyhole}
              title={t('security.loginEmpty')}
              description={t('security.loginEmptyDesc')}
              className="flex flex-col items-center justify-center py-10 text-center"
            />
          )
        )}
      </section>

      {/* 管理端审计日志：登录成败、改密码、清日志等敏感操作留痕，分页浏览。 */}
      <section className="overflow-hidden rounded-[20px] bg-muted">
        <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <FileClock className="h-4 w-4" />
            {t('security.auditLog')}
            <span className="hidden text-[11px] font-normal text-muted-foreground sm:inline">
              {t('security.auditLogHint')}
            </span>
          </div>
          {isAdmin && (
            <Badge variant="secondary" className="shrink-0 rounded-full tabular-nums">
              {t('security.auditCount', {count: auditTotal, n: auditTotal})}
            </Badge>
          )}
        </div>
        {isAdmin ? (
          <>
            {audit.length > 0 ? (
              <AuditTable rows={audit} />
            ) : (
              !loading && (
                <EmptyState
                  icon={FileClock}
                  title={t('security.noAudit')}
                  description={t('security.noAuditDesc')}
                  className="flex flex-col items-center justify-center py-14 text-center"
                />
              )
            )}
            <div className="flex items-center justify-between px-4 py-3">
              <div className="text-[11px] text-muted-foreground">
                {t('logs.pageInfo', {total: fmtNumber(auditTotal), page: auditPage, pages: auditPages})}
              </div>
              <div className="flex gap-1">
                <Button
                  variant="outline"
                  size="icon"
                  className="h-7 w-7 rounded-md"
                  disabled={auditPage <= 1}
                  onClick={() => setAuditPage((p) => Math.max(1, p - 1))}
                >
                  <ChevronLeft className="h-3.5 w-3.5" />
                </Button>
                <Button
                  variant="outline"
                  size="icon"
                  className="h-7 w-7 rounded-md"
                  disabled={auditPage >= auditPages}
                  onClick={() => setAuditPage((p) => Math.min(auditPages, p + 1))}
                >
                  <ChevronRight className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          </>
        ) : (
          <EmptyState
            icon={Lock}
            title={t('security.auditAdminOnly')}
            description={t('security.auditAdminOnlyDesc')}
            className="flex flex-col items-center justify-center py-14 text-center"
          />
        )}
      </section>
    </div>
  );
}
