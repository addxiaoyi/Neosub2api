'use client';

import {Fragment, useCallback, useEffect, useRef, useState} from 'react';
import Link from 'next/link';
import {
  AlertCircle,
  ChevronDown,
  ChevronRight,
  Pencil,
  Plus,
  PlugZap,
  RefreshCw,
  Save,
  ShieldCheck,
  Trash2,
  TriangleAlert,
} from 'lucide-react';
import {credentialApi, errText, gatewayApi, providerApi, runtimeConfigApi} from '@/lib/api';
import {notify} from '@/lib/toast';
import {useAuth} from '@/lib/auth-context';
import {useI18n} from '@/lib/i18n/provider';
import {
  credentialNote,
  legacyBaseWarning,
  localizedRequires,
  sidecarMessage,
  type ProviderCredential,
  type ProviderCredentialInput,
  type ProviderCredentialStateSupport,
  type ProviderHealth,
  type ProviderInfo,
  type ProviderTokenCheck,
} from '@/lib/providers';
import {formatDateTime} from '@/lib/utils';
import {CAPABILITY_LABELS, exposedOptionFields, STATUS_LABEL_KEY} from '@/lib/provider-schema';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {GrokRegisterTasks} from '@/components/common/settings/GrokRegisterTasks';
import {ProviderChecklist} from '@/components/common/settings/ProviderChecklist';
import {ProviderConsolePanel} from '@/components/common/settings/ProviderConsolePanel';
import {QoderSessionsPanel} from '@/components/common/settings/QoderSessionsPanel';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Switch} from '@/components/ui/switch';
import {Textarea} from '@/components/ui/textarea';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

// 挂管理台的服务商；其余服务商保持原有展开区不变
const CONSOLE_PROVIDERS = new Set([
  'codebuddy',
  'muse',
  'gptgrok',
  'zcode',
  'trae',
  'qoder',
  'qoder_sidecar',
  'codar',
  'codar_sidecar',
]);

interface CredDraft {
  label: string;
  base_url: string;
  secret: string;
  sort_order: string;
  note: string;
  enabled: boolean;
}

const EMPTY_CRED: CredDraft = {label: '', base_url: '', secret: '', sort_order: '', note: '', enabled: true};

const HEALTH_LABEL_KEY: Record<string, string> = {
  ok: 'providers.healthOk',
  failed: 'providers.healthFailed',
  unsupported: 'providers.healthUnsupported',
};

/** 判不出时能给用户的下一步动作；后端 support 决定走哪一条。 */
const CREDENTIAL_NEXT_KEY: Record<ProviderCredentialStateSupport, string> = {
  auth_probe: 'providers.credentialNoteNextGeneric',
  token_expiry: 'providers.credentialNoteNextGeneric',
  request_log: 'providers.credentialNoteNextRequestLog',
  unavailable: 'providers.credentialNoteNextUnavailable',
};

/** ok 用主色，failed 才用告警色，unsupported/未检测保持中性，避免把「不支持」画成故障。 */
function healthVariant(status: string | null): 'default' | 'destructive' | 'secondary' {
  if (status === 'ok') return 'default';
  if (status === 'failed') return 'destructive';
  return 'secondary';
}

/** 编辑时 secret 故意留空：掩码值不回填，也不会被当成新密钥提交。 */
function draftOfCred(c: ProviderCredential): CredDraft {
  return {
    label: c.label,
    base_url: c.base_url,
    secret: '',
    sort_order: String(c.sort_order),
    note: c.note,
    enabled: c.enabled,
  };
}

