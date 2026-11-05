'use client';

import {useCallback, useEffect, useState} from 'react';
import {Loader2, ShieldCheck, ShieldOff} from 'lucide-react';
import {authApi, errText} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {CopyButton} from '@/components/ui/copy-button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';

type Setup = {secret: string; otpauth_uri: string};

/**
 * 个人信息里的 TOTP 两步验证面板。
 *
 * 密钥只在 setup 时下发一次，恢复码只在 enable 时下发一次；面板不提供
 * 「查看已存密钥」，因为服务端也不回读 secret，避免把已生效的第二因子
 * 变成可被网页随手复读的字段。
 */
export function TotpSection() {
  const t = useT();
  const [enabled, setEnabled] = useState(false);
  const [left, setLeft] = useState(0);
  const [busy, setBusy] = useState(false);
  const [mode, setMode] = useState<'idle' | 'setup' | 'disable'>('idle');
  const [setup, setSetup] = useState<Setup | null>(null);
  const [codes, setCodes] = useState<string[] | null>(null);
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      const status = await authApi.totpStatus();
      setEnabled(status.enabled);
      setLeft(status.recovery_codes_left);
    } catch (e) {
      notify.err(errText(e));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function reset() {
    setMode('idle');
    setSetup(null);
    setCode('');
    setPassword('');
    setError('');
  }

  async function startSetup() {
    setBusy(true);
    setError('');
    try {
      setSetup(await authApi.totpSetup());
      setMode('setup');
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function enable(e: React.FormEvent) {
    e.preventDefault();
    if (busy || !code.trim()) return;
    setBusy(true);
    setError('');
    try {
      const res = await authApi.totpEnable(code.trim());
      setCodes(res.recovery_codes);
      setEnabled(true);
      reset();
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  }

  async function disable(e: React.FormEvent) {
    e.preventDefault();
    if (busy || !password || !code.trim()) return;
    setBusy(true);
    setError('');
    try {
      await authApi.totpDisable(password, code.trim());
      setEnabled(false);
      setLeft(0);
      reset();
      notify.ok(t('profile.totpDisabled'));
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  }

  if (codes) {
    return (
      <div className="space-y-3">
        <div className="text-[11px] font-medium text-muted-foreground">{t('profile.totpTitle')}</div>
        <div className="rounded-[16px] bg-muted p-4">
          <div className="text-sm font-medium">{t('profile.totpRecoveryTitle')}</div>
          <div className="mt-1 text-[11px] leading-5 text-muted-foreground">
            {t('profile.totpRecoveryDesc')}
          </div>
          {/* 等宽网格随内容长高，不做内部滚动：最后一行不能被「我已保存」压住 */}
          <div className="mt-3 grid grid-cols-2 gap-2 font-mono text-xs">
            {codes.map((item) => (
              <span
                key={item}
                className="flex min-w-0 items-center justify-center rounded-md bg-background px-2 py-2 text-center tracking-wider"
              >
                {item}
              </span>
            ))}
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            {/* 恢复码是敏感信息：只进剪贴板，不写任何日志 */}
            <CopyButton
              value={codes.join('\n')}
              label={t('profile.totpCopyAll')}
              showLabel
              size="sm"
              variant="outline"
              className="rounded-full"
            />
            <Button size="sm" className="rounded-full" onClick={() => setCodes(null)}>
              {t('profile.totpRecoverySaved')}
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="text-[11px] font-medium text-muted-foreground">{t('profile.totpTitle')}</div>
      <div className="rounded-[16px] bg-muted p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="text-sm font-medium">{t('profile.totpName')}</span>
              <Badge variant="secondary" className="h-5 rounded-full px-2 text-[10px]">
                {enabled ? t('profile.totpOn') : t('profile.totpOff')}
              </Badge>
            </div>
            <div className="mt-1 text-[11px] leading-5 text-muted-foreground">
              {enabled ? t('profile.totpOnDesc') : t('profile.totpOffDesc')}
            </div>
            {enabled && (
              <div className="mt-1 text-[11px] text-muted-foreground">
                {t('profile.totpRecoveryLeft')}: {left}
              </div>
            )}
          </div>
          {mode === 'idle' && (
            <Button
              variant="ghost"
              size="sm"
              className="h-8 shrink-0 rounded-full"
              disabled={busy}
              onClick={enabled ? () => setMode('disable') : startSetup}
            >
              {busy ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : enabled ? (
                <ShieldOff className="size-3.5" />
              ) : (
                <ShieldCheck className="size-3.5" />
              )}
              {enabled ? t('profile.totpDisable') : t('profile.totpEnable')}
            </Button>
          )}
        </div>

        {mode === 'setup' && setup && (
          <form onSubmit={enable} className="mt-4 space-y-3">
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">{t('profile.totpSecret')}</Label>
              <div className="flex items-center gap-2">
                <Input readOnly value={setup.secret} className="bg-background font-mono text-xs" />
                <CopyButton value={setup.secret} />
              </div>
            </div>
            <div className="space-y-1.5">
              <Label className="text-[11px] text-muted-foreground">{t('profile.totpUri')}</Label>
              <div className="break-all rounded-md bg-background px-2 py-1.5 font-mono text-[10px] text-muted-foreground">
                {setup.otpauth_uri}
              </div>
            </div>
            <div className="text-[11px] leading-5 text-muted-foreground">{t('profile.totpHint')}</div>
            <div className="space-y-1.5">
              <Label htmlFor="totp-enable-code" className="text-[11px] text-muted-foreground">
                {t('profile.totpCode')}
              </Label>
              <Input
                id="totp-enable-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                inputMode="numeric"
                autoComplete="one-time-code"
                placeholder="123456"
                className="bg-background font-mono tracking-[0.2em]"
              />
            </div>
            {error && <p role="alert" className="text-[11px] text-destructive-text">{error}</p>}
            <div className="flex gap-2">
              <Button type="submit" size="sm" className="rounded-full" disabled={busy}>
                {busy && <Loader2 className="size-3.5 animate-spin" />}
                {t('profile.totpVerifyEnable')}
              </Button>
              <Button type="button" variant="ghost" size="sm" className="rounded-full" onClick={reset}>
                {t('common.cancel')}
              </Button>
            </div>
          </form>
        )}

        {mode === 'disable' && (
          <form onSubmit={disable} className="mt-4 space-y-3">
            <div className="text-[11px] leading-5 text-muted-foreground">{t('profile.totpDisableDesc')}</div>
            <div className="space-y-1.5">
              <Label htmlFor="totp-disable-password" className="text-[11px] text-muted-foreground">
                {t('profile.totpPassword')}
              </Label>
              <Input
                id="totp-disable-password"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="bg-background"
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="totp-disable-code" className="text-[11px] text-muted-foreground">
                {t('profile.totpCode')}
              </Label>
              <Input
                id="totp-disable-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                inputMode="numeric"
                autoComplete="one-time-code"
                placeholder="123456"
                className="bg-background font-mono tracking-[0.2em]"
              />
            </div>
            {error && <p role="alert" className="text-[11px] text-destructive-text">{error}</p>}
            <div className="flex gap-2">
              <Button type="submit" size="sm" variant="destructive" className="rounded-full" disabled={busy}>
                {busy && <Loader2 className="size-3.5 animate-spin" />}
                {t('profile.totpDisableConfirm')}
              </Button>
              <Button type="button" variant="ghost" size="sm" className="rounded-full" onClick={reset}>
                {t('common.cancel')}
              </Button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
