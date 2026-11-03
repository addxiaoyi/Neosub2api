'use client';

import {useCallback, useEffect, useMemo, useState} from 'react';
import {
  AlertCircle,
  AlertTriangle,
  Coins,
  Gauge,
  Lock,
  Pencil,
  Plus,
  Trash2,
} from 'lucide-react';
import {keyApi, errText} from '@/lib/api';
import {quotaApi, type QuotaAlert, type QuotaPolicy, type QuotaStatusItem} from '@/lib/quota-api';
import type {ApiKey} from '@/lib/types';
import {fmtDateTime, fmtNumber, fmtRemain, fmtMicroUsd, fmtUsd, usdToMicro} from '@/lib/format';
import {notify} from '@/lib/toast';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Switch} from '@/components/ui/switch';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
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

type FormState = {
  keyId: string;
  window: 'day' | 'month';
  provider: string;
  model: string;
  amount: string;
  requests: string;
  thresholds: string;
  enabled: boolean;
};

const EMPTY_FORM: FormState = {
  keyId: '',
  window: 'day',
  provider: '',
  model: '',
  amount: '',
  requests: '',
  thresholds: '80,100',
  enabled: true,
};

export default function QuotaPoliciesPage() {
  const t = useT();
  const {isAdmin, loading} = useAuth();

  // 读取接口已收紧为 admin-only：非管理员不请求，直接给说明，避免三块 403 报错
  if (loading) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        <PageHeader title={t('quotaPolicy.title')} description={t('quotaPolicy.desc')} />
        <div className="h-40 animate-pulse rounded-[20px] bg-muted" />
      </div>
    );
  }

  if (!isAdmin) {
    return (
      <div className="flex flex-col gap-4 md:gap-6">
        <PageHeader title={t('quotaPolicy.title')} description={t('quotaPolicy.desc')} />
        <EmptyState
          icon={Lock}
          title={t('quotaPolicy.adminOnly')}
          description={t('quotaPolicy.adminOnlyDesc')}
          className="flex flex-col items-center justify-center py-20 text-center"
        />
      </div>
    );
  }

  return <AdminQuotaPolicies />;
}

