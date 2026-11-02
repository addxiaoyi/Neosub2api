'use client';

import {Fragment, useCallback, useEffect, useState} from 'react';
import {AlertCircle, ChevronDown, ChevronRight, RefreshCw, Save} from 'lucide-react';
import {errText, gatewayApi, providerApi} from '@/lib/api';
import {notify} from '@/lib/toast';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import type {ProviderInfo} from '@/lib/providers';
import {CAPABILITY_LABELS as CAP_LABELS, exposedOptionFields, STATUS_LABEL_KEY as STATUS_KEY} from '@/lib/provider-schema';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Switch} from '@/components/ui/switch';
import {Textarea} from '@/components/ui/textarea';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

type OptionKind = 'text' | 'switch' | 'json';
interface OptionField {
  key: string;
  label: string;
  kind: OptionKind;
}

/** 高级区字段来自 provider descriptor，只有 exposed=true 的 option 字段会出现在这里。 */
function optionFieldsOf(id: string): OptionField[] {
  return exposedOptionFields(id).map((field) => ({
    key: field.key,
    label: field.label,
    kind: field.type === 'switch' || field.type === 'json' ? field.type : 'text',
  }));
}

export function ProviderPanel() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [items, setItems] = useState<ProviderInfo[]>([]);
  const [loading, setLoading] = useState(true);
  /** 列表请求失败：与「空列表」区分开，避免把故障画成没有数据 */
  const [error, setError] = useState('');
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({});
  const [optDrafts, setOptDrafts] = useState<Record<string, Record<string, unknown>>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [allowAnonymous, setAllowAnonymous] = useState<boolean | null>(null);
  const [gatewayBusy, setGatewayBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const data = await providerApi.list();
      setItems(data);
      setDrafts(Object.fromEntries(data.map((p) => [p.id, p.base_url])));
      setOptDrafts(Object.fromEntries(data.map((p) => [p.id, {...p.options}])));
    } catch {
      // 不透传 axios 原文（网络问题会变成英文 Network Error）
      setError(t('providers.loadFailed'));
      setItems([]);
      notify.err(t('providers.loadFailed'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load();
    gatewayApi
      .get()
      .then((g) => setAllowAnonymous(g.allow_anonymous))
      .catch(() => setAllowAnonymous(null));
  }, [load]);

  async function patch(
    p: ProviderInfo,
    body: {enabled?: boolean; base_url?: string; api_key?: string; options?: Record<string, unknown>},
  ) {
    setBusy(p.id);
    try {
      const updated = await providerApi.update(p.id, body);
      setItems((prev) => prev.map((item) => (item.id === updated.id ? updated : item)));
      setDrafts((prev) => ({...prev, [updated.id]: updated.base_url}));
      setOptDrafts((prev) => ({...prev, [updated.id]: {...updated.options}}));
      setKeyDrafts((prev) => ({...prev, [updated.id]: ''}));
      notify.ok(t('providers.saved'), t('providers.savedDetail', {name: updated.label}));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(null);
    }
  }

  async function toggleGateway(checked: boolean) {
    setGatewayBusy(true);
    try {
      const g = await gatewayApi.update(checked);
      setAllowAnonymous(g.allow_anonymous);
      notify.ok(t('providers.gatewaySaved'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setGatewayBusy(false);
    }
  }

  function optionValue(p: ProviderInfo, key: string): unknown {
    return optDrafts[p.id]?.[key] ?? p.options[key];
  }

  function setOption(p: ProviderInfo, key: string, value: unknown) {
    setOptDrafts((prev) => ({...prev, [p.id]: {...(prev[p.id] ?? p.options), [key]: value}}));
  }

  function saveOptions(p: ProviderInfo) {
    const raw = {...(optDrafts[p.id] ?? {})};
    // json 字段在界面上是字符串，保存前解析；解析失败如实报错不落库
    for (const field of optionFieldsOf(p.id)) {
      if (field.kind !== 'json') continue;
      const v = raw[field.key];
      if (typeof v !== 'string') continue;
      if (!v.trim()) {
        delete raw[field.key];
        continue;
      }
      try {
        raw[field.key] = JSON.parse(v);
      } catch {
        notify.err(t('providers.optBadJson'));
        return;
      }
    }
    patch(p, {options: raw});
  }

  const optionFields = (p: ProviderInfo) => optionFieldsOf(p.id);
  const keyDirty = (p: ProviderInfo) => (keyDrafts[p.id] ?? '') !== '';
  const optionsDirty = (p: ProviderInfo) =>
    JSON.stringify(optDrafts[p.id] ?? p.options) !== JSON.stringify(p.options);

  return (
    <div className="flex flex-col gap-4">
      <section className="overflow-hidden rounded-[20px] bg-muted">
        <p className="px-4 pt-3 text-[11px] text-muted-foreground lg:hidden">{t('providers.scrollHint')}</p>
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('providers.colName')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('providers.colStatus')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('providers.colLicense')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('providers.colCapabilities')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('providers.colBaseUrl')}</TableHead>
              <TableHead className="sticky right-0 z-10 bg-muted pr-4 text-right text-[11px] text-muted-foreground">
                {t('providers.colEnabled')}
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((p) => (
              <Fragment key={p.id}>
                <TableRow className="border-b border-border/40">
                  <TableCell className="pl-4">
                    <div className="flex items-center gap-1">
                      {(optionFields(p).length > 0 || !p.capabilities.sidecar) && (
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-5 w-5 rounded-md"
                          title={t('providers.advanced')}
                          onClick={() => setExpanded((prev) => ({...prev, [p.id]: !prev[p.id]}))}
                        >
                          {expanded[p.id] ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                        </Button>
                      )}
                      <div>
                        <div className="flex items-center gap-1.5">
                          <span className="text-sm font-medium">{p.label}</span>
                          {p.risk_level === 'danger' && (
                            <Badge variant="destructive" className="h-5 rounded-md px-1.5 text-xs font-semibold">
                              ⚠️ 商用禁用
                            </Badge>
                          )}
                          {p.risk_level === 'warn' && (
                            <Badge
                              variant="secondary"
                              className="h-5 rounded-md bg-amber-100 px-1.5 text-[10px] font-semibold text-amber-900 dark:bg-amber-900/40 dark:text-amber-200"
                            >
                              ⚠️ GPL 隔离
                            </Badge>
                          )}
                        </div>
                        <div className="text-[10px] text-muted-foreground">{p.vendor}</div>
                        <div className="text-[10px] text-muted-foreground">{p.id}</div>
                        {p.risk_level !== 'safe' && p.risk_note && (
                          <div className="mt-0.5 max-w-[260px] whitespace-normal text-xs text-red-700 dark:text-amber-200">
                            {p.risk_note}
                          </div>
                        )}
                      </div>
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant={p.status === 'implemented' ? 'default' : 'secondary'} className="rounded-full">
                      {t(STATUS_KEY[p.status] ?? 'providers.statusStub')}
                    </Badge>
                    <div className="mt-1 text-[10px] text-muted-foreground">{p.runtime}</div>
                  </TableCell>
                  <TableCell className="font-mono text-[11px] text-muted-foreground">{p.license}</TableCell>
                  <TableCell>
                    <div className="flex max-w-[170px] flex-wrap gap-1">
                      {CAP_LABELS.filter(([key]) => p.capabilities[key]).map(([key, label]) => (
                        <span
                          key={key}
                          className="rounded-full bg-background px-1.5 py-0.5 text-[10px] text-muted-foreground"
                        >
                          {t(label)}
                        </span>
                      ))}
                      {p.protocols.length > 0 && (
                        <span className="rounded-full bg-background px-1.5 py-0.5 text-[10px] text-muted-foreground">
                          {p.protocols.join(' / ')}
                        </span>
                      )}
                    </div>
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-1.5">
                      <Input
                        value={drafts[p.id] ?? p.base_url}
                        onChange={(e) => setDrafts((prev) => ({...prev, [p.id]: e.target.value}))}
                        disabled={!isAdmin || busy === p.id}
                        placeholder={p.default_base_url}
                        className="h-8 w-[150px] bg-background font-mono text-[11px]"
                      />
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 rounded-md"
                        title={t('providers.save')}
                        disabled={!isAdmin || busy === p.id || (drafts[p.id] ?? p.base_url) === p.base_url}
                        onClick={() => patch(p, {base_url: drafts[p.id] ?? p.base_url})}
                      >
                        <Save className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  </TableCell>
                  <TableCell className="sticky right-0 bg-muted pr-4 text-right">
                    <ConfirmDialog
                      trigger={
                        <Switch
                          checked={p.enabled}
                          disabled={!isAdmin || busy === p.id}
                          aria-label={t(p.enabled ? 'providers.disable' : 'providers.enable')}
                        />
                      }
                      title={t(p.enabled ? 'providers.disableConfirmTitle' : 'providers.enableConfirmTitle', {
                        name: p.label,
                      })}
                      description={t(p.enabled ? 'providers.disableConfirmDesc' : 'providers.enableConfirmDesc')}
                      confirmText={t(p.enabled ? 'providers.disable' : 'providers.enable')}
                      destructive={p.enabled}
                      onConfirm={() => patch(p, {enabled: !p.enabled})}
                    />
                  </TableCell>
                </TableRow>
                {expanded[p.id] && (
                  <TableRow key={`${p.id}-adv`} className="border-b border-border/40 bg-background/40">
                    <TableCell colSpan={6} className="px-6 py-3">
                      <div className="flex flex-col gap-3">
                        {!p.capabilities.sidecar && (
                          <div className="flex items-center gap-2">
                            <span className="w-28 text-[11px] text-muted-foreground">{t('providers.colApiKey')}</span>
                            <Input
                              type="password"
                              value={keyDrafts[p.id] ?? ''}
                              onChange={(e) => setKeyDrafts((prev) => ({...prev, [p.id]: e.target.value}))}
                              disabled={!isAdmin || busy === p.id}
                              placeholder={p.has_api_key ? '********' : t('providers.apiKeyPlaceholder')}
                              className="h-8 w-[280px] bg-background font-mono text-[11px]"
                            />
                            <Button
                              variant="ghost"
                              size="icon"
                              className="h-7 w-7 rounded-md"
                              title={t('providers.saveApiKey')}
                              disabled={!isAdmin || busy === p.id || !keyDirty(p)}
                              onClick={() => patch(p, {api_key: keyDrafts[p.id] ?? ''})}
                            >
                              <Save className="h-3.5 w-3.5" />
                            </Button>
                            {p.has_api_key && (
                              <span className="text-[10px] text-emerald-600 dark:text-emerald-400">{t('providers.apiKeySet')}</span>
                            )}
                          </div>
                        )}
                        {optionFields(p).map((field) => (
                          <div key={field.key} className="flex items-center gap-2">
                            <span className="w-28 text-[11px] text-muted-foreground">{t(field.label)}</span>
                            {field.kind === 'switch' ? (
                              <Switch
                                checked={Boolean(optionValue(p, field.key))}
                                disabled={!isAdmin || busy === p.id}
                                onCheckedChange={(checked) => setOption(p, field.key, checked)}
                              />
                            ) : field.kind === 'json' ? (
                              <Textarea
                                rows={2}
                                value={
                                  typeof optionValue(p, field.key) === 'string'
                                    ? String(optionValue(p, field.key))
                                    : JSON.stringify(optionValue(p, field.key) ?? {})
                                }
                                onChange={(e) => setOption(p, field.key, e.target.value)}
                                disabled={!isAdmin || busy === p.id}
                                placeholder='{"User-Agent": "..."}'
                                className="w-[380px] bg-background font-mono text-[11px]"
                              />
                            ) : (
                              <Input
                                value={String(optionValue(p, field.key) ?? '')}
                                onChange={(e) => setOption(p, field.key, e.target.value)}
                                disabled={!isAdmin || busy === p.id}
                                className="h-8 w-[280px] bg-background font-mono text-[11px]"
                              />
                            )}
                          </div>
                        ))}
                        {optionFields(p).length > 0 && (
                          <div>
                            <Button
                              size="sm"
                              variant="outline"
                              className="h-7 rounded-full text-[11px]"
                              disabled={!isAdmin || busy === p.id || !optionsDirty(p)}
                              onClick={() => saveOptions(p)}
                            >
                              {t('providers.saveOptions')}
                            </Button>
                          </div>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                )}
              </Fragment>
            ))}
          </TableBody>
        </Table>

        {error && !loading && (
          <div className="flex flex-col items-center gap-3 px-4 py-12 text-center">
            <AlertCircle className="h-5 w-5 text-red-600 dark:text-red-400" />
            <div className="text-sm">{error}</div>
            <Button variant="outline" size="sm" className="rounded-full" onClick={load}>
              <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
              {t('providers.retry')}
            </Button>
          </div>
        )}
        {!items.length && !loading && !error && (
          <div className="flex flex-col items-center gap-3 px-4 py-12 text-center">
            <div className="text-sm">{t('providers.empty')}</div>
            <div className="text-xs text-muted-foreground">{t('providers.emptyHint')}</div>
            <Button variant="outline" size="sm" className="rounded-full" onClick={load}>
              <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
              {t('providers.retry')}
            </Button>
          </div>
        )}
        {loading && (
          <div className="px-4 py-12 text-center text-xs text-muted-foreground">
            <RefreshCw className="mx-auto mb-2 h-4 w-4 animate-spin" />
            {t('common.loading')}
          </div>
        )}
        {!isAdmin && items.length > 0 && (
          <div className="px-4 py-3 text-[11px] text-muted-foreground">{t('providers.requiresAdmin')}</div>
        )}
      </section>

      {/* 网关鉴权：默认要求 Bearer key；匿名访问仅本机调试时打开 */}
      <section className="flex items-center justify-between rounded-[20px] bg-muted px-4 py-3">
        <div>
          <div className="text-sm font-medium">{t('providers.gatewayTitle')}</div>
          <div className="text-[11px] text-muted-foreground">{t('providers.gatewayDesc')}</div>
        </div>
        <ConfirmDialog
          trigger={
            <Switch
              checked={Boolean(allowAnonymous)}
              disabled={!isAdmin || gatewayBusy || allowAnonymous === null}
              aria-label={t('providers.gatewayTitle')}
            />
          }
          title={t(allowAnonymous ? 'providers.gatewayOffTitle' : 'providers.gatewayOnTitle')}
          description={t(allowAnonymous ? 'providers.gatewayOffDesc' : 'providers.gatewayOnDesc')}
          confirmText={t(allowAnonymous ? 'providers.gatewayOffConfirm' : 'providers.gatewayOnConfirm')}
          destructive={!allowAnonymous}
          onConfirm={() => toggleGateway(!allowAnonymous)}
        />
      </section>
    </div>
  );
}
