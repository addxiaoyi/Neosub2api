'use client';

import Link from 'next/link';
import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {
  Layers3,
  Lock,
  Plus,
  Pencil,
  Trash2,
  RefreshCcw,
  Snowflake,
  Database,
  AlertCircle,
  ScrollText,
} from 'lucide-react';
import {sub2Api, errText} from '@/lib/api';
import {notify} from '@/lib/toast';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import type {Sub2apiAccount, Sub2apiGroup, Sub2apiLedgerEntry} from '@/lib/types';
import {fmtDateTime, fmtNumber} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {Button} from '@/components/ui/button';
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
import {ScrollArea} from '@/components/ui/scroll-area';
import {Tabs, TabsContent, TabsList, TabsTrigger} from '@/components/ui/tabs';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';

type TabKey = 'groups' | 'accounts' | 'ledger';

function quotaText(used: number, limit: number): string {
  if (limit <= 0) return '—';
  return `${fmtNumber(used)} / ${fmtNumber(limit)}`;
}

function statusBadge(enabled: boolean, t: (k: string) => string) {
  return enabled ?
    <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">
      {t('quota.enabled')}
    </Badge> :
    <Badge variant="secondary" className="rounded-full text-muted-foreground">
      {t('quota.disabled')}
    </Badge>;
}

export default function QuotaPage() {
  const t = useT();
  const {isAdmin, loading} = useAuth();

  const policiesEntry = (
    <Button asChild variant="outline" className="rounded-full">
      <Link href="/quota/policies">{t('quotaPolicy.title')}</Link>
    </Button>
  );

  // 三个 tab 的数据接口已收紧为 admin-only：非管理员不发请求，直接给说明，
  // 避免三块 403 错误面板。文案与 /quota/policies 共用同一组键。
  if (loading) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        <PageHeader title={t('quota.title')} description={t('quota.desc')} />
        <div className="h-40 animate-pulse rounded-[20px] bg-muted" />
      </div>
    );
  }

  if (!isAdmin) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        <PageHeader
          title={t('quota.title')}
          description={t('quota.desc')}
          actions={<div className="flex items-center gap-2">{policiesEntry}</div>}
        />
        <EmptyState
          icon={Lock}
          title={t('quotaPolicy.adminOnly')}
          description={t('quotaPolicy.adminOnlyDesc')}
          className="flex flex-col items-center justify-center py-20 text-center"
        />
      </div>
    );
  }

  return <AdminQuotaPage />;
}