/** 服务商接入：列表沿用本页表格样式，单条配置在行内展开。 */
export function ProvidersSection() {
  const {t, tp} = useI18n();
  const {isAdmin} = useAuth();
  const [items, setItems] = useState<ProviderInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [keyDrafts, setKeyDrafts] = useState<Record<string, string>>({});
  const [optDrafts, setOptDrafts] = useState<Record<string, Record<string, unknown>>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [allowAnonymous, setAllowAnonymous] = useState<boolean | null>(null);
  const [gatewayBusy, setGatewayBusy] = useState(false);
  const [health, setHealth] = useState<ProviderHealth | null>(null);
  const [healthEnabled, setHealthEnabled] = useState<boolean | null>(null);
  const [healthBusy, setHealthBusy] = useState(false);
  const [healthToggleBusy, setHealthToggleBusy] = useState(false);

  const [creds, setCreds] = useState<ProviderCredential[]>([]);
  const [credLoading, setCredLoading] = useState(true);
  const [credError, setCredError] = useState('');
  const [adding, setAdding] = useState<Record<string, boolean>>({});
  const [addDrafts, setAddDrafts] = useState<Record<string, CredDraft>>({});
  const [editing, setEditing] = useState<number | null>(null);
  const [editDraft, setEditDraft] = useState<CredDraft | null>(null);
  const [tokenChecks, setTokenChecks] = useState<Record<string, ProviderTokenCheck>>({});
  // hash 直达只消费一次，避免凭据列表刷新后焦点被反复抢走
  const hashFocused = useRef(false);

  const load = useCallback(async () => {
    setLoading(true);
    setCredLoading(true);
    setError('');
    setCredError('');
    try {
      const data = await providerApi.list();
      setItems(data);
      setDrafts(Object.fromEntries(data.map((p) => [p.id, p.base_url])));
      setOptDrafts(Object.fromEntries(data.map((p) => [p.id, {...p.options}])));
      try {
        setCreds(await credentialApi.list());
      } catch (e) {
        setCreds([]);
        setCredError(errText(e));
      }
      try {
        // 检测结果是只读观测，viewer 也看；失败不拖垮上面的列表
        setHealth(await providerApi.health());
      } catch {
        setHealth(null);
      }
    } catch {
      // 不透传 axios 原文（网络问题会变成英文 Network Error）
      setError(t('providers.loadFailed'));
      setItems([]);
      setCreds([]);
    } finally {
      setLoading(false);
      setCredLoading(false);
    }
  }, [t]);

  useEffect(() => {
    load();
    // 网关开关是 admin-only 配置：viewer 打开设置页不该为此拿一次 403
    if (!isAdmin) return;
    gatewayApi
      .get()
      .then((g) => setAllowAnonymous(g.allow_anonymous))
      .catch(() => setAllowAnonymous(null));
    // 总开关存在运行参数里（viewer 拿不到这个接口，也不该看到开关）
    runtimeConfigApi
      .get()
      .then((r) => {
        const item = r.items.find((i) => i.key === 'health_check_enabled');
        setHealthEnabled(Boolean(item?.value));
      })
      .catch(() => setHealthEnabled(null));
  }, [load, isAdmin]);

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
      // 启用不校验前置（允许先启用后配），但必须把「仍不可用」说出来，不能静默成功
      if (body.enabled === true && updated.readiness === 'needs_config') {
        notify.warn(
          t('providers.enabledButUnavailable', {
            requires: updated.requires.map((item) => tp(item)).join(t('common.listSeparator')),
          }),
        );
      } else {
        notify.ok(t('providers.saved'), t('providers.savedDetail', {name: tp(updated.label)}));
      }
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

  /** 手动触发一轮；总开关关着也能跑（只影响自动调度，不影响手动验证） */
  async function runHealth() {
    setHealthBusy(true);
    try {
      const next = await providerApi.runHealth();
      setHealth(next);
      setHealthEnabled(next.enabled);
      notify.ok(t('providers.healthRunOk'), t('providers.healthRunDone', {count: next.providers.length}));
    } catch (e) {
      notify.err(`${t('providers.healthRunFailed')}：${errText(e)}`);
    } finally {
      setHealthBusy(false);
    }
  }

  async function toggleHealthCheck(checked: boolean) {
    setHealthToggleBusy(true);
    try {
      await runtimeConfigApi.save({health_check_enabled: checked});
      setHealthEnabled(checked);
      setHealth((prev) => (prev ? {...prev, enabled: checked} : prev));
      notify.ok(t('providers.healthToggleSaved'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setHealthToggleBusy(false);
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
    for (const field of exposedOptionFields(p.id)) {
      if (field.type !== 'json') continue;
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

  const fieldsOf = (p: ProviderInfo) => exposedOptionFields(p.id);
  const keyDirty = (p: ProviderInfo) => (keyDrafts[p.id] ?? '') !== '';
  const optionsDirty = (p: ProviderInfo) =>
    JSON.stringify(optDrafts[p.id] ?? p.options) !== JSON.stringify(p.options);
  const credsOf = (p: ProviderInfo) => creds.filter((c) => c.provider === p.id);
  const healthOf = (p: ProviderInfo) => health?.providers.find((h) => h.provider === p.id);

  /** 排序字段留空 = 交给后端默认值；填了就必须是整数，先拦下再提交。 */
  function parsedSort(raw: string): number | null | undefined {
    const text = raw.trim();
    if (!text) return undefined;
    const n = Number(text);
    if (!Number.isFinite(n) || !Number.isInteger(n)) return null;
    return n;
  }

  async function createCred(p: ProviderInfo) {
    const draft = addDrafts[p.id] ?? EMPTY_CRED;
    const sort = parsedSort(draft.sort_order);
    if (sort === null) {
      notify.err(t('providers.credBadSort'));
      return;
    }
    setBusy(`cred:${p.id}`);
    try {
      const body: ProviderCredentialInput = {
        provider: p.id,
        label: draft.label.trim(),
        base_url: draft.base_url.trim(),
        secret: draft.secret,
        enabled: draft.enabled,
        note: draft.note.trim(),
      };
      if (sort !== undefined) body.sort_order = sort;
      await credentialApi.create(body);
      setAddDrafts((prev) => ({...prev, [p.id]: EMPTY_CRED}));
      setAdding((prev) => ({...prev, [p.id]: false}));
      notify.ok(t('providers.credSaved'), tp(p.label));
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(null);
    }
  }

  async function saveCred(c: ProviderCredential) {
    if (!editDraft) return;
    const sort = parsedSort(editDraft.sort_order);
    if (sort === null) {
      notify.err(t('providers.credBadSort'));
      return;
    }
    setBusy(`cred:${c.id}`);
    try {
      const body: Omit<ProviderCredentialInput, 'provider'> = {
        label: editDraft.label.trim(),
        enabled: editDraft.enabled,
        note: editDraft.note.trim(),
      };
      // 地址没改就不回传，免得多一次内网地址校验把整次保存打回
      const baseUrl = editDraft.base_url.trim();
      if (baseUrl !== c.base_url) body.base_url = baseUrl;
      // 只有用户真的输入了新密钥才提交；空串不发送，避免把掩码当成新值
      if (editDraft.secret !== '') body.secret = editDraft.secret;
      if (sort !== undefined) body.sort_order = sort;
      await credentialApi.update(c.id, body);
      setEditing(null);
      setEditDraft(null);
      notify.ok(t('providers.credSaved'), c.label || `#${c.id}`);
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(null);
    }
  }

  async function toggleCred(c: ProviderCredential, enabled: boolean) {
    setBusy(`cred:${c.id}`);
    try {
      await credentialApi.update(c.id, {enabled});
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(null);
    }
  }

  async function deleteCred(c: ProviderCredential) {
    setBusy(`cred:${c.id}`);
    try {
      await credentialApi.remove(c.id);
      if (editing === c.id) {
        setEditing(null);
        setEditDraft(null);
      }
      notify.ok(t('providers.credDeleted'), c.label || `#${c.id}`);
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(null);
    }
  }

  function startEdit(c: ProviderCredential) {
    setEditing(c.id);
    setEditDraft(draftOfCred(c));
  }

  const TOKEN_STATE_LABEL: Record<ProviderTokenCheck['state'], string> = {
    valid: 'providers.codarTokenValid',
    expired: 'providers.codarTokenExpired',
    invalid: 'providers.codarTokenInvalid',
    missing: 'providers.codarTokenMissing',
  };

  function tokenStateText(result: ProviderTokenCheck): string {
    const head = t(TOKEN_STATE_LABEL[result.state]);
    const detail = result.next_step || result.reason;
    return detail ? `${head}：${detail}` : head;
  }

  async function checkToken(p: ProviderInfo, opts: {credentialId?: number; token?: string}) {
    const key = opts.credentialId ? `cred:${opts.credentialId}` : `draft:${p.id}`;
    setBusy(key);
    try {
      const result = await credentialApi.validate(p.id, {
        credential_id: opts.credentialId,
        token: opts.token,
      });
      setTokenChecks((prev) => ({...prev, [key]: result}));
      if (result.state === 'valid') notify.ok(tokenStateText(result));
      else notify.err(tokenStateText(result));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(null);
    }
  }

  function tokenBadge(key: string) {
    const result = tokenChecks[key];
    if (!result) return null;
    const variant = result.state === 'valid' ? 'default' : result.state === 'missing' ? 'secondary' : 'destructive';
    return (
      <Badge variant={variant} className="rounded-full text-[10px]" title={result.reason || result.next_step}>
        {t(TOKEN_STATE_LABEL[result.state])}
      </Badge>
    );
  }

  function credFields(
    p: ProviderInfo,
    draft: CredDraft,
    onChange: (next: CredDraft) => void,
    masked: string,
    secretId: string,
  ) {
    return (
      <>
        <div className="flex items-center gap-2">
          <span className="w-28 text-[11px] text-muted-foreground">{t('providers.credLabel')}</span>
          <Input
            value={draft.label}
            onChange={(e) => onChange({...draft, label: e.target.value})}
            disabled={!isAdmin || busy !== null}
            placeholder={t('providers.credLabelPlaceholder')}
            className="h-8 w-[280px] bg-background text-[11px]"
          />
        </div>
        <div className="flex items-center gap-2">
          <span className="w-28 text-[11px] text-muted-foreground">{t('providers.credBaseUrl')}</span>
          <Input
            value={draft.base_url}
            onChange={(e) => onChange({...draft, base_url: e.target.value})}
            disabled={!isAdmin || busy !== null}
            placeholder={p.base_url || p.default_base_url}
            className="h-8 w-[280px] bg-background font-mono text-[11px]"
          />
        </div>
        <div className="flex items-center gap-2">
          <span className="w-28 text-[11px] text-muted-foreground">{t('providers.credSecret')}</span>
          <Input
            id={secretId}
            type="password"
            autoComplete="off"
            value={draft.secret}
            onChange={(e) => onChange({...draft, secret: e.target.value})}
            disabled={!isAdmin || busy !== null}
            placeholder={masked || t('providers.credSecretPlaceholder')}
            className="h-8 w-[280px] bg-background font-mono text-[11px]"
          />
          {masked && <span className="text-[10px] text-muted-foreground">{t('providers.credSecretKeep')}</span>}
        </div>
        <div className="flex items-center gap-2">
          <span className="w-28 text-[11px] text-muted-foreground">{t('providers.credSortOrder')}</span>
          <Input
            inputMode="numeric"
            value={draft.sort_order}
            onChange={(e) => onChange({...draft, sort_order: e.target.value})}
            disabled={!isAdmin || busy !== null}
            placeholder="0"
            className="h-8 w-[100px] bg-background font-mono text-[11px]"
          />
        </div>
        <div className="flex items-center gap-2">
          <span className="w-28 text-[11px] text-muted-foreground">{t('providers.credNote')}</span>
          <Input
            value={draft.note}
            onChange={(e) => onChange({...draft, note: e.target.value})}
            disabled={!isAdmin || busy !== null}
            placeholder={t('providers.credNotePlaceholder')}
            className="h-8 w-[380px] bg-background text-[11px]"
          />
        </div>
        <div className="flex items-center gap-2">
          <span className="w-28 text-[11px] text-muted-foreground">{t('providers.credEnabled')}</span>
          <Switch
            checked={draft.enabled}
            onCheckedChange={(checked) => onChange({...draft, enabled: checked})}
            disabled={!isAdmin || busy !== null}
          />
        </div>
      </>
    );
  }

  /**
   * 「重新粘贴凭据」的直达动作：展开该行、打开第一条凭据的编辑位（没有凭据就打开新增），
   * 再把焦点落到密文输入框。只做界面导航，不代替用户写库。
   */
  function focusRepaste(p: ProviderInfo) {
    setExpanded((prev) => ({...prev, [p.id]: true}));
    const target = credsOf(p).find((c) => c.has_secret) ?? credsOf(p)[0];
    if (target) {
      startEdit(target);
      setAdding((prev) => ({...prev, [p.id]: false}));
      focusSecret(`provider-secret-${target.id}`);
    } else if (!p.capabilities.sidecar) {
      setAdding((prev) => ({...prev, [p.id]: true}));
      focusSecret(`provider-api-key-${p.id}`);
    }
    document.getElementById('provider-' + p.id)?.scrollIntoView({behavior: 'smooth', block: 'center'});
  }

  function focusSecret(id: string) {
    window.setTimeout(() => {
      const el = document.getElementById(id) as HTMLInputElement | null;
      el?.focus();
    }, 0);
  }

  // 账号页带 #provider-<id> 过来时落到同一个动作，跨页也不用用户自己找输入框
  useEffect(() => {
    if (hashFocused.current || credLoading || items.length === 0) return;
    const hash = window.location.hash;
    if (!hash.startsWith('#provider-')) return;
    const target = items.find((item) => item.id === hash.slice('#provider-'.length));
    if (!target) return;
    hashFocused.current = true;
    focusRepaste(target);
    // 只在首次加载后消费一次 hash，之后由用户自己操作
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, credLoading]);

  return (
    <div className="space-y-3">
      <section className="overflow-hidden rounded-[20px] bg-muted">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('providers.colName')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('providers.colStatus')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('providers.colCapabilities')}</TableHead>
              <TableHead className="text-[11px] text-muted-foreground">{t('providers.colLicense')}</TableHead>
              <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">{t('providers.colEnabled')}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {items.map((p) => (
              <Fragment key={p.id}>
                <TableRow id={'provider-' + p.id} className="border-b border-border/40">
                  <TableCell className="pl-4">
                    <div className="flex items-center gap-1">
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-5 w-5 rounded-md"
                        title={t('providers.advanced')}
                        onClick={() => setExpanded((prev) => ({...prev, [p.id]: !prev[p.id]}))}
                      >
                        {expanded[p.id] ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
                      </Button>
                      <div>
                        <div className="text-sm font-medium">{tp(p.label)}</div>
                        <div className="text-[10px] text-muted-foreground">{tp(p.vendor)} · {p.id}</div>
                      </div>
                    </div>
                  </TableCell>
                  <TableCell>
                    <Badge variant={p.status === 'implemented' ? 'default' : 'secondary'} className="rounded-full">
                      {t(STATUS_LABEL_KEY[p.status] ?? 'providers.statusStub')}
                    </Badge>
                    <div className="mt-1 text-[10px] text-muted-foreground">{p.runtime}</div>
                    <div className="mt-1">
                      <Badge variant="secondary" className="rounded-full text-[10px]">
                        {t(p.readiness === 'needs_config' ? 'providers.needsConfig' : 'providers.ready')}
                      </Badge>
                    </div>
                    {/* 判不出失效时别硬判：徽标写「未知」，tooltip 带上后端给的判定说明 */}
                    {p.credential_state === 'unknown' && (
                      <div className="mt-1">
                        <Badge
                          variant="secondary"
                          className="rounded-full text-[10px]"
                          title={credentialNote(p, t, tp)}
                        >
                          {t('providers.credentialUnknown')}
                        </Badge>
                      </div>
                    )}
                    {/* 凭据已被上游明确拒掉：不是「还没配」，给出直达重新粘贴的入口 */}
                    {p.credential_state === 'invalidated' && (
                      <div className="mt-1 flex flex-wrap items-center gap-1">
                        <Badge
                          variant="destructive"
                          className="rounded-full text-[10px]"
                          title={credentialNote(p, t, tp)}
                        >
                          {t('providers.credentialInvalidated')}
                        </Badge>
                        <Button
                          size="sm"
                          variant="outline"
                          className="h-6 rounded-full px-2 text-[10px]"
                          disabled={!isAdmin}
                          title={credentialNote(p, t, tp)}
                          onClick={() => focusRepaste(p)}
                        >
                          {t('providers.repasteCredential')}
                        </Button>
                      </div>
                    )}
                    {/* sidecar 存活是独立事实：在线时单独标一句，异常时由 requires 行给出原因 */}
                    {p.sidecar_status?.state === 'alive' && (
                      <div className="mt-1">
                        <Badge
                          variant="secondary"
                          className="rounded-full bg-emerald-500/10 text-[10px] text-emerald-700 dark:text-emerald-400"
                          title={sidecarMessage(p.sidecar_status, t)}
                        >
                          {sidecarMessage(p.sidecar_status, t)}
                        </Badge>
                      </div>
                    )}
                    {/* 启用状态与就绪状态分开看：已启用但缺前置，卡片上直接标不可用 */}
                    {p.enabled && p.readiness === 'needs_config' && (
                      <div className="mt-1">
                        <Badge
                          variant="secondary"
                          className="rounded-full bg-amber-500/10 text-[10px] text-amber-700 dark:text-amber-400"
                          title={localizedRequires(p, t, tp).join('；')}
                        >
                          {t('providers.enabledUnavailable')}
                        </Badge>
                      </div>
                    )}
                    {(() => {
                      const entry = healthOf(p);
                      const status = entry?.status ?? null;
                      return (
                        <div className="mt-1 flex flex-wrap items-center gap-1">
                          <Badge
                            variant={healthVariant(status)}
                            className="rounded-full text-[10px]"
                            title={entry?.error || undefined}
                          >
                            {t(status ? HEALTH_LABEL_KEY[status] : 'providers.healthNone')}
                          </Badge>
                          {entry?.checked_at ? (
                            <span className="text-[10px] text-muted-foreground">
                              {formatDateTime(new Date(entry.checked_at * 1000).toISOString())}
                            </span>
                          ) : null}
                        </div>
                      );
                    })()}
                  </TableCell>
                  <TableCell className="max-w-[180px] whitespace-normal text-[11px] text-muted-foreground">
                    {CAPABILITY_LABELS.filter(([key]) => p.capabilities[key])
                      .map(([, label]) => t(label))
                      .join(' / ') || '—'}
                    {Object.entries(p.limitations).map(([key, text]) => (
                      <div key={key} className="mt-0.5">
                        {key}: {tp(text)}
                      </div>
                    ))}
                  </TableCell>
                  <TableCell className="max-w-[220px] whitespace-normal text-[11px] text-muted-foreground">
                    <div className="font-mono">{p.license}</div>
                    {p.risk_note && <div className="mt-0.5">{tp(p.risk_note)}</div>}
                  </TableCell>
                  <TableCell className="pr-4 text-right">
                    <ConfirmDialog
                      trigger={
                        <Switch
                          checked={p.enabled}
                          disabled={!isAdmin || busy === p.id}
                          aria-label={t(p.enabled ? 'providers.disable' : 'providers.enable')}
                        />
                      }
                      title={t(p.enabled ? 'providers.disableConfirmTitle' : 'providers.enableConfirmTitle', {
                        name: tp(p.label),
                      })}
                      description={t(p.enabled ? 'providers.disableConfirmDesc' : 'providers.enableConfirmDesc')}
                      confirmText={t(p.enabled ? 'providers.disable' : 'providers.enable')}
                      destructive={p.enabled}
                      onConfirm={() => patch(p, {enabled: !p.enabled})}
                    />
                  </TableCell>
                </TableRow>
                <TableRow className="border-b border-border/40 bg-background/40 hover:bg-transparent">
                  <TableCell colSpan={5} className="px-6 py-2">
                    <ProviderChecklist
                      provider={p}
                      healthStatus={healthOf(p)?.status ?? null}
                      healthError={healthOf(p)?.error ?? null}
                      canEdit={isAdmin}
                      onEnable={() => patch(p, {enabled: true})}
                      onRunHealth={runHealth}
                      onPick={() => {
                        document.getElementById('provider-' + p.id)?.scrollIntoView({behavior: 'smooth', block: 'center'});
                      }}
                    />
                  </TableCell>
                </TableRow>
                {p.base_url_warning && (
                  <TableRow className="border-b border-border/40 bg-background/40 hover:bg-transparent">
                    <TableCell colSpan={5} className="px-6 py-2">
                      <div className="flex items-start gap-2 rounded-xl bg-muted/60 px-3 py-2 text-[11px] leading-4 text-muted-foreground">
                        <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                        <span className="whitespace-normal">{legacyBaseWarning(p, t)}</span>
                      </div>
                    </TableCell>
                  </TableRow>
                )}
                {expanded[p.id] && (
                  <TableRow className="border-b border-border/40 bg-background/40">
                    <TableCell colSpan={5} className="px-6 py-3">
                      <div className="flex flex-col gap-2">
                        <div className="flex items-center gap-2">
                          <span className="w-28 text-[11px] text-muted-foreground">{t('providers.colBaseUrl')}</span>
                          <Input
                            value={drafts[p.id] ?? p.base_url}
                            onChange={(e) => setDrafts((prev) => ({...prev, [p.id]: e.target.value}))}
                            disabled={!isAdmin || busy === p.id}
                            placeholder={p.default_base_url}
                            className="h-8 w-[280px] bg-background font-mono text-[11px]"
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
                        {!p.capabilities.sidecar && (
                          <div className="flex items-center gap-2">
                            <span className="w-28 text-[11px] text-muted-foreground">{t('providers.colApiKey')}</span>
                            <Input
                              id={`provider-api-key-${p.id}`}
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
                              <span className="text-[10px] text-muted-foreground">{t('providers.apiKeySet')}</span>
                            )}
                            {p.id === 'codar' && (
                              <>
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  className="h-7 w-7 rounded-md"
                                  title={t('providers.codarCheckToken')}
                                  disabled={!isAdmin || busy === `draft:${p.id}` || !keyDrafts[p.id]?.trim()}
                                  onClick={() => checkToken(p, {token: keyDrafts[p.id]})}
                                >
                                  <ShieldCheck className="h-3.5 w-3.5" />
                                </Button>
                                {tokenBadge(`draft:${p.id}`)}
                              </>
                            )}
                          </div>
                        )}
                        {/* 为什么只能给「未知」、用户能做什么：后端 note 是唯一说明，原样展示 */}
                        {(p.credential_state === 'unknown' || p.credential_state_note !== '') && (
                          <div className="flex items-start gap-2 rounded-xl bg-muted/60 px-3 py-2 text-[11px] leading-4 text-muted-foreground">
                            <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                            <div className="flex min-w-0 flex-col gap-0.5">
                              <span className="whitespace-normal">
                                <span className="text-foreground/80">{t('providers.credentialNoteWhy')}</span>
                                {credentialNote(p, t, tp)}
                              </span>
                              <span className="whitespace-normal">
                                <span className="text-foreground/80">{t('providers.credentialNoteNext')}</span>
                                {t(CREDENTIAL_NEXT_KEY[p.credential_state_support])}
                              </span>
                            </div>
                          </div>
                        )}
                        {p.id === 'sub2api' && (
                          <div className="flex items-center gap-2">
                            <span className="w-28 text-[11px] text-muted-foreground">{t('providers.sub2apiQuota')}</span>
                            <Button asChild size="sm" variant="outline" className="h-7 rounded-full text-[11px]">
                              <Link href="/quota">{t('providers.sub2apiOpenQuota')}</Link>
                            </Button>
                          </div>
                        )}
                        {fieldsOf(p).map((field) => (
                          <div key={field.key} className="flex flex-col gap-0.5">
                            <div className="flex items-center gap-2">
                              <span className="w-28 text-[11px] text-muted-foreground">{t(field.label)}</span>
                              {field.type === 'switch' ? (
                                <Switch
                                  checked={Boolean(optionValue(p, field.key))}
                                  disabled={!isAdmin || busy === p.id}
                                  onCheckedChange={(checked) => setOption(p, field.key, checked)}
                                />
                              ) : field.type === 'longtext' ? (
                                <Textarea
                                  rows={6}
                                  value={String(optionValue(p, field.key) ?? '')}
                                  onChange={(e) => setOption(p, field.key, e.target.value)}
                                  disabled={!isAdmin || busy === p.id}
                                  className="w-[380px] bg-background font-mono text-[11px]"
                                />
                              ) : field.type === 'json' ? (
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
                            {field.hint && (
                              <div className="pl-[7.5rem] text-[10px] leading-4 text-muted-foreground">
                                {t(field.hint)}
                              </div>
                            )}
                          </div>
                        ))}
                        {fieldsOf(p).length > 0 && (
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

                        {/* 统一凭据库：同一服务商可挂多条，按 sort_order 轮询 */}
                        <div className="mt-1 flex flex-col gap-2 border-t border-border/40 pt-2">
                          <div className="flex items-center gap-2">
                            <span className="text-[11px] text-muted-foreground">{t('providers.credentialsTitle')}</span>
                            <span className="text-[10px] text-muted-foreground">
                              {t('providers.credentialsCount', {count: credsOf(p).length})}
                            </span>
                            {isAdmin && !adding[p.id] && (
                              <Button
                                size="sm"
                                variant="outline"
                                className="ml-auto h-7 rounded-full text-[11px]"
                                disabled={busy !== null}
                                onClick={() => setAdding((prev) => ({...prev, [p.id]: true}))}
                              >
                                <Plus className="mr-1 h-3 w-3" />
                                {t('providers.credAdd')}
                              </Button>
                            )}
                          </div>

                          {credLoading && (
                            <div className="flex items-center gap-1.5 px-1 py-1 text-[11px] text-muted-foreground">
                              <RefreshCw className="h-3 w-3 animate-spin" />
                              {t('common.loading')}
                            </div>
                          )}

                          {!credLoading && credError && (
                            <div className="flex items-center gap-2 px-1 py-1 text-[11px] text-muted-foreground">
                              <AlertCircle className="h-3.5 w-3.5 shrink-0" />
                              <span className="whitespace-normal">{credError}</span>
                              <Button variant="ghost" size="sm" className="h-6 rounded-full text-[11px]" onClick={load}>
                                {t('providers.retry')}
                              </Button>
                            </div>
                          )}

                          {!credLoading && !credError && credsOf(p).length === 0 && (
                            <div className="px-1 py-1 text-[11px] text-muted-foreground">{t('providers.credentialsEmpty')}</div>
                          )}

                          {!credLoading &&
                            !credError &&
                            credsOf(p).map((c) => (
                              <div key={c.id} className="rounded-xl bg-background/60 px-2 py-1.5">
                                {editing === c.id && editDraft ? (
                                  <div className="flex flex-col gap-2">
                                    {credFields(
                                      p,
                                      editDraft,
                                      setEditDraft,
                                      c.has_secret ? c.secret_masked : '',
                                      `provider-secret-${c.id}`,
                                    )}
                                    <div className="flex items-center gap-2 pl-[120px]">
                                      <Button
                                        size="sm"
                                        className="h-7 rounded-full text-[11px]"
                                        disabled={!isAdmin || busy === `cred:${c.id}`}
                                        onClick={() => saveCred(c)}
                                      >
                                        {t('providers.credSave')}
                                      </Button>
                                      {p.id === 'codar' && (
                                        <Button
                                          size="sm"
                                          variant="outline"
                                          className="h-7 rounded-full text-[11px]"
                                          disabled={!isAdmin || busy === `draft:${p.id}` || !editDraft.secret.trim()}
                                          onClick={() => checkToken(p, {token: editDraft.secret})}
                                        >
                                          {t('providers.codarCheckToken')}
                                        </Button>
                                      )}
                                      {p.id === 'codar' && tokenBadge(`draft:${p.id}`)}
                                      <Button
                                        size="sm"
                                        variant="ghost"
                                        className="h-7 rounded-full text-[11px]"
                                        onClick={() => {
                                          setEditing(null);
                                          setEditDraft(null);
                                        }}
                                      >
                                        {t('common.cancel')}
                                      </Button>
                                    </div>
                                  </div>
                                ) : (
                                  <div className="flex items-start gap-2">
                                    <div className="min-w-0 flex-1">
                                      <div className="flex flex-wrap items-center gap-1.5">
                                        <span className="truncate text-xs font-medium">{c.label || `#${c.id}`}</span>
                                        <Badge variant="secondary" className="rounded-full text-[10px]">
                                          {t(c.has_secret ? 'providers.credSecretSet' : 'providers.credSecretUnset')}
                                        </Badge>
                                        {p.id === 'codar' && c.has_secret && tokenBadge(`cred:${c.id}`)}
                                        {c.base_url && (
                                          <span className="truncate font-mono text-[10px] text-muted-foreground">
                                            {c.base_url}
                                          </span>
                                        )}
                                      </div>
                                      <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">
                                        {c.secret_masked || '—'} · {t('providers.credSortOrder')} {c.sort_order}
                                      </div>
                                      {c.note && (
                                        <div className="mt-0.5 whitespace-normal text-[10px] text-muted-foreground">
                                          {c.note}
                                        </div>
                                      )}
                                    </div>
                                    {p.id === 'codar' && c.has_secret && (
                                      <Button
                                        variant="ghost"
                                        size="icon"
                                        className="h-7 w-7 rounded-md"
                                        title={t('providers.codarCheckToken')}
                                        disabled={!isAdmin || busy === `cred:${c.id}`}
                                        onClick={() => checkToken(p, {credentialId: c.id})}
                                      >
                                        <ShieldCheck className="h-3.5 w-3.5" />
                                      </Button>
                                    )}
                                    <Switch
                                      checked={c.enabled}
                                      disabled={!isAdmin || busy === `cred:${c.id}`}
                                      onCheckedChange={(checked) => toggleCred(c, checked)}
                                      aria-label={t('providers.credEnabled')}
                                    />
                                    <Button
                                      variant="ghost"
                                      size="icon"
                                      className="h-7 w-7 rounded-md"
                                      title={t('providers.credEdit')}
                                      disabled={!isAdmin || busy === `cred:${c.id}`}
                                      onClick={() => startEdit(c)}
                                    >
                                      <Pencil className="h-3.5 w-3.5" />
                                    </Button>
                                    <ConfirmDialog
                                      trigger={
                                        <Button
                                          variant="ghost"
                                          size="icon"
                                          className="h-7 w-7 rounded-md text-destructive"
                                          title={t('providers.credDelete')}
                                          disabled={!isAdmin || busy === `cred:${c.id}`}
                                        >
                                          <Trash2 className="h-3.5 w-3.5" />
                                        </Button>
                                      }
                                      title={t('providers.credDeleteTitle', {name: c.label || `#${c.id}`})}
                                      description={t('providers.credDeleteDesc')}
                                      confirmText={t('common.delete')}
                                      destructive
                                      onConfirm={() => deleteCred(c)}
                                    />
                                  </div>
                                )}
                              </div>
                            ))}

                          {adding[p.id] && (
                            <div className="flex flex-col gap-2 rounded-xl bg-background/60 p-2">
                              {credFields(
                                p,
                                addDrafts[p.id] ?? EMPTY_CRED,
                                (next) => setAddDrafts((prev) => ({...prev, [p.id]: next})),
                                '',
                                `provider-secret-new-${p.id}`,
                              )}
                              <div className="flex items-center gap-2 pl-[120px]">
                                <Button
                                  size="sm"
                                  className="h-7 rounded-full text-[11px]"
                                  disabled={!isAdmin || busy === `cred:${p.id}`}
                                  onClick={() => createCred(p)}
                                >
                                  {t('providers.credSave')}
                                </Button>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  className="h-7 rounded-full text-[11px]"
                                  onClick={() => setAdding((prev) => ({...prev, [p.id]: false}))}
                                >
                                  {t('common.cancel')}
                                </Button>
                              </div>
                            </div>
                          )}
                        </div>

                        {/* 注册任务：只挂 grok-register 一家，admin-only（接口本身也只放行 admin） */}
                        {p.id === 'grok-register' && isAdmin && <GrokRegisterTasks />}

                        {/* 会话搬运：只挂启用的 qoder_sidecar，admin-only；未启用时整块不出现 */}
                        {p.id === 'qoder_sidecar' && isAdmin && p.enabled && <QoderSessionsPanel />}

                        {/* 管理台：放在既有展开区，不新增顶层导航；viewer 打开是只读态 */}
                        {CONSOLE_PROVIDERS.has(p.id) && (
                          <ProviderConsolePanel providerId={p.id} providerLabel={p.label} />
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                )}
              </Fragment>
            ))}
          </TableBody>
        </Table>

        {loading && (
          <div className="px-4 py-8 text-center text-xs text-muted-foreground">
            <RefreshCw className="mx-auto mb-2 h-4 w-4 animate-spin" />
            {t('common.loading')}
          </div>
        )}
        {error && !loading && (
          <EmptyState
            icon={AlertCircle}
            title={error}
            description={t('providers.emptyHint')}
            className="flex flex-col items-center justify-center py-10 text-center"
          >
            <Button variant="outline" size="sm" className="rounded-full" onClick={load}>
              <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
              {t('providers.retry')}
            </Button>
          </EmptyState>
        )}
        {!items.length && !loading && !error && (
          <EmptyState
            icon={PlugZap}
            title={t('providers.empty')}
            description={t('providers.emptyHint')}
            className="flex flex-col items-center justify-center py-10 text-center"
          >
            <Button variant="outline" size="sm" className="rounded-full" onClick={load}>
              <RefreshCw className="mr-1.5 h-3.5 w-3.5" />
              {t('providers.retry')}
            </Button>
          </EmptyState>
        )}
        {!isAdmin && items.length > 0 && (
          <div className="px-4 pb-3 text-[11px] text-muted-foreground">{t('providers.requiresAdmin')}</div>
        )}
      </section>

      {/* 定时健康检测：只观测可用性，不改路由/启用状态，也不并入连败告警。
          admin-only：viewer 既看不到按钮，也不发写请求 */}
      {isAdmin && (
        <section className="rounded-[20px] bg-muted px-3.5 py-3">
          <div className="flex items-center justify-between gap-3 rounded-2xl bg-background/60 px-3 py-2">
            <div className="min-w-0">
              <div className="text-xs font-medium">{t('providers.healthTitle')}</div>
              <div className="mt-0.5 text-[11px] leading-4 text-muted-foreground">
                {t('providers.healthDesc', {sec: health?.interval_sec ?? 300})}
              </div>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <Button
                size="sm"
                variant="outline"
                className="h-7 rounded-full text-[11px]"
                disabled={healthBusy}
                onClick={runHealth}
              >
                <RefreshCw className={healthBusy ? 'mr-1 h-3 w-3 animate-spin' : 'mr-1 h-3 w-3'} />
                {healthBusy ? t('providers.healthRunning') : t('providers.healthRun')}
              </Button>
              <Switch
                checked={Boolean(healthEnabled)}
                disabled={healthToggleBusy || healthEnabled === null}
                onCheckedChange={toggleHealthCheck}
                aria-label={t('providers.healthTitle')}
              />
            </div>
          </div>
        </section>
      )}

      {/* 网关鉴权：默认要求 Bearer key；匿名访问仅本机调试时打开。
          这是 admin-only 配置，viewer 不渲染，也不发请求 */}
      {isAdmin && (
        <section className="rounded-[20px] bg-muted px-3.5 py-3">
        <div className="flex items-center justify-between gap-3 rounded-2xl bg-background/60 px-3 py-2">
          <div className="min-w-0">
            <div className="text-xs font-medium">{t('providers.gatewayTitle')}</div>
            <div className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{t('providers.gatewayDesc')}</div>
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
        </div>
        </section>
      )}
    </div>
  );
}