function AdminQuotaPolicies() {
  const t = useT();
  const {isAdmin} = useAuth();

  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [keysError, setKeysError] = useState('');
  const [policies, setPolicies] = useState<QuotaPolicy[]>([]);
  const [loadingPolicies, setLoadingPolicies] = useState(true);
  const [policiesError, setPoliciesError] = useState('');
  const [reloadToken, setReloadToken] = useState(0);

  const [policyKey, setPolicyKey] = useState('all');

  const [usageKey, setUsageKey] = useState('');
  const [statusItems, setStatusItems] = useState<QuotaStatusItem[]>([]);
  const [loadingStatus, setLoadingStatus] = useState(false);
  const [statusError, setStatusError] = useState('');

  const [alertKey, setAlertKey] = useState('all');
  const [alertPolicy, setAlertPolicy] = useState('all');
  const [alertLimit, setAlertLimit] = useState(50);
  const [alerts, setAlerts] = useState<QuotaAlert[]>([]);
  const [loadingAlerts, setLoadingAlerts] = useState(true);
  const [alertsError, setAlertsError] = useState('');

  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<QuotaPolicy | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);

  const keyName = useCallback(
    (id: number | null | undefined) => {
      if (!id) return '—';
      return keys.find((k) => k.id === id)?.name || `#${id}`;
    },
    [keys],
  );

  const loadKeys = useCallback(async () => {
    try {
      const data = await keyApi.list();
      setKeys(data);
      setKeysError('');
      setUsageKey((cur) => (cur || !data.length ? cur : String(data[0].id)));
    } catch (e) {
      setKeysError(errText(e));
      notify.err(errText(e));
    }
  }, []);

  const loadPolicies = useCallback(async () => {
    setLoadingPolicies(true);
    setPoliciesError('');
    try {
      setPolicies(await quotaApi.policies());
    } catch (e) {
      setPoliciesError(errText(e));
      setPolicies([]);
    } finally {
      setLoadingPolicies(false);
    }
  }, []);

  useEffect(() => {
    loadKeys();
  }, [loadKeys]);

  useEffect(() => {
    loadPolicies();
  }, [loadPolicies, reloadToken]);

  useEffect(() => {
    if (!usageKey) {
      setStatusItems([]);
      return;
    }
    let alive = true;
    setLoadingStatus(true);
    setStatusError('');
    quotaApi
      .status(Number(usageKey))
      .then((data) => {
        if (alive) setStatusItems(data.items);
      })
      .catch((e) => {
        if (alive) {
          setStatusError(errText(e));
          setStatusItems([]);
        }
      })
      .finally(() => {
        if (alive) setLoadingStatus(false);
      });
    return () => {
      alive = false;
    };
  }, [usageKey, reloadToken]);

  useEffect(() => {
    let alive = true;
    setLoadingAlerts(true);
    setAlertsError('');
    quotaApi
      .alerts({
        key_id: alertKey === 'all' ? undefined : Number(alertKey),
        policy_id: alertPolicy === 'all' ? undefined : Number(alertPolicy),
        limit: alertLimit,
      })
      .then((data) => {
        if (alive) setAlerts(data.items);
      })
      .catch((e) => {
        if (alive) {
          setAlertsError(errText(e));
          setAlerts([]);
        }
      })
      .finally(() => {
        if (alive) setLoadingAlerts(false);
      });
    return () => {
      alive = false;
    };
  }, [alertKey, alertPolicy, alertLimit, reloadToken]);

  const visiblePolicies = useMemo(
    () => (policyKey === 'all' ? policies : policies.filter((p) => p.key_id === Number(policyKey))),
    [policies, policyKey],
  );

  const alertPolicyOptions = useMemo(
    () => (alertKey === 'all' ? policies : policies.filter((p) => p.key_id === Number(alertKey))),
    [policies, alertKey],
  );

  function openCreate() {
    setEditing(null);
    setForm({...EMPTY_FORM, keyId: usageKey || String(keys[0]?.id ?? '')});
    setFormOpen(true);
  }

  function openEdit(policy: QuotaPolicy) {
    setEditing(policy);
    setForm({
      keyId: String(policy.key_id),
      window: policy.window,
      provider: policy.provider,
      model: policy.model,
      amount: String(policy.limit_usd),
      requests: String(policy.limit_requests),
      thresholds: policy.thresholds.join(','),
      enabled: policy.enabled,
    });
    setFormOpen(true);
  }

  async function submit() {
    if (saving) return;

    const keyId = Number(form.keyId);
    if (!keyId) {
      notify.err(t('quotaPolicy.keyRequired'));
      return;
    }
    const amount = form.amount.trim() === '' ? 0 : Number(form.amount);
    if (!Number.isFinite(amount) || amount < 0) {
      notify.err(t('quotaPolicy.invalidAmount'));
      return;
    }
    const requests = form.requests.trim() === '' ? 0 : Number(form.requests);
    if (!Number.isInteger(requests) || requests < 0) {
      notify.err(t('quotaPolicy.invalidRequests'));
      return;
    }
    const rawThresholds = form.thresholds
      .split(',')
      .map((part) => part.trim())
      .filter(Boolean);
    const thresholds = rawThresholds.map(Number);
    if (thresholds.some((n) => !Number.isInteger(n) || n < 1 || n > 1000)) {
      notify.err(t('quotaPolicy.invalidThresholds'));
      return;
    }
    if (usdToMicro(amount) === 0 && requests === 0) {
      notify.err(t('quotaPolicy.bothZero'));
      return;
    }

    setSaving(true);
    try {
      await quotaApi.savePolicy({
        key_id: keyId,
        window: form.window,
        provider: form.provider.trim(),
        model: form.model.trim(),
        limit_micro: usdToMicro(amount),
        limit_requests: requests,
        thresholds,
        enabled: form.enabled,
      });
      notify.ok(t('quotaPolicy.saved'));
      setFormOpen(false);
      setReloadToken((n) => n + 1);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  }

  async function remove(policy: QuotaPolicy) {
    try {
      await quotaApi.removePolicy(policy.id);
      notify.ok(t('quotaPolicy.deleted'));
      if (alertPolicy === String(policy.id)) setAlertPolicy('all');
      setReloadToken((n) => n + 1);
    } catch (e) {
      notify.err(errText(e));
    }
  }

  const scopeText = (p: QuotaPolicy) => {
    if (!p.provider && !p.model) return t('quota.unlimited');
    if (!p.model) return p.provider;
    return `${p.provider || '*'} / ${p.model}`;
  };

  const windowText = (w: QuotaPolicy['window']) =>
    w === 'month' ? t('quotaPolicy.windowMonth') : t('quotaPolicy.windowDay');

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title={t('quotaPolicy.title')}
        description={t('quotaPolicy.desc')}
        actions={
          isAdmin ? (
            <Button size="sm" className="rounded-full" onClick={openCreate}>
              <Plus />
              {t('quotaPolicy.add')}
            </Button>
          ) : undefined
        }
      />

      {!isAdmin && (
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Lock className="h-3.5 w-3.5 shrink-0" />
          {t('quotaPolicy.readonly')}
        </p>
      )}

      {/* ── 策略列表 ── */}
      <section className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Coins className="h-4 w-4 text-muted-foreground" />
            {t('quotaPolicy.listTitle')}
          </div>
          <div className="w-full sm:w-56">
            <Select value={policyKey} onValueChange={setPolicyKey}>
              <SelectTrigger className="h-9">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="all">{t('quotaPolicy.allKeys')}</SelectItem>
                {keys.map((k) => (
                  <SelectItem key={k.id} value={String(k.id)}>
                    {k.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        {policiesError ? (
          <EmptyState
            icon={AlertCircle}
            title={t('quotaPolicy.loadFailed')}
            description={policiesError}
            className="flex flex-col items-center justify-center py-16 text-center"
          >
            <Button variant="outline" className="mt-4 rounded-full" onClick={() => setReloadToken((n) => n + 1)}>
              {t('common.refresh')}
            </Button>
          </EmptyState>
        ) : loadingPolicies ? (
          <TableSkeleton cols={8} />
        ) : visiblePolicies.length === 0 ? (
          <EmptyState
            icon={Coins}
            title={t('quotaPolicy.emptyPolicies')}
            description={isAdmin ? t('quotaPolicy.emptyPoliciesDesc') : t('quotaPolicy.emptyPoliciesReadonly')}
            className="flex flex-col items-center justify-center py-16 text-center"
          />
        ) : (
          <section className="overflow-x-auto rounded-[20px] bg-muted">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-border/60 hover:bg-transparent">
                  <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('quotaPolicy.colKey')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('quotaPolicy.colScope')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('quotaPolicy.colWindow')}</TableHead>
                  <TableHead className="text-right text-[11px] text-muted-foreground">{t('quotaPolicy.colAmount')}</TableHead>
                  <TableHead className="text-right text-[11px] text-muted-foreground">{t('quotaPolicy.colRequests')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('quotaPolicy.colThresholds')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('quotaPolicy.colStatus')}</TableHead>
                  {isAdmin && <TableHead className="pr-4 text-right text-[11px] text-muted-foreground" />}
                </TableRow>
              </TableHeader>
              <TableBody>
                {visiblePolicies.map((p) => (
                  <TableRow key={p.id} className="border-b border-border/40">
                    <TableCell className="pl-4">
                      <div className="font-medium">{p.key_name || keyName(p.key_id)}</div>
                      <div className="text-[11px] text-muted-foreground">#{p.key_id}</div>
                    </TableCell>
                    <TableCell className="max-w-[220px] truncate text-xs" title={scopeText(p)}>
                      {scopeText(p)}
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary" className="rounded-full">{windowText(p.window)}</Badge>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {p.limit_micro ? fmtUsd(p.limit_usd) : t('quotaPolicy.notSet')}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {p.limit_requests ? fmtNumber(p.limit_requests) : t('quotaPolicy.notSet')}
                    </TableCell>
                    <TableCell>
                      {p.thresholds.length === 0 ? (
                        <span className="text-muted-foreground">—</span>
                      ) : (
                        <div className="flex flex-wrap gap-1">
                          {p.thresholds.map((percent) => (
                            <Badge key={percent} variant="secondary" className="rounded-full text-[10px]">
                              {percent}%
                            </Badge>
                          ))}
                        </div>
                      )}
                    </TableCell>
                    <TableCell>
                      {p.enabled ? (
                        <Badge variant="secondary" className="rounded-full text-emerald-600 dark:text-emerald-400">
                          {t('quota.enabled')}
                        </Badge>
                      ) : (
                        <Badge variant="secondary" className="rounded-full text-muted-foreground">
                          {t('quota.disabled')}
                        </Badge>
                      )}
                    </TableCell>
                    {isAdmin && (
                      <TableCell className="pr-4">
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-8 w-8 rounded-md"
                            title={t('quotaPolicy.edit')}
                            onClick={() => openEdit(p)}
                          >
                            <Pencil className="h-4 w-4" />
                          </Button>
                          <ConfirmDialog
                            title={t('quotaPolicy.deleteTitle', {name: p.key_name || `#${p.key_id}`})}
                            description={t('quotaPolicy.deleteDesc')}
                            confirmText={t('quotaPolicy.delete')}
                            destructive
                            onConfirm={() => remove(p)}
                            trigger={
                              <Button
                                variant="ghost"
                                size="icon"
                                className="h-8 w-8 rounded-md text-destructive"
                                title={t('quotaPolicy.delete')}
                              >
                                <Trash2 className="h-4 w-4" />
                              </Button>
                            }
                          />
                        </div>
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </section>
        )}
      </section>

      {/* ── 当前周期用量 ── */}
      <section className="rounded-[20px] border p-4">
        <div className="mb-3 flex flex-wrap items-end justify-between gap-3">
          <div className="space-y-1">
            <div className="flex items-center gap-2 text-sm font-medium">
              <Gauge className="h-4 w-4 text-muted-foreground" />
              {t('quotaPolicy.usageTitle')}
            </div>
            <p className="text-xs text-muted-foreground">{t('quotaPolicy.usageDesc')}</p>
          </div>
          <div className="w-full sm:w-56">
            <Label className="mb-1.5 block text-[11px] text-muted-foreground">{t('quotaPolicy.usageKey')}</Label>
            <Select value={usageKey} onValueChange={setUsageKey} disabled={!keys.length}>
              <SelectTrigger className="h-9">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {keys.map((k) => (
                  <SelectItem key={k.id} value={String(k.id)}>
                    {k.name} ({k.prefix}…)
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        {statusError ? (
          <EmptyState
            icon={AlertCircle}
            title={t('quotaPolicy.loadFailed')}
            description={statusError}
            className="flex flex-col items-center justify-center py-12 text-center"
          />
        ) : keysError && !keys.length ? (
          <EmptyState
            icon={AlertCircle}
            title={t('quotaPolicy.loadFailed')}
            description={keysError}
            className="flex flex-col items-center justify-center py-12 text-center"
          />
        ) : !keys.length ? (
          <EmptyState
            icon={Gauge}
            title={t('quotaPolicy.noKeys')}
            description={t('quotaPolicy.noKeysDesc')}
            className="flex flex-col items-center justify-center py-12 text-center"
          />
        ) : loadingStatus ? (
          <CardSkeleton />
        ) : statusItems.length === 0 ? (
          <EmptyState
            icon={Gauge}
            title={t('quotaPolicy.usageEmpty')}
            description={t('quotaPolicy.usageEmptyDesc')}
            className="flex flex-col items-center justify-center py-12 text-center"
          />
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {statusItems.map((item) => (
              <UsageCard key={item.policy.id} item={item} t={t} scopeText={scopeText} windowText={windowText} />
            ))}
          </div>
        )}
      </section>

      {/* ── 告警历史 ── */}
      <section className="flex flex-col gap-3">
        <div className="space-y-1">
          <div className="flex items-center gap-2 text-sm font-medium">
            <AlertTriangle className="h-4 w-4 text-muted-foreground" />
            {t('quotaPolicy.alertsTitle')}
          </div>
          <p className="text-xs text-muted-foreground">{t('quotaPolicy.alertsDesc')}</p>
        </div>

        <section className="rounded-[20px] bg-muted p-4">
          <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">{t('quotaPolicy.filterKey')}</Label>
              <Select
                value={alertKey}
                onValueChange={(value) => {
                  setAlertKey(value);
                  setAlertPolicy('all');
                }}
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{t('quotaPolicy.allKeys')}</SelectItem>
                  {keys.map((k) => (
                    <SelectItem key={k.id} value={String(k.id)}>
                      {k.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">{t('quotaPolicy.policyFilter')}</Label>
              <Select value={alertPolicy} onValueChange={setAlertPolicy}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="all">{t('common.all')}</SelectItem>
                  {alertPolicyOptions.map((p) => (
                    <SelectItem key={p.id} value={String(p.id)}>
                      {`${p.key_name || `#${p.key_id}`} · ${windowText(p.window)} · ${scopeText(p)}`}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">{t('quotaPolicy.alertsLimit')}</Label>
              <Select value={String(alertLimit)} onValueChange={(v) => setAlertLimit(Number(v))}>
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="20">20</SelectItem>
                  <SelectItem value="50">50</SelectItem>
                  <SelectItem value="100">100</SelectItem>
                </SelectContent>
              </Select>
            </div>
          </div>
        </section>

        {alertsError ? (
          <EmptyState
            icon={AlertCircle}
            title={t('quotaPolicy.loadFailed')}
            description={alertsError}
            className="flex flex-col items-center justify-center py-16 text-center"
          />
        ) : loadingAlerts ? (
          <TableSkeleton cols={6} />
        ) : alerts.length === 0 ? (
          <EmptyState
            icon={AlertTriangle}
            title={t('quotaPolicy.alertsEmpty')}
            description={t('quotaPolicy.alertsEmptyDesc')}
            className="flex flex-col items-center justify-center py-16 text-center"
          />
        ) : (
          <section className="overflow-x-auto rounded-[20px] bg-muted">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-border/60 hover:bg-transparent">
                  <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('quotaPolicy.colTriggeredAt')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('quotaPolicy.colKey')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('quotaPolicy.colScope')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('quotaPolicy.colThreshold')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('quotaPolicy.colMetric')}</TableHead>
                  <TableHead className="text-right text-[11px] text-muted-foreground">{t('quotaPolicy.colUsed')}</TableHead>
                  <TableHead className="pr-4 text-[11px] text-muted-foreground">{t('quotaPolicy.colPeriod')}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {alerts.map((alert) => {
                  const isAmount = alert.metric === 'micro';
                  return (
                    <TableRow key={alert.id} className="border-b border-border/40">
                      <TableCell className="pl-4 text-xs text-muted-foreground">{fmtDateTime(alert.triggered_at)}</TableCell>
                      <TableCell className="text-xs">{keyName(alert.key_id)}</TableCell>
                      <TableCell className="max-w-[200px] truncate text-xs" title={`${alert.provider} / ${alert.model}`}>
                        {alert.provider || alert.model
                          ? `${alert.provider || '*'} / ${alert.model || '*'}`
                          : t('quota.unlimited')}
                      </TableCell>
                      <TableCell>
                        <Badge variant="secondary" className="rounded-full">{alert.threshold_percent}%</Badge>
                      </TableCell>
                      <TableCell className="text-xs">
                        {isAmount ? t('quotaPolicy.metricMicro') : t('quotaPolicy.metricRequests')}
                      </TableCell>
                      <TableCell className="text-right tabular-nums text-xs">
                        {isAmount
                          ? `${fmtUsd(alert.used_usd)} / ${fmtMicroUsd(alert.limit_micro)}`
                          : `${fmtNumber(alert.used_requests)} / ${fmtNumber(alert.limit_requests)}`}
                      </TableCell>
                      <TableCell className="pr-4 text-xs text-muted-foreground">
                        {windowText(alert.window)} · {fmtDateTime(alert.period_start)}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </section>
        )}
      </section>

      {/* ── 新建 / 编辑 ── */}
      <Dialog open={formOpen} onOpenChange={setFormOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{editing ? t('quotaPolicy.edit') : t('quotaPolicy.add')}</DialogTitle>
            <DialogDescription>{t('quotaPolicy.formDesc')}</DialogDescription>
          </DialogHeader>
          <DialogBody>
            <div className="space-y-4 px-5 pb-4">
              <div className="space-y-1.5">
                <Label>{t('quotaPolicy.fieldKey')}</Label>
                <Select
                  value={form.keyId}
                  onValueChange={(value) => setForm({...form, keyId: value})}
                  disabled={!!editing}
                >
                  <SelectTrigger className={editing ? 'opacity-60' : undefined}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {keys.map((k) => (
                      <SelectItem key={k.id} value={String(k.id)}>
                        {k.name} ({k.prefix}…)
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label>{t('quotaPolicy.fieldProvider')}</Label>
                  <Input
                    value={form.provider}
                    onChange={(e) => setForm({...form, provider: e.target.value})}
                    disabled={!!editing}
                    className={editing ? 'opacity-60' : undefined}
                    placeholder={t('quota.unlimited')}
                  />
                  <p className="text-[11px] text-muted-foreground">{t('quotaPolicy.providerHint')}</p>
                </div>
                <div className="space-y-1.5">
                  <Label>{t('quotaPolicy.fieldModel')}</Label>
                  <Input
                    value={form.model}
                    onChange={(e) => setForm({...form, model: e.target.value})}
                    disabled={!!editing}
                    className={editing ? 'opacity-60' : undefined}
                    placeholder={t('quota.unlimited')}
                  />
                  <p className="text-[11px] text-muted-foreground">{t('quotaPolicy.modelHint')}</p>
                </div>
              </div>

              <div className="space-y-1.5">
                <Label>{t('quotaPolicy.fieldWindow')}</Label>
                <Select
                  value={form.window}
                  onValueChange={(value) => setForm({...form, window: value as 'day' | 'month'})}
                  disabled={!!editing}
                >
                  <SelectTrigger className={editing ? 'opacity-60' : undefined}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="day">{t('quotaPolicy.windowDay')}</SelectItem>
                    <SelectItem value="month">{t('quotaPolicy.windowMonth')}</SelectItem>
                  </SelectContent>
                </Select>
                {editing && <p className="text-[11px] text-muted-foreground">{t('quotaPolicy.identityLocked')}</p>}
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label>{t('quotaPolicy.fieldAmount')}</Label>
                  <Input
                    type="number"
                    min="0"
                    step="any"
                    value={form.amount}
                    onChange={(e) => setForm({...form, amount: e.target.value})}
                    placeholder="0.00"
                  />
                  <p className="text-[11px] text-muted-foreground">{t('quotaPolicy.amountHint')}</p>
                </div>
                <div className="space-y-1.5">
                  <Label>{t('quotaPolicy.fieldRequests')}</Label>
                  <Input
                    type="number"
                    min="0"
                    step="1"
                    value={form.requests}
                    onChange={(e) => setForm({...form, requests: e.target.value})}
                    placeholder="0"
                  />
                  <p className="text-[11px] text-muted-foreground">{t('quotaPolicy.requestsHint')}</p>
                </div>
              </div>

              <div className="space-y-1.5">
                <Label>{t('quotaPolicy.fieldThresholds')}</Label>
                <Input
                  value={form.thresholds}
                  onChange={(e) => setForm({...form, thresholds: e.target.value})}
                  placeholder={t('quotaPolicy.thresholdsPlaceholder')}
                />
                <p className="text-[11px] text-muted-foreground">{t('quotaPolicy.thresholdsHint')}</p>
              </div>

              <div className="flex items-center justify-between gap-3">
                <Label>{t('quotaPolicy.fieldEnabled')}</Label>
                <Switch
                  checked={form.enabled}
                  onCheckedChange={(checked) => setForm({...form, enabled: checked})}
                />
              </div>
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setFormOpen(false)} disabled={saving}>
              {t('common.cancel')}
            </Button>
            <Button onClick={submit} disabled={saving}>
              {t('common.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

/* ── 用量卡片与骨架屏：只用基准组件，不另造视觉 ── */

function UsageCard({
  item,
  t,
  scopeText,
  windowText,
}: {
  item: QuotaStatusItem;
  t: (key: string, params?: Record<string, string | number>) => string;
  scopeText: (p: QuotaPolicy) => string;
  windowText: (w: QuotaPolicy['window']) => string;
}) {
  const {policy} = item;
  const ratio = Math.max(item.ratio_micro ?? 0, item.ratio_requests ?? 0);
  const percent = Math.min(100, Math.round(ratio * 100));

  return (
    <section className={`rounded-2xl border p-4 ${item.exceeded ? 'border-destructive/60 bg-destructive/5' : ''}`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium">{policy.key_name || `#${policy.key_id}`}</div>
          <div className="truncate text-[11px] text-muted-foreground" title={scopeText(policy)}>
            {scopeText(policy)} · {windowText(policy.window)}
          </div>
        </div>
        {item.exceeded && (
          <Badge variant="secondary" className="rounded-full text-red-600 dark:text-red-400">
            {t('quotaPolicy.exceeded')}
          </Badge>
        )}
      </div>

      <div className="mt-3 space-y-1">
        <div className="h-1.5 w-full overflow-hidden rounded-full bg-border">
          <div
            className={`h-full rounded-full ${item.exceeded ? 'bg-destructive' : 'bg-foreground/70'}`}
            style={{width: `${percent}%`}}
          />
        </div>
        <div className="flex justify-between text-[11px] text-muted-foreground">
          <span>
            {policy.limit_micro
              ? `${fmtUsd(item.used_usd)} / ${fmtUsd(policy.limit_usd)}`
              : `${fmtUsd(item.used_usd)} · ${t('quotaPolicy.amountUnlimited')}`}
          </span>
          <span className="tabular-nums">
            {policy.limit_requests
              ? `${fmtNumber(item.used_requests)} / ${fmtNumber(policy.limit_requests)}`
              : `${fmtNumber(item.used_requests)} · ${t('quotaPolicy.requestsUnlimited')}`}
          </span>
        </div>
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-2 text-[11px]">
        <div>
          <dt className="text-muted-foreground">{t('quotaPolicy.remainingAmount')}</dt>
          <dd className="tabular-nums">
            {item.remaining_micro === null ? '—' : fmtMicroUsd(item.remaining_micro)}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">{t('quotaPolicy.remainingRequests')}</dt>
          <dd className="tabular-nums">
            {item.remaining_requests === null ? '—' : fmtNumber(item.remaining_requests)}
          </dd>
        </div>
        <div className="col-span-2">
          <dt className="text-muted-foreground">{t('quotaPolicy.resetAt')}</dt>
          <dd>
            {fmtDateTime(item.reset_at)}
            <span className="text-muted-foreground"> · {fmtRemain(item.reset_after)}</span>
          </dd>
        </div>
      </dl>

      {item.alerts.length > 0 && (
        <div className="mt-3 flex flex-wrap gap-1">
          {item.alerts.map((alert) => (
            <Badge key={alert.id} variant="secondary" className="rounded-full text-[10px]">
              {alert.threshold_percent}%
            </Badge>
          ))}
        </div>
      )}
    </section>
  );
}

function TableSkeleton({cols}: {cols: number}) {
  return (
    <section className="overflow-hidden rounded-[20px] bg-muted">
      <Table>
        <TableHeader>
          <TableRow className="border-b border-border/60 hover:bg-transparent">
            {Array.from({length: cols}).map((_, i) => (
              <TableHead key={i} className="text-[11px] text-muted-foreground">
                <div className="h-3 w-16 animate-pulse rounded bg-foreground/10" />
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {Array.from({length: 4}).map((_, r) => (
            <TableRow key={r} className="border-b border-border/40">
              {Array.from({length: cols}).map((_, c) => (
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

function CardSkeleton() {
  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
      {Array.from({length: 3}).map((_, i) => (
        <div key={i} className="space-y-3 rounded-2xl border p-4">
          <div className="h-4 w-32 animate-pulse rounded bg-foreground/10" />
          <div className="h-1.5 w-full animate-pulse rounded bg-foreground/10" />
          <div className="h-3 w-2/3 animate-pulse rounded bg-foreground/10" />
          <div className="h-3 w-1/2 animate-pulse rounded bg-foreground/10" />
        </div>
      ))}
    </div>
  );
}
