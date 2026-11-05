'use client';

import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {AlertTriangle, CheckCircle2, Loader2, RefreshCw, Server, ShieldCheck, ShieldOff, Terminal, X, XCircle} from 'lucide-react';
import {errText, systemApi} from '@/lib/api';
import type {UpdateCheck, UpdateStatus} from '@/lib/types';
import {notify} from '@/lib/toast';
import {fmtAgo} from '@/lib/format';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {RichText} from '@/lib/i18n/rich-text';
import {useT} from '@/lib/i18n/provider';

/** 更新进行中 1.5 秒一拉，日志跟得上；空闲 15 秒一拉，避免无意义请求（更新期间服务会短暂不可用） */
const RUNNING_POLL_MS = 1500;
const IDLE_POLL_MS = 15000;

export function UpdatePanel() {
  const t = useT();
  const [status, setStatus] = useState<UpdateStatus | null>(null);
  const [versions, setVersions] = useState<{manager: string; upstream_connected: boolean; upstream_accounts: number | null} | null>(null);
  const [check, setCheck] = useState<UpdateCheck | null>(null);
  const [checking, setChecking] = useState(false);
  /**
   * 结果横幅是否已被关闭。用「是否正在运行」的边沿来复位：
   * 状态从 running 变为结束、或再次发起更新时会重新出现，
   * 不依赖 finished_at 是否存在，避免时间戳缺失时关不掉。
   */
  const [resultDismissed, setResultDismissed] = useState(false);
  const logRef = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    try {
      const [st, vs, ck] = await Promise.allSettled([
        systemApi.updateStatus(),
        systemApi.versions(),
        systemApi.checkUpdate(),
      ]);
      if (st.status === 'fulfilled') setStatus(st.value);
      if (vs.status === 'fulfilled') setVersions(vs.value);
      if (ck.status === 'fulfilled') setCheck(ck.value);
    } catch {
      /* 服务重启时忽略瞬时失败 */
    }
  }, []);

  /** 强制重新检测（绕过服务端 6 小时缓存） */
  const recheck = useCallback(async () => {
    setChecking(true);
    try {
      const r = await systemApi.checkUpdate(true);
      setCheck(r);
      if (r.available === false) {
        // 本部署没有在线更新能力：has_any 恒 false，不能据此弹绿色「已是最新」骗用户。
        notify.warn(
          t('updatePanel.updateNotAvailable'),
          t('updatePanel.updateNotAvailableDesc', {reason: r.reason || ''}),
        );
      } else if (r.has_any) {
        const parts: string[] = [];
        if (r.manager.has_update) parts.push(t('update.managerVersion', {v: r.manager.latest}));
        if (r.upstream.has_update) parts.push(t('update.upstreamVersion', {v: r.upstream.latest}));
        notify.warn(t('updatePanel.newVersion'), parts.join(' · '));
      } else {
        notify.ok(t('updatePanel.upToDate'), t('updatePanel.upToDateDesc'));
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // 运行中高频轮询，空闲时低频（服务可能在更新中短暂不可用）
  const running = !!status?.running;
  useEffect(() => {
    const interval = running ? RUNNING_POLL_MS : IDLE_POLL_MS;
    const timer = window.setInterval(() => {
      void load();
    }, interval);
    return () => window.clearInterval(timer);
  }, [load, running]);

  // running 一旦变为 true，就复位「已关闭」，使本轮结果在结束后重新可见
  useEffect(() => {
    if (running) setResultDismissed(false);
  }, [running]);

  // 日志自动滚到底
  useEffect(() => {
    if (logRef.current) {
      logRef.current.scrollTop = logRef.current.scrollHeight;
    }
  }, [status?.logs]);

  const logs = status?.logs ?? [];
  const logText = useMemo(
    () => (logs.length ? logs.map((l) => l.text).join('\n') : status?.log_tail || ''),
    [logs, status?.log_tail],
  );

  const done = !!status && !status.running && status.ok !== null && !resultDismissed;

  return (
    <div className="space-y-4">
      {/* 当前版本 */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2 text-sm font-medium">
            <Server className="h-4 w-4" />
            {t('updatePanel.currentVersion')}
          </div>
          <div className="flex items-center gap-2">
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
            <Button variant="outline" size="sm" className="rounded-full" onClick={load} disabled={running}>
              <RefreshCw className={running ? 'animate-spin' : ''} />
              {t('common.refresh')}
            </Button>
          </div>
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
          <div className="rounded-2xl bg-background/60 px-3.5 py-3">
            <div className="text-[11px] text-muted-foreground">{t('updatePanel.manager')}</div>
            <div className="mt-1 flex items-center gap-1.5 text-sm font-semibold tabular-nums">
              {versions?.manager || status?.version || '—'}
              {/* 供应链防护：本次更新的包是否经过签名校验，必须让用户看得见 */}
              {status?.signature?.status === 'verified' && (
                <span
                  className="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium text-emerald-600 dark:text-emerald-400"
                  title={
                    status.signature.detail
                      ? t('updatePanel.signedTitleDetail', {detail: status.signature.detail})
                      : t('updatePanel.signedTitle')
                  }
                >
                  <ShieldCheck className="h-3 w-3" />
                  {t('updatePanel.signed')}
                </span>
              )}
              {status?.signature?.status === 'skipped' && (
                <span
                  className="inline-flex items-center gap-1 rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-warning-text"
                  title={t('updatePanel.unsignedTitle')}
                >
                  <ShieldOff className="h-3 w-3" />
                  {t('updatePanel.unsigned')}
                </span>
              )}
            </div>
          </div>
          <div className="rounded-2xl bg-background/60 px-3.5 py-3">
            <div className="text-[11px] text-muted-foreground">{t('updatePanel.upstreamConn')}</div>
            <div className="mt-1 flex items-center gap-1.5 text-sm font-semibold">
              {versions?.upstream_connected ? (
                <>
                  <span className="text-emerald-600 dark:text-emerald-400">{t('updatePanel.connOk')}</span>
                  {typeof versions.upstream_accounts === 'number' && (
                    <span className="text-xs font-normal text-muted-foreground">
                      {t('updatePanel.accountCount', {
                        count: versions.upstream_accounts,
                        n: versions.upstream_accounts,
                      })}
                    </span>
                  )}
                </>
              ) : (
                <span className="text-destructive-text">{t('updatePanel.connDown')}</span>
              )}
            </div>
          </div>
          <div className="rounded-2xl bg-background/60 px-3.5 py-3">
            <div className="text-[11px] text-muted-foreground">{t('updatePanel.lastUpdate')}</div>
            <div className="mt-1 text-sm font-semibold">
              {status?.finished_at ? fmtAgo(status.finished_at) : t('format.never')}
            </div>
          </div>
        </div>
        {status?.upstream_dir && (
          <div className="mt-2 truncate font-mono text-[11px] text-muted-foreground" title={status.upstream_dir}>
            {t('updatePanel.upstreamDir', {dir: status.upstream_dir})}
          </div>
        )}
      </div>

      {check && !check.has_any && !check.manager.error && !check.upstream.error && !status?.running && check.available !== false && (
        <div className="flex items-center gap-2 rounded-[20px] border border-emerald-500/30 bg-emerald-500/10 px-4 py-3">
          <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-500" />
          <span className="text-xs">{t('updatePanel.allUpToDate')}</span>
          {check.checked_at > 0 && (
            <span className="text-[11px] text-muted-foreground">
              {t('updatePanel.checkedAgo', {ago: fmtAgo(check.checked_at)})}
            </span>
          )}
        </div>
      )}

      {/* 更新状态 */}
      {status?.running && (
        <div className="flex items-start gap-2.5 rounded-[20px] border border-blue-500/30 bg-blue-500/10 p-4">
          <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin text-blue-500" />
          <div className="min-w-0 flex-1 space-y-1">
            <div className="text-xs font-medium">
              {t('updatePanel.updatingNow', {step: status.step || t('updatePanel.stepRunning')})}
            </div>
            <div className="text-[11px] text-muted-foreground">
              {t('updatePanel.updatingHint')}
            </div>
          </div>
        </div>
      )}

      {done && (
        <div
          className={
            'flex items-start gap-2.5 rounded-[20px] border p-4 ' +
            (status?.ok
              ? 'border-emerald-500/30 bg-emerald-500/10'
              : 'border-red-500/30 bg-red-500/10')
          }
        >
          {status?.ok ? (
            <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-500" />
          ) : (
            <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive-text" />
          )}
          <div className="min-w-0 flex-1 space-y-1">
            <div className="text-xs font-medium">
              {status?.ok ? t('updatePanel.updateDone') : t('updatePanel.updateNotDone')}
            </div>
            <div className="text-[11px] text-muted-foreground">
              {status?.ok
                ? (status?.duration ? t('updatePanel.doneDuration', {n: status.duration}) : '') +
                  t('updatePanel.doneRestart') +
                  (status?.signature?.status === 'verified' ? t('updatePanel.doneSigned') : '')
                : t('updatePanel.failedDetail')}
            </div>
            {status?.ok && status?.signature?.status === 'skipped' && (
              <div className="flex items-start gap-1.5 pt-1 text-[11px] text-warning-text">
                <ShieldOff className="mt-0.5 h-3 w-3 shrink-0" />
                <span>
                  <RichText text={t('updatePanel.skippedWarn')} />
                </span>
              </div>
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

      {/* 本部署没有在线更新能力：后端 /api/system/* 只提供状态与版本查询，
          没有 update / upstream-ref 接口，所以这里不摆任何会 404 的按钮 */}
      {(check?.available === false || status?.updater_found === false) && (
        <div className="flex items-start gap-2.5 rounded-[20px] border border-amber-500/30 bg-amber-500/10 p-4">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-warning-text" />
          <div className="space-y-1">
            <div className="text-xs font-medium">{t('updatePanel.updateNotAvailable')}</div>
            <div className="text-[11px] text-muted-foreground">
              {check?.reason || t('updatePanel.updateNotAvailable')}
            </div>
          </div>
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
    </div>
  );
}
