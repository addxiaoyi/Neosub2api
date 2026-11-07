'use client';

import {useCallback, useEffect, useState} from 'react';
import {ShieldAlert, ShieldCheck, TriangleAlert} from 'lucide-react';
import {errText, metricsApi, securityApi} from '@/lib/api';
import type {CryptoBackend, RateLimitSnapshot, SecretAtRest} from '@/lib/api';
import {notify} from '@/lib/toast';
import {useI18n} from '@/lib/i18n/provider';
import {RichText} from '@/lib/i18n/rich-text';

/**
 * 环境变量驱动的两块能力（限流 / 落库加密）只做展示与自检。
 * 两者都在 admin-only 的「运行参数」页签里，viewer 看不到，避免露出会 403 的按钮。
 */
export function SecurityPostureSection({isAdmin}: {isAdmin: boolean}) {
  const {t} = useI18n();
  const [rate, setRate] = useState<RateLimitSnapshot | null>(null);
  const [atRest, setAtRest] = useState<SecretAtRest | null>(null);
  const [rateFailed, setRateFailed] = useState(false);
  const [atRestFailed, setAtRestFailed] = useState(false);

  const load = useCallback(async () => {
    const [rateReply, atRestReply] = await Promise.allSettled([
      metricsApi.json(60),
      securityApi.secretAtRest(),
    ]);

    if (rateReply.status === 'fulfilled') {
      setRate(rateReply.value.runtime?.rate_limit ?? null);
      setRateFailed(false);
    } else {
      setRateFailed(true);
      notify.err(t('settings.securityPosture.readRateFailed'), errText(rateReply.reason));
    }

    if (atRestReply.status === 'fulfilled') {
      setAtRest(atRestReply.value);
      setAtRestFailed(false);
    } else {
      setAtRestFailed(true);
      notify.err(t('settings.securityPosture.readAtRestFailed'), errText(atRestReply.reason));
    }
  }, [t]);

  useEffect(() => {
    if (!isAdmin) return;
    void load();
  }, [isAdmin, load]);

  if (!isAdmin) return null;

  const gate = (limit: number) => (
    <span className="rounded-full border px-1.5 py-0.5 text-[10px] text-muted-foreground">
      {t(limit > 0 ? 'settings.securityPosture.enabled' : 'settings.securityPosture.disabled')}
    </span>
  );

  const keyStateText = (state: SecretAtRest['key_state']) => {
    if (state === 'ok') return t('settings.securityPosture.keyOk');
    if (state === 'invalid') return t('settings.securityPosture.keyInvalid');
    return t('settings.securityPosture.keyMissing');
  };

  const plaintextTotal = atRest
    ? atRest.credentials_plaintext + atRest.api_keys_plaintext
    : 0;

  const cryptoRow = (label: string, backend: CryptoBackend) => (
    <div className="rounded-2xl bg-background/60 px-3 py-2">
      <div className="flex items-center justify-between gap-3">
        <span className="text-[11px] font-medium">{label}</span>
        <span className="shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] text-muted-foreground">
          {backend.implementation === 'lib'
            ? t('settings.securityPosture.cryptoLib', {version: backend.library_version || '-'})
            : t('settings.securityPosture.cryptoStdlib')}
        </span>
      </div>
      {backend.implementation !== 'lib' && backend.reason && (
        <div className="mt-1 text-[11px] leading-relaxed text-warning-text">
          {t('settings.securityPosture.cryptoReason', {reason: backend.reason})}
        </div>
      )}
    </div>
  );

  return (
    <div className="space-y-3">
      <div className="rounded-[20px] bg-muted px-3.5 py-3">
        <div className="mb-2">
          <div className="text-sm font-medium">{t('settings.securityPosture.rateLimitTitle')}</div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {t('settings.securityPosture.rateLimitDesc')}
          </div>
        </div>

        {rate === null ? (
          <div className="text-xs text-muted-foreground">
            {t(rateFailed ? 'settings.securityPosture.readRateFailed' : 'common.loading')}
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-1.5 xl:grid-cols-2">
            <div className="rounded-2xl bg-background/60 px-3 py-2">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-xs font-medium">
                      {t('settings.securityPosture.rpmLabel')}
                    </span>
                    {gate(rate.requests_limit)}
                  </div>
                </div>
                <span className="font-mono text-base tabular-nums text-foreground">
                  {rate.requests_limit}
                </span>
              </div>
            </div>

            <div className="rounded-2xl bg-background/60 px-3 py-2">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="text-xs font-medium">
                      {t('settings.securityPosture.concurrencyLabel')}
                    </span>
                    {gate(rate.concurrency_limit)}
                  </div>
                </div>
                <span className="font-mono text-base tabular-nums text-foreground">
                  {rate.concurrency_limit}
                </span>
              </div>
            </div>
          </div>
        )}

        <div className="mt-2 flex items-start gap-1.5 rounded-2xl border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[11px] leading-relaxed text-warning-text">
          <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{t('settings.securityPosture.workerNote')}</span>
        </div>
      </div>

      <div className="rounded-[20px] bg-muted px-3.5 py-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-sm font-medium">{t('settings.securityPosture.strictTitle')}</div>
            <div className="mt-0.5 text-xs text-muted-foreground">
              {t('settings.securityPosture.strictDesc')}
            </div>
          </div>
          {atRest === null ? null : (
            <span className="shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {t(atRest.required ? 'settings.securityPosture.strictOn' : 'settings.securityPosture.strictOff')}
            </span>
          )}
        </div>
        <div className="mt-1.5 text-[11px] leading-relaxed text-muted-foreground">
          <RichText text={t('settings.securityPosture.strictEnvNote')} />
        </div>
      </div>

      <div className="rounded-[20px] bg-muted px-3.5 py-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="text-sm font-medium">{t('settings.securityPosture.atRestTitle')}</div>
          </div>
          {atRest === null ? null : (
            <span className="shrink-0 rounded-full border px-1.5 py-0.5 text-[10px] text-muted-foreground">
              {t(
                atRest.mode === 'encrypted'
                  ? 'settings.securityPosture.modeEncrypted'
                  : 'settings.securityPosture.modePlaintext',
              )}
            </span>
          )}
        </div>

        {atRest === null ? (
          <div className="mt-2 text-xs text-muted-foreground">
            {t(atRestFailed ? 'settings.securityPosture.readAtRestFailed' : 'common.loading')}
          </div>
        ) : (
          <>
            <div className="mt-2 flex items-center gap-1.5 text-[11px] text-muted-foreground">
              {atRest.key_configured ? (
                <ShieldCheck className="h-3.5 w-3.5" />
              ) : (
                <ShieldAlert className="h-3.5 w-3.5 text-warning-text" />
              )}
              <span>{keyStateText(atRest.key_state)}</span>
            </div>

            <div className="mt-1.5 grid grid-cols-1 gap-1.5 xl:grid-cols-2">
              <div className="rounded-2xl bg-background/60 px-3 py-2">
                <div className="text-[11px] font-medium">
                  {t('settings.securityPosture.credLabel')}
                </div>
                <div className="mt-0.5 font-mono text-[11px] tabular-nums text-muted-foreground">
                  {t('settings.securityPosture.sealedOfTotal', {
                    sealed: atRest.credentials_sealed,
                    total: atRest.credentials_total,
                  })}
                </div>
                {atRest.credentials_plaintext > 0 && (
                  <div className="mt-0.5 text-[11px] font-medium text-destructive-text">
                    {t('settings.securityPosture.plaintextLeft', {
                      count: atRest.credentials_plaintext,
                    })}
                  </div>
                )}
              </div>

              <div className="rounded-2xl bg-background/60 px-3 py-2">
                <div className="text-[11px] font-medium">
                  {t('settings.securityPosture.apiKeyLabel')}
                </div>
                <div className="mt-0.5 font-mono text-[11px] tabular-nums text-muted-foreground">
                  {t('settings.securityPosture.sealedOfTotal', {
                    sealed: atRest.api_keys_sealed,
                    total: atRest.api_keys_total,
                  })}
                </div>
                {atRest.api_keys_plaintext > 0 && (
                  <div className="mt-0.5 text-[11px] font-medium text-destructive-text">
                    {t('settings.securityPosture.plaintextLeft', {count: atRest.api_keys_plaintext})}
                  </div>
                )}
              </div>
            </div>

            {atRest.undecryptable > 0 && (
              <div className="mt-2 flex items-start gap-1.5 rounded-2xl border border-destructive/30 px-3 py-2 text-[11px] leading-relaxed text-destructive-text">
                <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span>{t('settings.securityPosture.undecryptable', {count: atRest.undecryptable})}</span>
              </div>
            )}

            {(!atRest.key_configured || plaintextTotal > 0) && (
              <div className="mt-2 rounded-2xl border border-border/60 bg-background/60 px-3 py-2">
                <div className="text-[11px] font-medium">{t('settings.securityPosture.fixTitle')}</div>
                <div className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
                  <RichText text={t('settings.securityPosture.fixDesc')} />
                </div>
              </div>
            )}
          </>
        )}
      </div>

      <div className="rounded-[20px] bg-muted px-3.5 py-3">
        <div className="text-sm font-medium">
          {t('settings.securityPosture.cryptoTitle')}
        </div>
        <div className="mt-0.5 text-xs text-muted-foreground">
          {t('settings.securityPosture.cryptoDesc')}
        </div>

        {atRest === null ? (
          <div className="mt-2 text-xs text-muted-foreground">
            {t(atRestFailed ? 'settings.securityPosture.readAtRestFailed' : 'common.loading')}
          </div>
        ) : (
          <div className="mt-2 grid grid-cols-1 gap-1.5 xl:grid-cols-2">
            {cryptoRow(
              t('settings.securityPosture.cryptoSigning'),
              atRest.crypto_backends.manifest_signing,
            )}
            {cryptoRow(
              t('settings.securityPosture.cryptoBackup'),
              atRest.crypto_backends.backup_encryption,
            )}
          </div>
        )}
      </div>
    </div>
  );
}
