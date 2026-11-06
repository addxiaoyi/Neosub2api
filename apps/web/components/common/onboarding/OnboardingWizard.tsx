'use client';

/**
 * 首次启动向导：六步走完「账号确认 → 选服务商 → 填凭据 → 自检 → 真请求 → 拿 key」。
 *
 * 每一步都能回退、能跳过；跳过的项只记进库内状态，首页用一条待办提示承接，
 * 不在每次进页面时重复弹窗（本次会话的「稍后再说」只存 sessionStorage）。
 */
import {useEffect, useMemo, useState} from 'react';
import {Check, ChevronLeft, ChevronRight, Copy, Loader2, RefreshCw} from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {ImportAccountDialog} from '@/components/common/accounts/ImportAccountDialog';
import {BASE_PATH} from '@/lib/base-path';
import {errText, http, keyApi, onboardingApi, providerApi, type OnboardingState} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {
  STEP_IDS,
  WIZARD_KEY_NAME,
  notifyOnboardingUpdated,
  snoozeOnboarding,
  type OnboardingStepId,
} from '@/lib/onboarding';
import type {ProviderHealthEntry, ProviderInfo} from '@/lib/providers';
import {notify} from '@/lib/toast';

const DEMO_PROVIDER = 'demo_local';
const DEMO_MODEL = 'fake-chat-1';

/** 0 = 未填；1–4 档只在浏览器本地算，不发送、不保存 */
function passwordScore(value: string): number {
  if (!value) return 0;
  let score = 0;
  if (value.length >= 8) score += 1;
  if (value.length >= 12) score += 1;
  if (/[a-z]/.test(value) && /[A-Z]/.test(value)) score += 1;
  if (/\d/.test(value) && /[^A-Za-z0-9]/.test(value)) score += 1;
  return Math.min(4, Math.max(1, score));
}

type SmokeState = {status: number; body: string} | null;

