'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {QRCodeSVG} from 'qrcode.react';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {useRealm} from '@/lib/realm-context';
import {Loader2, CheckCircle2, AlertTriangle, ExternalLink} from 'lucide-react';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {CopyButton, ShareButton} from '@/components/ui/copy-button';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
  DialogHeader,
} from '@/components/animate-ui/radix/dialog';
import {accountApi, errText} from '@/lib/api';

type Phase = 'loading' | 'waiting' | 'success' | 'error';

/**
 * 连续失败几次才打断轮询。
 *
 * 取 3（约 6 秒）：单次抖动/超时不该打断用户，而持续失败必须让用户看到原因
 * —— 否则界面会一直转圈到 5 分钟后再报「已失效」，把真正的故障藏起来。
 */
const POLL_FAIL_LIMIT = 3;

export function OAuthDialog({
  open,
  onOpenChange,
  onSuccess,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  onSuccess?: () => void;
}) {
  const t = useT();
  const [phase, setPhase] = useState<Phase>('loading');
  const {realm, label: realmName} = useRealm();
  const [name, setName] = useState('');
  const [authUrl, setAuthUrl] = useState('');
  const [message, setMessage] = useState('');
  const stateRef = useRef('');
  const timerRef = useRef<number | null>(null);
  const pollingRef = useRef(false);
  const failsRef = useRef(0);
  const tickRef = useRef<(() => void) | null>(null);

  const stopPoll = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (tickRef.current !== null) {
      document.removeEventListener('visibilitychange', tickRef.current);
      tickRef.current = null;
    }
    pollingRef.current = false;
  }, []);

  useEffect(() => {
    if (!open) {
      // 关闭时只清理前端状态，不调后端 cancel（让后端 TTL 自动过期）
      stateRef.current = '';
      stopPoll();
      return;
    }

    // 重置状态
    stopPoll();
    failsRef.current = 0;
    setPhase('loading');
    setMessage(t('oauth.requesting'));
    setAuthUrl('');

    const version = realm === 'global' ? 'intl' : 'cn';
    const currentName = name;  // 捕获当前值

    let cancelled = false;

    (async () => {
      try {
        const data = await accountApi.oauthStart(currentName, version);
        if (cancelled) return;

        stateRef.current = data.id;
        setAuthUrl(data.url);
        setPhase('waiting');
        setMessage(t('oauth.waiting'));

        const tick = async () => {
          if (document.hidden) return;
          if (pollingRef.current) return;
          pollingRef.current = true;
          try {
            const res = await accountApi.oauthPoll(stateRef.current);
            failsRef.current = 0;
            if (res.status === 'success') {
              stopPoll();
              setPhase('success');
              const accountName = res.nickname || res.uid || '';
              setMessage(
                res.updated
                  ? t('oauth.successUpdated', {name: accountName})
                  : t('oauth.success', {name: accountName}),
              );
              notify.ok(
                t('oauth.success', {name: accountName}),
                res.realm === 'global'
                  ? t('oauth.successGlobal')
                  : res.updated
                    ? t('oauth.successToken')
                    : t('oauth.successCheckin'),
              );
              window.dispatchEvent(new Event('workbuddy-manager:accounts-changed'));
              onSuccess?.();
              window.setTimeout(() => onOpenChange(false), 1600);
            } else if (res.status === 'expired') {
              stopPoll();
              setPhase('error');
              setMessage(t('oauth.expired'));
            } else if (res.status === 'invalid') {
              stopPoll();
              setPhase('error');
              setMessage(t('oauth.stateLost'));
            }
          } catch (e) {
            failsRef.current += 1;
            if (failsRef.current >= POLL_FAIL_LIMIT) {
              stopPoll();
              setPhase('error');
              setMessage(errText(e));
            }
          } finally {
            pollingRef.current = false;
          }
        };

        tickRef.current = tick;
        timerRef.current = window.setInterval(tick, 2000);
        document.addEventListener('visibilitychange', tick);
      } catch (e) {
        if (!cancelled) {
          setPhase('error');
          setMessage(errText(e));
        }
      }
    })();

    return () => {
      cancelled = true;
      // 不在 cleanup 调 oauthCancel —— React effect 可能 double-invoke，
      // 会把刚 start 出来的 fid 取消掉。让后端 TTL（300s）自动过期清理。
      stateRef.current = '';
      stopPoll();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, realm]);

  const handleRetry = () => {
    setPhase('loading');
    setMessage(t('oauth.requesting'));
    setAuthUrl('');
    failsRef.current = 0;
    stopPoll();
    const version = realm === 'global' ? 'intl' : 'cn';
    const currentName = name;

    (async () => {
      try {
        const data = await accountApi.oauthStart(currentName, version);
        stateRef.current = data.id;
        setAuthUrl(data.url);
        setPhase('waiting');
        setMessage(t('oauth.waiting'));

        const tick = async () => {
          if (document.hidden) return;
          if (pollingRef.current) return;
          pollingRef.current = true;
          try {
            const res = await accountApi.oauthPoll(stateRef.current);
            failsRef.current = 0;
            if (res.status === 'success') {
              stopPoll();
              setPhase('success');
              const accountName = res.nickname || res.uid || '';
              setMessage(
                res.updated
                  ? t('oauth.successUpdated', {name: accountName})
                  : t('oauth.success', {name: accountName}),
              );
              notify.ok(
                t('oauth.success', {name: accountName}),
                res.realm === 'global'
                  ? t('oauth.successGlobal')
                  : res.updated
                    ? t('oauth.successToken')
                    : t('oauth.successCheckin'),
              );
              window.dispatchEvent(new Event('workbuddy-manager:accounts-changed'));
              onSuccess?.();
              window.setTimeout(() => onOpenChange(false), 1600);
            } else if (res.status === 'expired') {
              stopPoll();
              setPhase('error');
              setMessage(t('oauth.expired'));
            } else if (res.status === 'invalid') {
              stopPoll();
              setPhase('error');
              setMessage(t('oauth.stateLost'));
            }
          } catch (e) {
            failsRef.current += 1;
            if (failsRef.current >= POLL_FAIL_LIMIT) {
              stopPoll();
              setPhase('error');
              setMessage(errText(e));
            }
          } finally {
            pollingRef.current = false;
          }
        };

        tickRef.current = tick;
        timerRef.current = window.setInterval(tick, 2000);
        document.addEventListener('visibilitychange', tick);
      } catch (e) {
        setPhase('error');
        setMessage(errText(e));
      }
    })();
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[420px]" showCloseButton>
        <DialogHeader>
          <DialogTitle>{t('oauth.title', {realm: realmName})}</DialogTitle>
          <DialogDescription>
            {realm === 'global'
              ? t('oauth.descGlobal')
              : t('oauth.descCn')}
          </DialogDescription>
        </DialogHeader>

        <div className="flex w-full min-w-0 flex-col items-center gap-4 px-6 pb-6">
          {/* 账号备注输入 */}
          <div className="w-full space-y-1.5">
            <div className="text-[11px] font-medium">{t('oauth.nameLabel')}</div>
            <Input
              placeholder={t('oauth.namePlaceholder')}
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="h-9 rounded-full text-xs"
            />
          </div>

          {/* 固定尺寸，避免 loading/waiting/error 各阶段弹窗高度跳动 */}
          <div className="grid h-[212px] w-[212px] shrink-0 place-items-center overflow-hidden rounded-2xl bg-white p-3 ring-1 ring-black/5">
            {phase === 'loading' && <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" />}
            {phase === 'error' && <AlertTriangle className="h-7 w-7 text-amber-500" />}
            {(phase === 'waiting' || phase === 'success') && authUrl && (
              <QRCodeSVG value={authUrl} size={188} level="M" />
            )}
          </div>

          {authUrl && (
            <div className="w-full space-y-2">
              <div className="flex w-full items-center gap-1.5 rounded-full bg-muted px-3 py-1.5">
                <ExternalLink className="h-3 w-3 shrink-0 text-muted-foreground" />
                <a
                  href={authUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  title={authUrl}
                  className="min-w-0 flex-1 truncate text-[11px] text-blue-500 hover:underline"
                >
                  {authUrl}
                </a>
              </div>
              <div className="flex flex-wrap items-center justify-center gap-2">
                <CopyButton
                  value={authUrl}
                  size="sm"
                  showLabel
                  label={t('oauth.copyLink')}
                  variant="outline"
                  className="rounded-full"
                />
                <ShareButton
                  title={t('oauth.copyLink')}
                  text={realm === 'global' ? t('oauth.descGlobal') : t('oauth.descCn')}
                  url={authUrl}
                />
              </div>
            </div>
          )}

          <div
            className={
              'flex items-center gap-2 px-2 text-xs ' +
              (phase === 'success' ?
                'text-emerald-500' :
                phase === 'error' ?
                  'text-red-500' :
                  'text-muted-foreground')
            }
          >
            {phase === 'waiting' && <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin" />}
            {phase === 'success' && <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />}
            <span className="text-center">{message}</span>
          </div>

          <div className="flex w-full gap-2">
            <Button variant="outline" className="flex-1 rounded-full" onClick={() => onOpenChange(false)}>
              {t('common.cancel')}
            </Button>
            {phase === 'error' && (
              <Button className="flex-1 rounded-full" onClick={handleRetry}>
                {t('oauth.retry')}
              </Button>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}