function AdminQuotaPage() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [tab, setTab] = useState<TabKey>('groups');

  const [groups, setGroups] = useState<Sub2apiGroup[]>([]);
  const [accounts, setAccounts] = useState<Sub2apiAccount[]>([]);
  const [ledger, setLedger] = useState<Sub2apiLedgerEntry[]>([]);

  const [loadingGroups, setLoadingGroups] = useState(true);
  const [loadingAccounts, setLoadingAccounts] = useState(false);
  const [loadingLedger, setLoadingLedger] = useState(true);
  const [error, setError] = useState('');

  const [selectedGroupId, setSelectedGroupId] = useState<string>('');
  const [ledgerGroupFilter, setLedgerGroupFilter] = useState<string>('all');
  const [ledgerLimit, setLedgerLimit] = useState(50);

  // 分组表单
  const [groupFormOpen, setGroupFormOpen] = useState(false);
  const [editingGroup, setEditingGroup] = useState<Sub2apiGroup | null>(null);
  const [gId, setGId] = useState('');
  const [gName, setGName] = useState('');
  const [gPlatform, setGPlatform] = useState('openai');
  const [gLimit, setGLimit] = useState(0);

  // 账号表单
  const [accountFormOpen, setAccountFormOpen] = useState(false);
  const [editingAccount, setEditingAccount] = useState<Sub2apiAccount | null>(null);
  const [aGroupId, setAGroupId] = useState('');
  const [aName, setAName] = useState('');
  const [aUpstream, setAUpstream] = useState('');
  const [aApiKey, setAApiKey] = useState('');
  const [aWeight, setAWeight] = useState(1);
  const [aLimit, setALimit] = useState(0);

  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);

  // 不把 selectedGroupId 放进依赖：loadGroups 内部会设置它，放进来会导致
  // 「请求回来→设 selectedGroupId→重建 loadGroups→effect 重跑→再请求一次」的重复请求。
  const loadGroups = useCallback(async () => {
    setLoadingGroups(true);
    setError('');
    try {
      const data = await sub2Api.groups();
      setGroups(data);
      setSelectedGroupId((cur) => (cur || !data.length ? cur : data[0].id));
    } catch {
      setError(t('quota.loadFailed'));
      setGroups([]);
    } finally {
      setLoadingGroups(false);
    }
  }, [t]);

  const loadAccounts = useCallback(async (groupId: string) => {
    if (!groupId) {
      setAccounts([]);
      return;
    }
    setLoadingAccounts(true);
    try {
      setAccounts(await sub2Api.accounts(groupId));
    } catch (e) {
      notify.err(errText(e));
      setAccounts([]);
    } finally {
      setLoadingAccounts(false);
    }
  }, []);

  const loadLedger = useCallback(async () => {
    setLoadingLedger(true);
    try {
      const gid = ledgerGroupFilter === 'all' ? undefined : ledgerGroupFilter;
      setLedger(await sub2Api.ledger(gid, ledgerLimit));
    } catch (e) {
      notify.err(errText(e));
      setLedger([]);
    } finally {
      setLoadingLedger(false);
    }
  }, [ledgerGroupFilter, ledgerLimit]);

  useEffect(() => {
    loadGroups();
  }, [loadGroups]);

  useEffect(() => {
    if (tab === 'accounts') loadAccounts(selectedGroupId);
  }, [tab, selectedGroupId, loadAccounts]);

  useEffect(() => {
    if (tab === 'ledger') loadLedger();
  }, [tab, loadLedger]);

  function openGroupCreate() {
    setEditingGroup(null);
    setGId('');
    setGName('');
    setGPlatform('openai');
    setGLimit(0);
    setGroupFormOpen(true);
  }

  function openGroupEdit(g: Sub2apiGroup) {
    setEditingGroup(g);
    setGId(g.id);
    setGName(g.name);
    setGPlatform(g.platform);
    setGLimit(g.quota_limit);
    setGroupFormOpen(true);
  }

  async function submitGroup() {
    if (submitting.current) return;
    if (!editingGroup && !gId.trim()) {
      notify.err(t('quota.groupIdRequired'));
      return;
    }
    submitting.current = true;
    setBusy(true);
    try {
      if (editingGroup) {
        await sub2Api.patchGroup(editingGroup.id, {
          name: gName,
          platform: gPlatform,
          quota_limit: gLimit,
        });
        notify.ok(t('quota.groupUpdated'));
      } else {
        await sub2Api.createGroup({
          id: gId.trim(),
          name: gName,
          platform: gPlatform,
          quota_limit: gLimit,
        });
        notify.ok(t('quota.groupCreated'));
      }
      setGroupFormOpen(false);
      await loadGroups();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
      submitting.current = false;
    }
  }

  async function deleteGroup(g: Sub2apiGroup) {
    try {
      await sub2Api.removeGroup(g.id);
      notify.ok(t('quota.groupDeleted'));
      if (selectedGroupId === g.id) setSelectedGroupId('');
      await loadGroups();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  async function toggleGroup(g: Sub2apiGroup) {
    try {
      const updated = await sub2Api.patchGroup(g.id, {enabled: !g.enabled});
      setGroups((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
    } catch (e) {
      notify.err(errText(e));
    }
  }

  function openAccountCreate(groupId?: string) {
    setEditingAccount(null);
    setAGroupId(groupId || selectedGroupId || groups[0]?.id || '');
    setAName('');
    setAUpstream('');
    setAApiKey('');
    setAWeight(1);
    setALimit(0);
    setAccountFormOpen(true);
  }

  function openAccountEdit(a: Sub2apiAccount) {
    setEditingAccount(a);
    setAGroupId(a.group_id);
    setAName(a.name);
    setAUpstream(a.upstream_base);
    setAApiKey('');
    setAWeight(a.weight);
    setALimit(a.quota_limit);
    setAccountFormOpen(true);
  }

  async function submitAccount() {
    if (submitting.current) return;
    if (!aGroupId) {
      notify.err(t('quota.selectGroup'));
      return;
    }
    submitting.current = true;
    setBusy(true);
    try {
      if (editingAccount) {
        await sub2Api.patchAccount(editingAccount.id, {
          name: aName,
          upstream_base: aUpstream,
          api_key: aApiKey || undefined,
          weight: aWeight,
          quota_limit: aLimit,
        });
        notify.ok(t('quota.accountUpdated'));
      } else {
        await sub2Api.createAccount({
          group_id: aGroupId,
          name: aName,
          upstream_base: aUpstream,
          api_key: aApiKey,
          weight: aWeight,
          quota_limit: aLimit,
        });
        notify.ok(t('quota.accountCreated'));
      }
      setAccountFormOpen(false);
      await loadAccounts(selectedGroupId);
      await loadGroups();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
      submitting.current = false;
    }
  }

  async function deleteAccount(a: Sub2apiAccount) {
    try {
      await sub2Api.removeAccount(a.id);
      notify.ok(t('quota.accountDeleted'));
      await loadAccounts(selectedGroupId);
      await loadGroups();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  async function resetAccount(a: Sub2apiAccount) {
    try {
      const updated = await sub2Api.resetAccount(a.id);
      setAccounts((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
      notify.ok(t('quota.accountReset'));
    } catch (e) {
      notify.err(errText(e));
    }
  }

  async function clearCooldown(a: Sub2apiAccount) {
    try {
      const updated = await sub2Api.patchAccount(a.id, {clear_cooldown: true});
      setAccounts((prev) => prev.map((x) => (x.id === updated.id ? updated : x)));
      notify.ok(t('quota.cooldownCleared'));
    } catch (e) {
      notify.err(errText(e));
    }
  }

  const accountNameMap = useMemo(() => {
    const m = new Map<number, string>();
    for (const a of accounts) m.set(a.id, a.name);
    return m;
  }, [accounts]);

  const now = Math.floor(Date.now() / 1000);

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title={t('quota.title')}
        description={t('quota.desc')}
        actions={
          <div className="flex items-center gap-2">
            {isAdmin && tab === 'groups' && (
              <Button onClick={openGroupCreate} className="rounded-full">
                <Plus className="mr-1.5 h-4 w-4" />
                {t('quota.addGroup')}
              </Button>
            )}
            <Button asChild variant="outline" className="rounded-full">
              <Link href="/quota/policies">{t('quotaPolicy.title')}</Link>
            </Button>
          </div>
        }
      />

      <Tabs value={tab} onValueChange={(v) => setTab(v as TabKey)}>
        <TabsList className="w-max">
          <TabsTrigger value="groups">
            <Layers3 className="mr-1.5 h-3.5 w-3.5" />
            {t('quota.tabGroups')}
          </TabsTrigger>
          <TabsTrigger value="accounts">
            <Database className="mr-1.5 h-3.5 w-3.5" />
            {t('quota.tabAccounts')}
          </TabsTrigger>
          <TabsTrigger value="ledger">
            <ScrollText className="mr-1.5 h-3.5 w-3.5" />
            {t('quota.tabLedger')}
          </TabsTrigger>
        </TabsList>

        {/* ── 分组 ── */}
        <TabsContent value="groups" className="mt-3">
          {error ? (
            <EmptyState
              icon={AlertCircle}
              title={t('quota.loadFailed')}
              description={error}
              className="flex flex-col items-center justify-center py-16 text-center"
            />
          ) : loadingGroups ? (
            <GroupSkeleton />
          ) : groups.length === 0 ? (
            <EmptyState
              icon={Layers3}
              title={t('quota.emptyGroups')}
              description={t('quota.emptyGroupsHint')}
              className="flex flex-col items-center justify-center py-16 text-center"
            />
          ) : (
            <section className="overflow-hidden rounded-[20px] bg-muted">
              <Table>
                <TableHeader>
                  <TableRow className="border-b border-border/60 hover:bg-transparent">
                    <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('quota.colName')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('quota.colPlatform')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('quota.colLimit')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('quota.colAccounts')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('quota.colStatus')}</TableHead>
                    <TableHead className="pr-4 text-right text-[11px] text-muted-foreground" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {groups.map((g) => (
                    <TableRow key={g.id} className="border-b border-border/40">
                      <TableCell className="pl-4">
                        <div className="font-medium">{g.name || g.id}</div>
                        <div className="text-[11px] text-muted-foreground">{g.id}</div>
                      </TableCell>
                      <TableCell>
                        <Badge variant="secondary" className="rounded-full">{g.platform}</Badge>
                      </TableCell>
                      <TableCell>
                        <div className="tabular-nums">{quotaText(g.quota_used, g.quota_limit)}</div>
                        {g.quota_limit > 0 && (
                          <div className="mt-1 h-1 w-24 overflow-hidden rounded-full bg-border">
                            <div
                              className="h-full rounded-full bg-foreground/70"
                              style={{width: `${Math.min(100, (g.quota_used / g.quota_limit) * 100)}%`}}
                            />
                          </div>
                        )}
                      </TableCell>
                      <TableCell>
                        <span className="tabular-nums">{g.accounts}</span>
                        <span className="text-muted-foreground"> / {g.active_accounts} {t('quota.colActive')}</span>
                      </TableCell>
                      <TableCell>{statusBadge(g.enabled, t)}</TableCell>
                      <TableCell className="pr-4">
                        <div className="flex items-center justify-end gap-1">
                          {isAdmin && (
                            <>
                              <Button variant="ghost" size="icon" className="h-8 w-8 rounded-md" onClick={() => toggleGroup(g)} title={g.enabled ? t('quota.disabled') : t('quota.enabled')}>
                                <Snowflake className="h-4 w-4" />
                              </Button>
                              <Button variant="ghost" size="icon" className="h-8 w-8 rounded-md" onClick={() => openGroupEdit(g)} title={t('quota.editGroup')}>
                                <Pencil className="h-4 w-4" />
                              </Button>
                              <ConfirmDialog
                                title={t('quota.deleteGroupTitle', {name: g.name || g.id})}
                                description={t('quota.deleteGroupDesc')}
                                confirmText={t('quota.delete')}
                                destructive
                                onConfirm={() => deleteGroup(g)}
                                trigger={
                                  <Button variant="ghost" size="icon" className="h-8 w-8 rounded-md text-destructive" title={t('quota.delete')}>
                                    <Trash2 className="h-4 w-4" />
                                  </Button>
                                }
                              />
                            </>
                          )}
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </section>
          )}
        </TabsContent>

        {/* ── 账号池（两级：左分组列表 + 右账号表）── */}
        <TabsContent value="accounts" className="mt-3">
          <div className="flex flex-col gap-3 lg:flex-row">
            <aside className="lg:w-56 shrink-0">
              <section className="overflow-hidden rounded-[20px] bg-muted">
                <div className="px-4 py-3 text-[11px] font-medium text-muted-foreground">
                  {t('quota.tabGroups')}
                </div>
                <ScrollArea className="h-[60vh] lg:h-[480px]">
                  <div className="px-2 pb-2">
                    {groups.length === 0 ? (
                      <div className="px-2 py-6 text-center text-xs text-muted-foreground">
                        {t('quota.emptyGroups')}
                      </div>
                    ) : (
                      groups.map((g) => (
                        <button
                          key={g.id}
                          onClick={() => setSelectedGroupId(g.id)}
                          className={`mb-1 flex w-full items-center justify-between rounded-lg px-3 py-2 text-left text-sm transition-colors ${
                            selectedGroupId === g.id ? 'bg-foreground/10' : 'hover:bg-foreground/5'
                          }`}
                        >
                          <span className="truncate">{g.name || g.id}</span>
                          <Badge variant="secondary" className="ml-2 shrink-0 rounded-full text-[10px]">
                            {g.active_accounts}/{g.accounts}
                          </Badge>
                        </button>
                      ))
                    )}
                  </div>
                </ScrollArea>
              </section>
            </aside>

            <div className="min-w-0 flex-1">
              {selectedGroupId ? (
                <>
                  <div className="mb-3 flex items-center justify-between">
                    <div className="text-sm text-muted-foreground">
                      {groups.find((g) => g.id === selectedGroupId)?.name || selectedGroupId}
                    </div>
                    {isAdmin && (
                      <Button onClick={() => openAccountCreate(selectedGroupId)} className="rounded-full" size="sm">
                        <Plus className="mr-1.5 h-3.5 w-3.5" />
                        {t('quota.addAccount')}
                      </Button>
                    )}
                  </div>
                  {loadingAccounts ? (
                    <AccountSkeleton />
                  ) : accounts.length === 0 ? (
                    <EmptyState
                      icon={Database}
                      title={t('quota.emptyAccounts')}
                      description={t('quota.emptyAccountsHint')}
                      className="flex flex-col items-center justify-center py-16 text-center"
                    />
                  ) : (
                    <section className="overflow-hidden rounded-[20px] bg-muted">
                      <Table>
                        <TableHeader>
                          <TableRow className="border-b border-border/60 hover:bg-transparent">
                            <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('quota.colName')}</TableHead>
                            <TableHead className="text-[11px] text-muted-foreground">{t('quota.colUpstream')}</TableHead>
                            <TableHead className="text-[11px] text-muted-foreground">{t('quota.colWeight')}</TableHead>
                            <TableHead className="text-[11px] text-muted-foreground">{t('quota.colLimit')}</TableHead>
                            <TableHead className="text-[11px] text-muted-foreground">{t('quota.colStatus')}</TableHead>
                            <TableHead className="pr-4 text-right text-[11px] text-muted-foreground" />
                          </TableRow>
                        </TableHeader>
                        <TableBody>
                          {accounts.map((a) => {
                            const cooling = a.cooldown_until > now;
                            return (
                              <TableRow key={a.id} className="border-b border-border/40">
                                <TableCell className="pl-4">
                                  <div className="font-medium">{a.name || `#${a.id}`}</div>
                                  <div className="text-[11px] text-muted-foreground">
                                    {a.has_api_key ? t('quota.hasKey') : t('quota.noKey')}
                                  </div>
                                </TableCell>
                                <TableCell className="max-w-[200px] truncate text-muted-foreground" title={a.upstream_base}>
                                  {a.upstream_base || '—'}
                                </TableCell>
                                <TableCell className="tabular-nums">{a.weight}</TableCell>
                                <TableCell>
                                  <div className="tabular-nums">{quotaText(a.quota_used, a.quota_limit)}</div>
                                </TableCell>
                                <TableCell>
                                  {!a.enabled ? statusBadge(false, t) :
                                   cooling ? (
                                    <Badge variant="secondary" className="rounded-full text-amber-600 dark:text-amber-400">
                                      {t('quota.coolingShort')}
                                    </Badge>
                                  ) : statusBadge(true, t)}
                                </TableCell>
                                <TableCell className="pr-4">
                                  <div className="flex items-center justify-end gap-1">
                                    {isAdmin && (
                                      <>
                                        {cooling && (
                                          <Button variant="ghost" size="icon" className="h-8 w-8 rounded-md" onClick={() => clearCooldown(a)} title={t('quota.clearCooldown')}>
                                            <Snowflake className="h-4 w-4" />
                                          </Button>
                                        )}
                                        <ConfirmDialog
                                          title={t('quota.resetAccountTitle', {name: a.name || `#${a.id}`})}
                                          description={t('quota.resetAccountDesc')}
                                          confirmText={t('quota.reset')}
                                          onConfirm={() => resetAccount(a)}
                                          trigger={
                                            <Button variant="ghost" size="icon" className="h-8 w-8 rounded-md" title={t('quota.reset')}>
                                              <RefreshCcw className="h-4 w-4" />
                                            </Button>
                                          }
                                        />
                                        <Button variant="ghost" size="icon" className="h-8 w-8 rounded-md" onClick={() => openAccountEdit(a)} title={t('quota.editAccount')}>
                                          <Pencil className="h-4 w-4" />
                                        </Button>
                                        <ConfirmDialog
                                          title={t('quota.deleteAccountTitle', {name: a.name || `#${a.id}`})}
                                          description={t('quota.deleteAccountDesc')}
                                          confirmText={t('quota.delete')}
                                          destructive
                                          onConfirm={() => deleteAccount(a)}
                                          trigger={
                                            <Button variant="ghost" size="icon" className="h-8 w-8 rounded-md text-destructive" title={t('quota.delete')}>
                                              <Trash2 className="h-4 w-4" />
                                            </Button>
                                          }
                                        />
                                      </>
                                    )}
                                  </div>
                                </TableCell>
                              </TableRow>
                            );
                          })}
                        </TableBody>
                      </Table>
                    </section>
                  )}
                </>
              ) : (
                <EmptyState
                  icon={Layers3}
                  title={t('quota.selectGroup')}
                  description=""
                  className="flex flex-col items-center justify-center py-16 text-center"
                />
              )}
            </div>
          </div>
        </TabsContent>

        {/* ── 流水 ── */}
        <TabsContent value="ledger" className="mt-3 space-y-3">
          <section className="rounded-[20px] bg-muted p-4">
            <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('quota.colGroup')}</Label>
                <Select value={ledgerGroupFilter} onValueChange={setLedgerGroupFilter}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">{t('quota.filterAll')}</SelectItem>
                    {groups.map((g) => (
                      <SelectItem key={g.id} value={g.id}>{g.name || g.id}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label className="text-[11px] text-muted-foreground">{t('quota.colLimit')}</Label>
                <Select value={String(ledgerLimit)} onValueChange={(v) => setLedgerLimit(Number(v))}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="20">20</SelectItem>
                    <SelectItem value="50">50</SelectItem>
                    <SelectItem value="100">100</SelectItem>
                    <SelectItem value="200">200</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
          </section>

          {loadingLedger ? (
            <LedgerSkeleton />
          ) : ledger.length === 0 ? (
            <EmptyState
              icon={ScrollText}
              title={t('quota.emptyLedger')}
              description={t('quota.emptyLedgerHint')}
              className="flex flex-col items-center justify-center py-16 text-center"
            />
          ) : (
            <section className="overflow-hidden rounded-[20px] bg-muted">
              <Table>
                <TableHeader>
                  <TableRow className="border-b border-border/60 hover:bg-transparent">
                    <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('quota.colTime')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('quota.colGroup')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('quota.colAccount')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('quota.colModel')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('quota.colCost')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('quota.colHttpStatus')}</TableHead>
                    <TableHead className="pr-4 text-[11px] text-muted-foreground">{t('quota.colReason')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {ledger.map((l) => (
                    <TableRow key={l.id} className="border-b border-border/40">
                      <TableCell className="pl-4 text-muted-foreground">{fmtDateTime(l.ts)}</TableCell>
                      <TableCell>{l.group_id}</TableCell>
                      <TableCell>
                        {l.account_id ? (accountNameMap.get(l.account_id) || `#${l.account_id}`) : '—'}
                      </TableCell>
                      <TableCell className="max-w-[160px] truncate" title={l.model}>{l.model || '—'}</TableCell>
                      <TableCell className="tabular-nums">{fmtNumber(l.cost)}</TableCell>
                      <TableCell>
                        <Badge
                          variant="secondary"
                          className={`rounded-full ${l.status >= 400 ? 'text-red-600 dark:text-red-400' : 'text-emerald-600 dark:text-emerald-400'}`}
                        >
                          {l.status}
                        </Badge>
                      </TableCell>
                      <TableCell className="pr-4 max-w-[180px] truncate text-muted-foreground" title={l.reason || l.note}>
                        {l.reason || l.note || '—'}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </section>
          )}
        </TabsContent>
      </Tabs>

      {/* ── 分组表单 ── */}
      <Dialog open={groupFormOpen} onOpenChange={setGroupFormOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{editingGroup ? t('quota.editGroup') : t('quota.addGroup')}</DialogTitle>
            <DialogDescription />
          </DialogHeader>
          <DialogBody>
            <div className="space-y-4 px-5 pb-4">
              <div className="space-y-1.5">
                <Label>{t('quota.colId')}</Label>
                <Input
                  value={gId}
                  onChange={(e) => setGId(e.target.value)}
                  placeholder={t('quota.groupIdPlaceholder')}
                  disabled={!!editingGroup}
                />
              </div>
              <div className="space-y-1.5">
                <Label>{t('quota.colName')}</Label>
                <Input value={gName} onChange={(e) => setGName(e.target.value)} placeholder={t('quota.groupNamePlaceholder')} />
              </div>
              <div className="space-y-1.5">
                <Label>{t('quota.colPlatform')}</Label>
                <Input value={gPlatform} onChange={(e) => setGPlatform(e.target.value)} />
              </div>
              <div className="space-y-1.5">
                <Label>{t('quota.colLimit')}</Label>
                <Input
                  type="number"
                  min={0}
                  value={gLimit}
                  onChange={(e) => setGLimit(Number(e.target.value))}
                  placeholder={t('quota.limitHint')}
                />
                <p className="text-[11px] text-muted-foreground">{t('quota.limitHint')}</p>
              </div>
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setGroupFormOpen(false)}>{t('quota.cancel')}</Button>
            <Button onClick={submitGroup} disabled={busy}>{t('quota.save')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ── 账号表单 ── */}
      <Dialog open={accountFormOpen} onOpenChange={setAccountFormOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{editingAccount ? t('quota.editAccount') : t('quota.addAccount')}</DialogTitle>
            <DialogDescription />
          </DialogHeader>
          <DialogBody>
            <div className="space-y-4 px-5 pb-4">
              <div className="space-y-1.5">
                <Label>{t('quota.colGroup')}</Label>
                <Select value={aGroupId} onValueChange={setAGroupId} disabled={!!editingAccount}>
                  <SelectTrigger>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {groups.map((g) => (
                      <SelectItem key={g.id} value={g.id}>{g.name || g.id}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label>{t('quota.colName')}</Label>
                <Input value={aName} onChange={(e) => setAName(e.target.value)} placeholder={t('quota.accountNamePlaceholder')} />
              </div>
              <div className="space-y-1.5">
                <Label>{t('quota.colUpstream')}</Label>
                <Input value={aUpstream} onChange={(e) => setAUpstream(e.target.value)} placeholder={t('quota.upstreamPlaceholder')} />
              </div>
              <div className="space-y-1.5">
                <Label>{t('quota.colApiKey')}</Label>
                <Input
                  value={aApiKey}
                  onChange={(e) => setAApiKey(e.target.value)}
                  placeholder={editingAccount ? '••••••••' : t('quota.apiKeyPlaceholder')}
                />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label>{t('quota.colWeight')}</Label>
                  <Input type="number" min={1} value={aWeight} onChange={(e) => setAWeight(Number(e.target.value))} />
                  <p className="text-[11px] text-muted-foreground">{t('quota.weightHint')}</p>
                </div>
                <div className="space-y-1.5">
                  <Label>{t('quota.colLimit')}</Label>
                  <Input type="number" min={0} value={aLimit} onChange={(e) => setALimit(Number(e.target.value))} />
                  <p className="text-[11px] text-muted-foreground">{t('quota.limitHint')}</p>
                </div>
              </div>
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setAccountFormOpen(false)}>{t('quota.cancel')}</Button>
            <Button onClick={submitAccount} disabled={busy}>{t('quota.save')}</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/* ── 骨架屏（只用基准组件，不新增样式）──────────────── */

function GroupSkeleton() {
  return (
    <section className="overflow-hidden rounded-[20px] bg-muted">
      <Table>
        <TableHeader>
          <TableRow className="border-b border-border/60 hover:bg-transparent">
            {Array.from({length: 6}).map((_, i) => (
              <TableHead key={i} className="text-[11px] text-muted-foreground">
                <div className="h-3 w-16 animate-pulse rounded bg-foreground/10" />
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {Array.from({length: 4}).map((_, r) => (
            <TableRow key={r} className="border-b border-border/40">
              {Array.from({length: 6}).map((_, c) => (
                <TableCell key={c}>
                  <div className="h-3 w-20 animate-pulse rounded bg-foreground/10" />
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  );
}

function AccountSkeleton() {
  return (
    <section className="overflow-hidden rounded-[20px] bg-muted">
      <Table>
        <TableHeader>
          <TableRow className="border-b border-border/60 hover:bg-transparent">
            {Array.from({length: 6}).map((_, i) => (
              <TableHead key={i} className="text-[11px] text-muted-foreground">
                <div className="h-3 w-16 animate-pulse rounded bg-foreground/10" />
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {Array.from({length: 3}).map((_, r) => (
            <TableRow key={r} className="border-b border-border/40">
              {Array.from({length: 6}).map((_, c) => (
                <TableCell key={c}>
                  <div className="h-3 w-20 animate-pulse rounded bg-foreground/10" />
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  );
}

function LedgerSkeleton() {
  return (
    <section className="overflow-hidden rounded-[20px] bg-muted">
      <Table>
        <TableHeader>
          <TableRow className="border-b border-border/60 hover:bg-transparent">
            {Array.from({length: 7}).map((_, i) => (
              <TableHead key={i} className="text-[11px] text-muted-foreground">
                <div className="h-3 w-16 animate-pulse rounded bg-foreground/10" />
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {Array.from({length: 5}).map((_, r) => (
            <TableRow key={r} className="border-b border-border/40">
              {Array.from({length: 7}).map((_, c) => (
                <TableCell key={c}>
                  <div className="h-3 w-20 animate-pulse rounded bg-foreground/10" />
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </section>
  );
}