export function OnboardingWizard({open, onOpenChange}: {open: boolean; onOpenChange: (open: boolean) => void}) {
  const t = useT();
  const {me} = useAuth();
  const [step, setStep] = useState(0);
  const [state, setState] = useState<OnboardingState | null>(null);
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [selected, setSelected] = useState(DEMO_PROVIDER);
  const [pwd, setPwd] = useState('');
  const [importOpen, setImportOpen] = useState(false);
  const [imported, setImported] = useState(false);
  const [health, setHealth] = useState<ProviderHealthEntry | null>(null);
  const [healthBusy, setHealthBusy] = useState(false);
  const [model, setModel] = useState(DEMO_MODEL);
  const [keyValue, setKeyValue] = useState('');
  const [smoke, setSmoke] = useState<SmokeState>(null);
  const [smokeBusy, setSmokeBusy] = useState(false);
  const [busy, setBusy] = useState(false);

  // 每次打开都从库内状态续上一步；中途关掉再进不会丢进度
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      try {
        const [saved, list] = await Promise.all([onboardingApi.get(), providerApi.list()]);
        if (cancelled) return;
        setState(saved);
        setStep(Math.max(0, Math.min(STEP_IDS.length - 1, saved.current_step)));
        setProviders(list);
        // 未开演示模式时库里没有 demo_local，不能把选择停在不存在的上游
        if (!list.some((p) => p.id === DEMO_PROVIDER)) {
          const first = list.find((p) => p.readiness === 'ready' && !p.requires.length) ?? list[0];
          if (first) {
            setSelected(first.id);
            setModel('');
          }
        }
      } catch (error) {
        if (!cancelled) notify.err(errText(error));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open]);

  const ordered = useMemo(() => {
    const rank = (p: ProviderInfo) => (p.id === DEMO_PROVIDER ? 0 : p.readiness === 'ready' && !p.requires.length ? 1 : 2);
    return [...providers].sort((a, b) => rank(a) - rank(b));
  }, [providers]);

  const current = STEP_IDS[step] as OnboardingStepId;
  const skipped = state?.skipped_steps ?? [];
  const provider = providers.find((p) => p.id === selected);
  const isDemo = selected === DEMO_PROVIDER && providers.some((p) => p.id === DEMO_PROVIDER);

  const persist = async (patch: Parameters<typeof onboardingApi.save>[0]) => {
    try {
      const next = await onboardingApi.save(patch);
      setState(next);
      notifyOnboardingUpdated();
      return next;
    } catch (error) {
      notify.err(errText(error));
      return null;
    }
  };

  const goNext = async () => {
    const done = skipped.filter((id) => id !== current);
    await persist({current_step: step + 1, skipped_steps: done});
    setStep(step + 1);
  };

  const goBack = async () => {
    await persist({current_step: step - 1});
    setStep(step - 1);
  };

  const skipStep = async () => {
    const list = skipped.includes(current) ? skipped : [...skipped, current];
    const next = Math.min(step + 1, STEP_IDS.length - 1);
    const saved = await persist({current_step: next, skipped_steps: list});
    if (saved) {
      setStep(next);
      notify.ok(t('onboarding.skipMarked'));
    }
  };

  const close = (next: boolean) => {
    // 先同步落本地免打扰，再异步存进度：否则关窗后立刻跳页会因请求中断又弹回来
    if (!next) {
      snoozeOnboarding();
      void persist({current_step: step});
    }
    onOpenChange(next);
  };

  const runHealth = async () => {
    if (!provider || healthBusy) return;
    setHealthBusy(true);
    setHealth(null);
    try {
      const result = await providerApi.runHealth(provider.id);
      setHealth(result.providers.find((p) => p.provider === provider.id) ?? null);
    } catch (error) {
      setHealth({provider: provider.id, status: 'failed', checked_at: null, latency_ms: null, error: errText(error)});
    } finally {
      setHealthBusy(false);
    }
  };

  const ensureKey = async (): Promise<string> => {
    if (keyValue) return keyValue;
    const created = await keyApi.create({name: WIZARD_KEY_NAME});
    if (!created.key) throw new Error('密钥创建成功但未返回明文');
    setKeyValue(created.key);
    return created.key;
  };

  const runSmoke = async () => {
    if (smokeBusy || !model.trim()) return;
    setSmokeBusy(true);
    setSmoke(null);
    try {
      const key = await ensureKey();
      // 同一站点下打网关：静态导出由反代把 /v1 转到控制面
      const res = await http.post(
        '/v1/chat/completions',
        {model: model.trim(), messages: [{role: 'user', content: 'ping'}]},
        {headers: {Authorization: `Bearer ${key}`}, validateStatus: () => true},
      );
      setSmoke({status: res.status, body: JSON.stringify(res.data).slice(0, 600)});
    } catch (error) {
      setSmoke({status: 0, body: errText(error)});
    } finally {
      setSmokeBusy(false);
    }
  };

  const finish = async () => {
    if (busy) return;
    setBusy(true);
    try {
      await persist({completed: true, current_step: 0, skipped_steps: []});
      notify.ok(t('onboarding.todo.done'));
      onOpenChange(false);
    } finally {
      setBusy(false);
    }
  };

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      notify.ok(t('onboarding.key.copied'));
    } catch (error) {
      notify.err(errText(error));
    }
  };

  const origin = typeof window === 'undefined' ? '' : window.location.origin;
  const curl = [
    `curl -X POST '${origin}${BASE_PATH}/v1/chat/completions' \\`,
    `  -H 'Authorization: Bearer ${keyValue || '<YOUR_KEY>'}' \\`,
    `  -H 'Content-Type: application/json' \\`,
    `  -d '{"model":"${model || DEMO_MODEL}","messages":[{"role":"user","content":"你好"}]}'`,
  ].join('\n');

  const stepBody = () => {
    switch (current) {
      case 'account':
        return (
          <div className="space-y-4">
            <div className="rounded-xl bg-muted p-4 text-sm">
              <div className="flex items-center justify-between py-1">
                <span className="text-muted-foreground">{t('onboarding.account.user')}</span>
                <span className="font-medium">{me?.username ?? '—'}</span>
              </div>
              <div className="flex items-center justify-between py-1">
                <span className="text-muted-foreground">{t('onboarding.account.role')}</span>
                <Badge variant="secondary" className="rounded-full">
                  {me?.role === 'admin' ? t('onboarding.account.admin') : me?.role ?? '—'}
                </Badge>
              </div>
            </div>
            <div className="space-y-2">
              <Label>{t('onboarding.account.passwordLabel')}</Label>
              <Input
                type="password"
                value={pwd}
                onChange={(e) => setPwd(e.target.value)}
                placeholder={t('onboarding.account.passwordHint')}
                autoComplete="off"
              />
              <div className="flex gap-1">
                {[1, 2, 3, 4].map((n) => (
                  <span
                    key={n}
                    className={`h-1.5 flex-1 rounded-full ${
                      passwordScore(pwd) >= n
                        ? n >= 4
                          ? 'bg-emerald-500'
                          : n >= 3
                            ? 'bg-amber-500'
                            : 'bg-red-500'
                        : 'bg-border'
                    }`}
                  />
                ))}
              </div>
              <p className="text-xs text-muted-foreground">
                {t(`onboarding.account.passwordScore_${passwordScore(pwd)}`)} · {t('onboarding.account.passwordLocal')}
              </p>
            </div>
          </div>
        );

      case 'provider':
        return (
          <div className="grid max-h-[46vh] grid-cols-1 gap-2 overflow-y-auto pr-1 sm:grid-cols-2">
            {ordered.map((p) => {
              const active = p.id === selected;
              const ready = p.readiness === 'ready';
              return (
                <button
                  key={p.id}
                  type="button"
                  onClick={() => {
                    setSelected(p.id);
                    setHealth(null);
                    setSmoke(null);
                    setModel(p.id === DEMO_PROVIDER ? DEMO_MODEL : '');
                  }}
                  className={`rounded-xl border p-3 text-left transition-colors ${
                    active ? 'border-foreground/40 bg-muted' : 'border-border hover:bg-muted/60'
                  }`}
                  aria-pressed={active}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="min-w-0 truncate text-sm font-medium">{p.name}</span>
                    {p.id === DEMO_PROVIDER && (
                      <Badge className="shrink-0 rounded-full text-[10px]">{t('onboarding.provider.recommend')}</Badge>
                    )}
                  </div>
                  <div className="mt-1 text-[11px] text-muted-foreground">
                    {ready ? t('onboarding.provider.readyHint') : p.requires.join(t('common.listSeparator'))}
                  </div>
                </button>
              );
            })}
          </div>
        );

      case 'credential':
        return isDemo ? (
          <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-4 text-sm">
            <div className="flex items-center gap-2 font-medium text-emerald-700 dark:text-emerald-400">
              <Check className="h-4 w-4" />
              {t('onboarding.credential.demoNote')}
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            {provider?.readiness === 'needs_config' && (
              <p className="text-xs text-amber-600 dark:text-amber-400">
                {t('onboarding.credential.notReady', {requires: provider.requires.join(t('common.listSeparator'))})}
              </p>
            )}
            <Button variant="outline" onClick={() => setImportOpen(true)}>
              {imported ? t('onboarding.credential.imported') : t('onboarding.credential.openImport')}
            </Button>
          </div>
        );

      case 'selfcheck':
        return (
          <div className="space-y-3">
            <Button variant="outline" onClick={runHealth} disabled={healthBusy || !provider}>
              {healthBusy ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-1 h-4 w-4" />}
              {healthBusy ? t('onboarding.selfcheck.running') : t('onboarding.selfcheck.run')}
            </Button>
            {health && (
              <div
                className={`rounded-md border px-3 py-2 text-xs ${
                  health.status === 'ok'
                    ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400'
                    : 'border-red-500/40 bg-red-500/10 text-red-600 dark:text-red-400'
                }`}
              >
                {health.status === 'ok'
                  ? `${t('onboarding.selfcheck.ok')}${health.latency_ms ? ` · ${health.latency_ms}ms` : ''}`
                  : `${t('onboarding.selfcheck.fail')}${health.error ? ` · ${health.error}` : ''}`}
              </div>
            )}
            {!health && !healthBusy && <p className="text-xs text-muted-foreground">{t('onboarding.selfcheck.none')}</p>}
          </div>
        );

      case 'smoke':
        return (
          <div className="space-y-3">
            <div className="space-y-2">
              <Label>{t('onboarding.smoke.model')}</Label>
              <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder={DEMO_MODEL} />
            </div>
            <Button variant="outline" onClick={runSmoke} disabled={smokeBusy || !model.trim()}>
              {smokeBusy ? <Loader2 className="mr-1 h-4 w-4 animate-spin" /> : <ChevronRight className="mr-1 h-4 w-4" />}
              {smokeBusy ? t('onboarding.smoke.running') : t('onboarding.smoke.run')}
            </Button>
            {smoke && (
              <div className="space-y-2">
                <div
                  className={`text-sm font-medium ${
                    smoke.status === 200 ? 'text-emerald-600 dark:text-emerald-400' : 'text-red-600 dark:text-red-400'
                  }`}
                >
                  {smoke.status === 200
                    ? t('onboarding.smoke.ok', {status: smoke.status})
                    : t('onboarding.smoke.fail', {status: smoke.status || '—'})}
                </div>
                <pre className="max-h-40 min-w-0 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-3 text-[11px] leading-relaxed">{smoke.body}</pre>
              </div>
            )}
          </div>
        );

      default:
        return (
          <div className="space-y-4">
            {keyValue ? (
              <div className="space-y-2">
                <Label>{t('onboarding.key.label')}</Label>
                <div className="flex gap-2">
                  <Input readOnly value={keyValue} className="font-mono text-xs" />
                  <Button variant="outline" onClick={() => copy(keyValue)}>
                    <Copy className="mr-1 h-4 w-4" />
                    {t('onboarding.key.copy')}
                  </Button>
                </div>
                <p className="text-xs text-muted-foreground">{t('onboarding.key.once')}</p>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">{t('onboarding.key.createFirst')}</p>
            )}
            <div className="space-y-2">
              <Label>{t('onboarding.key.curl')}</Label>
              <pre className="min-w-0 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-3 text-[11px] leading-relaxed">{curl}</pre>
              <Button variant="outline" size="sm" onClick={() => copy(curl)}>
                <Copy className="mr-1 h-4 w-4" />
                {t('onboarding.key.copy')}
              </Button>
            </div>
          </div>
        );
    }
  };

  return (
    <>
      <Dialog open={open} onOpenChange={close}>
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{t('onboarding.title')}</DialogTitle>
            <DialogDescription>{t('onboarding.desc')}</DialogDescription>
          </DialogHeader>

          <div className="flex flex-wrap items-center gap-2">
            {STEP_IDS.map((id, index) => {
              const done = index < step || skipped.includes(id);
              const active = index === step;
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => index <= step && setStep(index)}
                  className={`flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] transition-colors ${
                    active
                      ? 'border-foreground/40 bg-foreground text-background'
                      : done
                        ? 'border-emerald-500/40 text-emerald-600 dark:text-emerald-400'
                        : 'border-border text-muted-foreground'
                  }`}
                  title={t(`onboarding.step.${id}.title`)}
                >
                  {completedMark(index, step, skipped, id) ? <Check className="h-3 w-3" /> : <span>{index + 1}</span>}
                  <span className="hidden sm:inline">{t(`onboarding.step.${id}.title`)}</span>
                </button>
              );
            })}
          </div>

          <div className="min-h-[200px] min-w-0 text-sm">{stepBody()}</div>

          <DialogFooter className="flex flex-wrap items-center justify-between gap-2">
            <Button variant="ghost" size="sm" onClick={() => close(false)}>
              {t('onboarding.later')}
            </Button>
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-muted-foreground">{t('onboarding.progress', {n: step + 1, total: STEP_IDS.length})}</span>
              <Button variant="outline" size="sm" onClick={goBack} disabled={step === 0}>
                <ChevronLeft className="mr-1 h-4 w-4" />
                {t('onboarding.prev')}
              </Button>
              <Button variant="ghost" size="sm" onClick={skipStep} disabled={step === STEP_IDS.length - 1}>
                {t('onboarding.skip')}
              </Button>
              {step === STEP_IDS.length - 1 ? (
                <Button size="sm" onClick={finish} disabled={busy}>
                  {busy && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}
                  {t('onboarding.finish')}
                </Button>
              ) : (
                <Button size="sm" onClick={goNext}>
                  {t('onboarding.next')}
                  <ChevronRight className="ml-1 h-4 w-4" />
                </Button>
              )}
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 复用现有导入表单，不复制一套字段与探测逻辑 */}
      <ImportAccountDialog
        open={importOpen}
        onOpenChange={setImportOpen}
        providers={
          provider
            ? [{
                id: provider.id,
                name: provider.name,
                readiness: provider.readiness,
                requires: provider.requires,
                credential_state: provider.credential_state,
                credential_state_reason: provider.credential_state_reason,
              }]
            : []
        }
        defaultProvider={selected}
        onSaved={() => setImported(true)}
      />
    </>
  );
}

function completedMark(index: number, step: number, skipped: string[], id: string): boolean {
  return index < step && !skipped.includes(id);
}
