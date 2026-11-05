'use client';

import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {AlertTriangle, CheckCircle2, Loader2, RefreshCw, Server, ShieldCheck, ShieldOff, Terminal, X, XCircle} from 'lucide-react';
import {errText, systemApi} from '@/lib/api';
import type {UpdateStatus, UpdateStep} from '@/lib/types';
import {useAuth} from '@/lib/auth-context';
import {notify} from '@/lib/toast';
import {fmtAgo} from '@/lib/format';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Progress} from '@/components/ui/progress';
import {Input} from '@/components/ui/input';
import {RichText} from '@/lib/i18n/rich-text';
import {useT} from '@/lib/i18n/provider';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';

/** 只在 job.state === running 时轮询，完成或组件卸载即停 */
const POLL_MS = 3000;
const EMPTY_LOGS: string[] = [];

const STEP_KEYS: Record<UpdateStep, string> = {
  download: 'updatePanel.stepDownload',
  verify: 'updatePanel.stepVerify',
  stage: 'updatePanel.stepStage',
  stop: 'updatePanel.stepStop',
  swap: 'updatePanel.stepSwap',
  start: 'updatePanel.stepStart',
  health: 'updatePanel.stepHealth',
  rollback: 'updatePanel.stepRollback',
};

export function UpdatePanel() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [checking, setChecking] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [typedVersion, setTypedVersion] = useState('');
  const [applying, setApplying] = useState(false);
  const [rollingBack, setRollingBack] = useState(false);
  /** 结果横幅是否已被关闭；新一轮更新开始时复位 */
  const [resultDismissed, setResultDismissed] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const st = await systemApi.updateStatus();
      setStatus(st);
      setError('');
    } catch (e) {
      // 更新期间服务会重启：轮询的瞬时失败不打断等待，保留上一份状态
      if (!silent) setError(errText(e));
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  const recheck = useCallback(async () => {
    setChecking(true);
    try {
      const st = await systemApi.checkUpdates();
      setStatus(st);
      setError('');
      if (st.available) {
        notify.warn(t('updatePanel.newVersion'), `${st.current_version} → ${st.latest_version}`);
      } else {
        notify.ok(t('updatePanel.upToDate'), t('updatePanel.upToDateDesc'));
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setChecking(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const job = status?.job;
  const jobState = job?.state ?? 'idle';
  const running = jobState === 'running';

  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => {
      void load(true);
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [load, running]);

  // 新一轮更新开始时复位「已关闭」，结束后结果重新可见
  useEffect(() => {
    if (running) setResultDismissed(false);
  }, [running]);

  const logs = job?.log_tail ?? EMPTY_LOGS;
  const logText = useMemo(() => logs.join('\n'), [logs]);

  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [logs]);

  const targetVersion = status?.latest_version ?? '';
  const canConfirm = !applying && targetVersion !== '' && typedVersion.trim() === targetVersion;
  const applyDisabled = !status || !status.can_apply || !status.available || running || applying;
  const tookSeconds =
    job && job.started_at > 0 && job.finished_at > job.started_at ? job.finished_at - job.started_at : 0;

  const applyUpdate = useCallback(async () => {
    if (!canConfirm) return;
    setApplying(true);
    try {
      const st = await systemApi.applyUpdate(typedVersion.trim());
      setStatus(st);
      setConfirmOpen(false);
      setTypedVersion('');
      notify.ok(t('updatePanel.applyStarted'));
      await load(true);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setApplying(false);
    }
  }, [canConfirm, load, t, typedVersion]);

  const rollback = useCallback(async () => {
    setRollingBack(true);
    try {
      const st = await systemApi.rollbackUpdate();
      setStatus(st);
      setResultDismissed(false);
      notify.ok(t('updatePanel.rollbackStarted'));
      await load(true);
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setRollingBack(false);
    }
  }, [load, t]);

  const signatureBadge = () => {
    if (status?.signature === 'verified') {
      return (
        <span
          className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium text-emerald-600 dark:text-emerald-400"
          title={t('updatePanel.signedTitle')}
        >
          <ShieldCheck className="h-3 w-3" />
          {t('updatePanel.signed')}
        </span>
      );
    }
    if (status?.signature === 'unsigned') {
      return (
        <span
          className="inline-flex items-center gap-1 rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-warning-text"
          title={t('updatePanel.unsignedTitle')}
        >
          <ShieldOff className="h-3 w-3" />
          {t('updatePanel.unsigned')}
        </span>
      );
    }
    if (status?.signature === 'invalid') {
      return (
        <span
          className="inline-flex items-center gap-1 rounded-full bg-red-500/15 px-1.5 py-0.5 text-[10px] font-medium text-destructive-text"
          title={t('updatePanel.invalidSignatureTitle')}
        >
          <XCircle className="h-3 w-3" />
          {t('updatePanel.invalidSignature')}
        </span>
      );
    }
    return null;
  };

  return (
    <div className="space-y-4">
      {/* 版本信息 */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Server className="h-4 w-4" />
            {t('updatePanel.currentVersion')}
          </div>
          <div className="flex items-center gap-2">
            {isAdmin && (
              <Button
                size="sm"
                className="rounded-full"
                disabled={applyDisabled}
                title={!status?.can_apply ? status?.reason : undefined}
                onClick={() => {
                  setTypedVersion('');
                  setConfirmOpen(true);
                }}
              >
                {t('updatePanel.applyUpdate')}
              </Button>
            )}
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              onClick={recheck}
              disabled={checking || running}
            >
              <RefreshCw className={checking ? 'animate-spin' : ''} />
              {t('updatePanel.checkUpdate')}
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              onClick={() => void load()}
              disabled={loading || running}
            >
              <RefreshCw className={loading ? 'animate-spin' : ''} />
              {t('common.refresh')}
            </Button>
          </div>
        </div>

        {loading && !status ? (
          <div className="flex items-center gap-2 py-6 text-xs text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" />
            {t('common.loading')}
          </div>
        ) : !status && error ? (
          <div className="flex items-start gap-2.5 rounded-2xl border border-destructive/30 bg-destructive/5 px-3.5 py-3">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive-text" />
            <div className="min-w-0 flex-1">
              <div className="text-xs font-medium">{t('updatePanel.loadFailed')}</div>
              <div className="mt-1 whitespace-pre-wrap break-words text-[11px] text-muted-foreground">
                {error}
              </div>
              <Button
                size="sm"
                variant="outline"
                className="mt-2 rounded-full"
                onClick={() => void load()}
              >
                <RefreshCw className="h-3.5 w-3.5" />
                {t('common.retry')}
              </Button>
            </div>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <div className="rounded-2xl bg-background/60 px-3.5 py-3">
              <div className="text-[11px] text-muted-foreground">{t('updatePanel.currentVersion')}</div>
              <div className="mt-1 text-sm font-semibold tabular-nums">
                {status?.current_version || '—'}
              </div>
            </div>
            <div className="rounded-2xl bg-background/60 px-3.5 py-3">
              <div className="text-[11px] text-muted-foreground">{t('updatePanel.latestVersion')}</div>
              <div className="mt-1 flex flex-wrap items-center gap-1.5 text-sm font-semibold tabular-nums">
                {status?.latest_version || '—'}
                {signatureBadge()}
              </div>
            </div>
            <div className="rounded-2xl bg-background/60 px-3.5 py-3">
              <div className="text-[11px] text-muted-foreground">{t('updatePanel.channel')}</div>
              <div className="mt-1 text-sm font-semibold">{status?.channel || '—'}</div>
            </div>
            <div className="rounded-2xl bg-background/60 px-3.5 py-3">
              <div className="text-[11px] text-muted-foreground">{t('updatePanel.lastUpdate')}</div>
              <div className="mt-1 text-sm font-semibold">
                {job && job.finished_at > 0 ? fmtAgo(job.finished_at) : t('format.never')}
              </div>
            </div>
          </div>
        )}
      </div>

      {/* 本部署无法执行更新：原因原样展示，按钮已在上面禁用 */}
      {status && (!status.updater_found || !status.can_apply) && (
        <div className="flex items-start gap-2.5 rounded-[20px] border border-amber-500/30 bg-amber-500/10 p-4">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning-text" />
          <div className="min-w-0 flex-1 space-y-1">
            <div className="text-xs font-medium">{t('updatePanel.updateNotAvailable')}</div>
            {status.reason && (
              <div className="whitespace-pre-wrap break-words text-[11px] leading-5 text-muted-foreground">
                {status.reason}
              </div>
            )}
          </div>
        </div>
      )}

      {/* 无可用更新（空态） */}
      {status && !status.available && !running && jobState !== 'failed' && (jobState === 'idle' || resultDismissed) && (
        <div className="flex items-center gap-2 rounded-[20px] border border-emerald-500/30 bg-emerald-500/10 px-4 py-3">
          <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />
          <span className="text-xs">{t('updatePanel.allUpToDate')}</span>
        </div>
      )}

      {/* 执行中：阶段名 + 进度 */}
      {running && job && (
        <div className="space-y-2.5 rounded-[20px] border border-blue-500/30 bg-blue-500/10 p-4">
          <div className="flex items-start gap-2.5">
            <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-blue-500" />
            <div className="min-w-0 flex-1 space-y-1">
              <div className="text-xs font-medium">
                {t('updatePanel.updatingNow', {
                  step: job.step ? t(STEP_KEYS[job.step]) : t('updatePanel.stepRunning'),
                })}
              </div>
              <div className="text-[11px] text-muted-foreground">{t('updatePanel.updatingHint')}</div>
            </div>
            <div className="shrink-0 text-[11px] font-medium tabular-nums text-muted-foreground">
              {t('updatePanel.progress', {n: Math.max(0, Math.min(100, job.progress))})}
            </div>
          </div>
          <Progress value={Math.max(0, Math.min(100, job.progress))} />
        </div>
      )}

      {/* 结果：成功展示新版本号，失败展示 error + 回滚 */}
      {!resultDismissed && (jobState === 'done' || jobState === 'failed') && (
        <div
          className={
            'flex items-start gap-2.5 rounded-[20px] border p-4 ' +
            (jobState === 'done'
              ? 'border-emerald-500/30 bg-emerald-500/10'
              : 'border-red-500/30 bg-red-500/10')
          }
        >
          {jobState === 'done' ? (
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
          ) : (
            <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive-text" />
          )}
          <div className="min-w-0 flex-1 space-y-1">
            <div className="text-xs font-medium">
              {jobState === 'done' ? t('updatePanel.updateDone') : t('updatePanel.updateNotDone')}
            </div>
            {jobState === 'done' ? (
              <>
                <div className="text-[11px] leading-5 text-muted-foreground">
                  {status?.current_version
                    ? `${t('updatePanel.updateDoneTo', {version: status.current_version})} `
                    : ''}
                  {tookSeconds > 0 ? t('updatePanel.doneDuration', {n: tookSeconds}) : ''}
                  {t('updatePanel.doneRestart')}
                </div>
                {status?.signature === 'verified' && (
                  <div className="text-[11px] text-muted-foreground">
                    {t('updatePanel.doneSigned')}
                  </div>
                )}
                {(status?.signature === 'unsigned' || status?.signature === 'invalid') && (
                  <div className="flex items-start gap-1.5 pt-1 text-[11px] text-warning-text">
                    <ShieldOff className="mt-0.5 h-3 w-3 shrink-0" />
                    <span>
                      <RichText text={t('updatePanel.skippedWarn')} />
                    </span>
                  </div>
                )}
              </>
            ) : (
              <>
                {job?.error && (
                  <div className="whitespace-pre-wrap break-words text-[11px] leading-5 text-muted-foreground">
                    {job.error}
                  </div>
                )}
                <div className="text-[11px] leading-5 text-muted-foreground">
                  {t('updatePanel.failedDetail')}
                </div>
                {isAdmin && (
                  <Button
                    size="sm"
                    variant="outline"
                    className="mt-1.5 rounded-full"
                    disabled={rollingBack}
                    onClick={() => void rollback()}
                  >
                    {rollingBack ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : null}
                    {rollingBack ? t('updatePanel.rollingBack') : t('updatePanel.rollback')}
                  </Button>
                )}
              </>
            )}
          </div>
          <button
            type="button"
            aria-label={t('updatePanel.closeTip')}
            title={t('updatePanel.closeTip')}
            className="-m-1 shrink-0 rounded-full p-1 text-muted-foreground transition-colors hover:bg-foreground/10 hover:text-foreground"
            onClick={() => setResultDismissed(true)}
          >
            <X className="h-4 w-4" />
          </button>
        </div>
      )}

      {/* 日志 */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-2 flex items-center justify-between">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Terminal className="h-4 w-4" />
            {t('updatePanel.logTitle')}
          </div>
          {logs.length > 0 && (
            <Badge variant="secondary" className="rounded-full text-[10px]">
              {t('updatePanel.logLines', {count: logs.length, n: logs.length})}
            </Badge>
          )}
        </div>
        {logText ? (
          <div
            ref={logRef}
            className="scroll-slim max-h-[320px] overflow-auto rounded-2xl bg-background/60 p-3"
          >
            <pre className="whitespace-pre-wrap break-all font-mono text-[11px] leading-5 text-muted-foreground">
              {logText}
            </pre>
          </div>
        ) : (
          <div className="rounded-2xl bg-background/60 px-3 py-8 text-center text-xs text-muted-foreground">
            {t('updatePanel.logEmpty')}
          </div>
        )}
      </div>

      {/* 执行更新前的二次确认：必须手动输入目标版本号 */}
      <AlertDialog
        open={confirmOpen}
        onOpenChange={(open) => {
          if (!open && !applying) {
            setConfirmOpen(false);
            setTypedVersion('');
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('updatePanel.confirmTitle')}</AlertDialogTitle>
            <AlertDialogDescription className="space-y-2">
              <span className="block">{t('updatePanel.confirmWarn')}</span>
              <span className="block break-all font-mono text-[11.5px] text-foreground">
                {status?.current_version || '—'} → {targetVersion || '—'}
              </span>
              {status?.in_container && (
                <span className="block font-medium text-warning-text">
                  {t('updatePanel.confirmRestart')}
                </span>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="mt-1 space-y-1.5">
            <label
              htmlFor="update-confirm-version"
              className="block text-[11.5px] leading-5 text-muted-foreground"
            >
              {t('updatePanel.typeVersion', {version: targetVersion})}
            </label>
            <Input
              id="update-confirm-version"
              autoFocus
              autoComplete="off"
              value={typedVersion}
              onChange={(e) => setTypedVersion(e.target.value)}
              placeholder={targetVersion}
              disabled={applying}
              className="bg-background"
            />
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={applying} onClick={() => setTypedVersion('')}>
              {t('common.cancel')}
            </AlertDialogCancel>
            <AlertDialogAction
              disabled={!canConfirm}
              onClick={(e) => {
                // 更新请求会重启服务，不能交给 Radix 自动关闭
                e.preventDefault();
                if (canConfirm) void applyUpdate();
              }}
            >
              {applying ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
              {applying ? t('updatePanel.applyingUpdate') : t('updatePanel.confirmApply')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
