'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {AlertCircle, ChevronDown, ChevronRight, Plus, RefreshCw, RotateCcw, Square, TriangleAlert} from 'lucide-react';
import {errText, grokRegisterApi} from '@/lib/api';
import {fmtDateTime} from '@/lib/format';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {GrokRegisterDetail, GrokRegisterTask, GrokRegisterTaskStatus, GrokRegisterWorkerStats} from '@/lib/types';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Textarea} from '@/components/ui/textarea';

const POLL_MS = 4000;
const LIST_LIMIT = 50;

// 与后端 TaskCreate 的取值范围一致（apps/control/app/routers/grok.py:13-19）
const COUNT_MIN = 1;
const COUNT_MAX = 1000;
const WORKERS_MIN = 1;
const WORKERS_MAX = 32;
const ATTEMPTS_MIN = 1;
const ATTEMPTS_MAX = 5;

interface TaskDraft {
  count: string;
  workers: string;
  email_provider: string;
  proxy: string;
  max_attempts: string;
  payload: string;
}

const EMPTY_DRAFT: TaskDraft = {
  count: '1',
  workers: '1',
  email_provider: '',
  proxy: '',
  max_attempts: '3',
  payload: '',
};

const STATUS_LABEL: Record<GrokRegisterTaskStatus, string> = {
  queued: 'grok.statusQueued',
  running: 'grok.statusRunning',
  succeeded: 'grok.statusSucceeded',
  failed: 'grok.statusFailed',
  cancelled: 'grok.statusCancelled',
};

/** 状态机阶段名 → 文案；后端新增阶段时原样显示英文，不吞掉未知步骤 */
const STEP_LABEL: Record<string, string> = {
  queued: 'grok.stepQueued',
  running: 'grok.stepRunning',
  retried: 'grok.stepRetried',
  retry: 'grok.stepRetry',
  succeeded: 'grok.stepSucceeded',
  failed: 'grok.stepFailed',
  cancelled: 'grok.stepCancelled',
};

const STATUS_BADGE: Record<GrokRegisterTaskStatus, 'default' | 'secondary' | 'destructive' | 'outline'> = {
  queued: 'secondary',
  running: 'default',
  succeeded: 'secondary',
  failed: 'destructive',
  cancelled: 'outline',
};

/** 只接受 [min, max] 内的整数，其余（含小数、空串）一律判非法。 */
function intIn(raw: string, min: number, max: number): number | null {
  const text = raw.trim();
  if (!/^-?\d+$/.test(text)) return null;
  const value = Number(text);
  return value >= min && value <= max ? value : null;
}

/** 服务商配置区内的「注册任务」块：admin-only，非管理员不渲染也不发请求。 */
export function GrokRegisterTasks() {
  const t = useT();
  const [tasks, setTasks] = useState<GrokRegisterTask[]>([]);
  const [worker, setWorker] = useState<GrokRegisterWorkerStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [draft, setDraft] = useState<TaskDraft>(EMPTY_DRAFT);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [detail, setDetail] = useState<GrokRegisterDetail | null>(null);
  const [detailError, setDetailError] = useState('');
  /** 轮询回调自己不重建定时器：展开项通过 ref 读取，避免每次展开重置 4 秒节奏 */
  const expandedRef = useRef<number | null>(null);

  const load = useCallback(async (initial = false) => {
    if (initial) setLoading(true);
    try {
      const [rows, stats] = await Promise.all([
        grokRegisterApi.list(LIST_LIMIT),
        grokRegisterApi.worker(),
      ]);
      setTasks(rows);
      setWorker(stats);
      setError('');
      // 展开项跟着同一个轮询刷新；请求返回时再核对一次 id，避免收起后旧数据回填
      const openId = expandedRef.current;
      if (openId !== null) {
        try {
          const next = await grokRegisterApi.detail(openId);
          if (expandedRef.current === openId) {
            setDetail(next);
            setDetailError('');
          }
        } catch (e) {
          if (expandedRef.current === openId) setDetailError(errText(e));
        }
      }
    } catch (e) {
      setError(errText(e));
    } finally {
      if (initial) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(true);
    // 4 秒轮询；页面不可见时空转，回到前台立即补一次
    const tick = () => {
      if (document.hidden) return;
      void load();
    };
    const timer = window.setInterval(tick, POLL_MS);
    document.addEventListener('visibilitychange', tick);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [load]);

  async function cancelTask(task: GrokRegisterTask) {
    setBusy(task.id);
    try {
      await grokRegisterApi.cancel(task.id);
      notify.ok(t('grok.cancelledOk'), `#${task.id}`);
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(null);
    }
  }

  async function retryTask(task: GrokRegisterTask) {
    setBusy(task.id);
    try {
      await grokRegisterApi.retry(task.id);
      notify.ok(t('grok.retriedOk'), `#${task.id}`);
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(null);
    }
  }

  async function toggleDetail(task: GrokRegisterTask) {
    if (expandedRef.current === task.id) {
      expandedRef.current = null;
      setExpandedId(null);
      setDetail(null);
      setDetailError('');
      return;
    }
    expandedRef.current = task.id;
    setExpandedId(task.id);
    setDetail(null);
    setDetailError('');
    try {
      const next = await grokRegisterApi.detail(task.id);
      if (expandedRef.current === task.id) setDetail(next);
    } catch (e) {
      if (expandedRef.current === task.id) setDetailError(errText(e));
    }
  }

  async function submitTask() {
    const count = intIn(draft.count, COUNT_MIN, COUNT_MAX);
    if (count === null) {
      notify.err(t('grok.badCount', {min: COUNT_MIN, max: COUNT_MAX}));
      return;
    }
    const workers = intIn(draft.workers, WORKERS_MIN, WORKERS_MAX);
    if (workers === null) {
      notify.err(t('grok.badWorkers', {min: WORKERS_MIN, max: WORKERS_MAX}));
      return;
    }
    const maxAttempts = intIn(draft.max_attempts, ATTEMPTS_MIN, ATTEMPTS_MAX);
    if (maxAttempts === null) {
      notify.err(t('grok.badAttempts', {min: ATTEMPTS_MIN, max: ATTEMPTS_MAX}));
      return;
    }
    let payload: Record<string, unknown> = {};
    const payloadText = draft.payload.trim();
    if (payloadText) {
      try {
        const parsed: unknown = JSON.parse(payloadText);
        if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
          throw new Error('payload must be an object');
        }
        payload = parsed as Record<string, unknown>;
      } catch {
        notify.err(t('grok.badPayload'));
        return;
      }
    }

    setSubmitting(true);
    try {
      await grokRegisterApi.create({
        count,
        workers,
        email_provider: draft.email_provider.trim(),
        proxy: draft.proxy.trim(),
        max_attempts: maxAttempts,
        payload,
      });
      notify.ok(t('grok.createdOk'));
      setCreating(false);
      setDraft(EMPTY_DRAFT);
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSubmitting(false);
    }
  }

  const executorMissing = worker?.executor === 'unconfigured';
  const showLoading = loading && tasks.length === 0;
  const showErrorOnly = !loading && error !== '' && tasks.length === 0;
  const showEmpty = !loading && error === '' && tasks.length === 0;
  const showList = !loading && tasks.length > 0;

  return (
    <div className="mt-1 flex flex-col gap-2 border-t border-border/40 pt-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[11px] text-muted-foreground">{t('grok.title')}</span>
        {worker && (
          <span className="text-[10px] text-muted-foreground">
            {t('grok.statusQueued')} {worker.queued} · {t('grok.statusRunning')} {worker.running} ·{' '}
            {t('grok.statusSucceeded')} {worker.succeeded} · {t('grok.statusFailed')} {worker.failed} ·{' '}
            {t('grok.statusCancelled')} {worker.cancelled}
          </span>
        )}
        <Button
          variant="ghost"
          size="icon"
          className="ml-auto h-6 w-6 rounded-md"
          title={t('grok.refresh')}
          disabled={busy !== null}
          onClick={() => load()}
        >
          <RefreshCw className="h-3 w-3" />
        </Button>
        {!creating && (
          <Button
            size="sm"
            variant="outline"
            className="h-7 rounded-full text-[11px]"
            disabled={busy !== null || submitting}
            onClick={() => setCreating(true)}
          >
            <Plus className="mr-1 h-3 w-3" />
            {t('grok.newTask')}
          </Button>
        )}
      </div>

      {/* 执行器未配置时提前拦一道：任务仍可入队，但会以明确原因失败 */}
      {executorMissing && (
        <div className="flex items-start gap-2 rounded-xl bg-muted/60 px-3 py-2 text-[11px] leading-4 text-muted-foreground">
          <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span className="whitespace-normal">{t('grok.executorMissing')}</span>
        </div>
      )}

      {creating && (
        <div className="flex flex-col gap-2 rounded-xl bg-background/60 p-2">
          <div className="flex items-center gap-2">
            <span className="w-28 text-[11px] text-muted-foreground">{t('grok.fCount')}</span>
            <Input
              inputMode="numeric"
              value={draft.count}
              onChange={(e) => setDraft((prev) => ({...prev, count: e.target.value}))}
              disabled={submitting}
              className="h-8 w-[100px] bg-background font-mono text-[11px]"
            />
            <span className="w-28 text-[11px] text-muted-foreground">{t('grok.fWorkers')}</span>
            <Input
              inputMode="numeric"
              value={draft.workers}
              onChange={(e) => setDraft((prev) => ({...prev, workers: e.target.value}))}
              disabled={submitting}
              className="h-8 w-[100px] bg-background font-mono text-[11px]"
            />
            {worker && (
              <span className="text-[10px] text-muted-foreground">
                {t('grok.workersCap', {max: worker.max_workers})}
              </span>
            )}
          </div>
          <div className="flex items-center gap-2">
            <span className="w-28 text-[11px] text-muted-foreground">{t('grok.fEmailProvider')}</span>
            <Input
              value={draft.email_provider}
              onChange={(e) => setDraft((prev) => ({...prev, email_provider: e.target.value}))}
              disabled={submitting}
              placeholder={t('grok.fEmailProviderPh')}
              className="h-8 w-[280px] bg-background text-[11px]"
            />
          </div>
          <div className="flex items-center gap-2">
            <span className="w-28 text-[11px] text-muted-foreground">{t('grok.fProxy')}</span>
            <Input
              value={draft.proxy}
              onChange={(e) => setDraft((prev) => ({...prev, proxy: e.target.value}))}
              disabled={submitting}
              placeholder={t('grok.fProxyPh')}
              className="h-8 w-[380px] bg-background font-mono text-[11px]"
            />
          </div>
          <div className="flex items-center gap-2">
            <span className="w-28 text-[11px] text-muted-foreground">{t('grok.fMaxAttempts')}</span>
            <Input
              inputMode="numeric"
              value={draft.max_attempts}
              onChange={(e) => setDraft((prev) => ({...prev, max_attempts: e.target.value}))}
              disabled={submitting}
              className="h-8 w-[100px] bg-background font-mono text-[11px]"
            />
          </div>
          <div className="flex items-start gap-2">
            <span className="mt-1.5 w-28 text-[11px] text-muted-foreground">{t('grok.fPayload')}</span>
            <Textarea
              rows={2}
              value={draft.payload}
              onChange={(e) => setDraft((prev) => ({...prev, payload: e.target.value}))}
              disabled={submitting}
              placeholder={t('grok.fPayloadPh')}
              className="w-[380px] bg-background font-mono text-[11px]"
            />
          </div>
          <div className="flex items-center gap-2 pl-[120px]">
            <Button
              size="sm"
              className="h-7 rounded-full text-[11px]"
              disabled={submitting}
              onClick={submitTask}
            >
              {t('grok.submit')}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-7 rounded-full text-[11px]"
              disabled={submitting}
              onClick={() => setCreating(false)}
            >
              {t('common.cancel')}
            </Button>
          </div>
        </div>
      )}

      {showLoading && (
        <div className="flex items-center gap-1.5 px-1 py-1 text-[11px] text-muted-foreground">
          <RefreshCw className="h-3 w-3 animate-spin" />
          {t('common.loading')}
        </div>
      )}

      {showErrorOnly && (
        <div className="flex items-start gap-2 px-1 py-1 text-[11px] text-muted-foreground">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span className="whitespace-normal">{error}</span>
          <Button
            variant="ghost"
            size="sm"
            className="ml-auto h-6 rounded-full text-[11px]"
            onClick={() => load(true)}
          >
            {t('grok.retry')}
          </Button>
        </div>
      )}

      {showEmpty && (
        <div className="flex flex-col items-start gap-2 px-1 py-1">
          <div className="text-[11px] text-muted-foreground">{t('grok.empty')}</div>
          <div className="text-[10px] leading-4 text-muted-foreground">{t('grok.emptyHint')}</div>
          {!creating && (
            <Button
              size="sm"
              variant="outline"
              className="h-7 rounded-full text-[11px]"
              onClick={() => setCreating(true)}
            >
              <Plus className="mr-1 h-3 w-3" />
              {t('grok.newTask')}
            </Button>
          )}
        </div>
      )}

      {!loading && error !== '' && tasks.length > 0 && (
        <div className="flex items-start gap-2 rounded-xl bg-muted/60 px-3 py-2 text-[11px] leading-4 text-muted-foreground">
          <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span className="whitespace-normal">{error}</span>
          <Button
            variant="ghost"
            size="sm"
            className="ml-auto h-6 rounded-full text-[11px]"
            onClick={() => load()}
          >
            {t('grok.retry')}
          </Button>
        </div>
      )}

      {showList && (
        <div className="flex flex-col gap-1.5">
          {tasks.map((task) => {
            const canCancel = task.status === 'queued' || task.status === 'running';
            const canRetry = task.status === 'failed' || task.status === 'cancelled';
            const isBusy = busy === task.id;
            return (
              <div key={task.id} className="rounded-xl bg-background/60 px-2 py-1.5">
                <div className="flex flex-wrap items-center gap-1.5">
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6 rounded-md"
                    title={t('grok.detailToggle')}
                    aria-expanded={expandedId === task.id}
                    aria-controls={`grok-task-${task.id}-detail`}
                    onClick={() => toggleDetail(task)}
                  >
                    {expandedId === task.id ? (
                      <ChevronDown className="h-3.5 w-3.5" />
                    ) : (
                      <ChevronRight className="h-3.5 w-3.5" />
                    )}
                  </Button>
                  <span className="font-mono text-xs font-medium">#{task.id}</span>
                  <Badge variant={STATUS_BADGE[task.status]} className="rounded-full text-[10px]">
                    {t(STATUS_LABEL[task.status])}
                  </Badge>
                  <span className="text-[10px] text-muted-foreground">{fmtDateTime(task.created_at)}</span>
                  <span className="text-[10px] text-muted-foreground">
                    {t('grok.progress', {
                      count: task.count,
                      done: task.status === 'succeeded' ? task.count : '—',
                    })}
                  </span>
                  <span className="text-[10px] text-muted-foreground">
                    {t('grok.attempts', {attempts: task.attempts, max: task.max_attempts})}
                  </span>
                  {task.email_provider && (
                    <span className="truncate text-[10px] text-muted-foreground">{task.email_provider}</span>
                  )}
                  <div className="ml-auto flex items-center gap-1">
                    {canCancel ? (
                      <ConfirmDialog
                        trigger={
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7 rounded-md"
                            title={t('grok.cancel')}
                            disabled={isBusy}
                          >
                            <Square className="h-3.5 w-3.5" />
                          </Button>
                        }
                        title={t('grok.cancelTitle', {id: task.id})}
                        description={t('grok.cancelDesc')}
                        confirmText={t('grok.cancel')}
                        destructive
                        onConfirm={() => cancelTask(task)}
                      />
                    ) : (
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 rounded-md"
                        title={t('grok.cancelDisabled')}
                        disabled
                      >
                        <Square className="h-3.5 w-3.5" />
                      </Button>
                    )}
                    {canRetry ? (
                      <ConfirmDialog
                        trigger={
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7 rounded-md"
                            title={t('grok.retryTask')}
                            disabled={isBusy}
                          >
                            <RotateCcw className="h-3.5 w-3.5" />
                          </Button>
                        }
                        title={t('grok.retryTitle', {id: task.id})}
                        description={t('grok.retryDesc')}
                        confirmText={t('grok.retryTask')}
                        onConfirm={() => retryTask(task)}
                      />
                    ) : (
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 rounded-md"
                        title={t('grok.retryDisabled')}
                        disabled
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                      </Button>
                    )}
                  </div>
                </div>
                {task.error && (
                  <div className="mt-0.5 whitespace-normal text-[10px] leading-4 text-destructive">
                    {task.error}
                  </div>
                )}
                {expandedId === task.id && (
                  <div id={`grok-task-${task.id}-detail`} className="mt-1.5 border-t border-border/40 pt-1.5">
                    <div className="mb-1 text-[10px] font-medium text-muted-foreground">{t('grok.timeline')}</div>
                    {/* 时间线只放后端真有的四个时刻；没有进度百分比，也不编原始 stdout */}
                    {detail && (
                      <div className="mb-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-muted-foreground">
                        <span>
                          {t('grok.tlCreated')} {detail.timeline.created_at ? fmtDateTime(detail.timeline.created_at) : t('grok.tlNone')}
                        </span>
                        <span>
                          {t('grok.tlStarted')} {detail.timeline.started_at ? fmtDateTime(detail.timeline.started_at) : t('grok.tlNone')}
                        </span>
                        <span>
                          {t('grok.tlFinished')} {detail.timeline.finished_at ? fmtDateTime(detail.timeline.finished_at) : t('grok.tlNone')}
                        </span>
                        <span>
                          {t('grok.tlNextRun')} {detail.timeline.next_run_at ? fmtDateTime(detail.timeline.next_run_at) : t('grok.tlNone')}
                        </span>
                      </div>
                    )}
                    {detailError ? (
                      <div className="whitespace-pre-wrap break-all text-[10px] leading-4 text-destructive">
                        {detailError}
                      </div>
                    ) : !detail ? (
                      <div className="text-[10px] text-muted-foreground">{t('common.loading')}</div>
                    ) : detail.steps.length === 0 ? (
                      <div className="text-[10px] text-muted-foreground">{t('grok.timelineEmpty')}</div>
                    ) : (
                      <ol className="flex flex-col gap-1">
                        {detail.steps.map((event, index) => {
                          const failed = event.status !== 'ok';
                          const stepLabel = STEP_LABEL[event.step];
                          return (
                            <li
                              key={`${event.ts}-${index}`}
                              className="flex flex-wrap items-baseline gap-1.5 text-[10px] leading-4"
                            >
                              <span className="font-mono text-muted-foreground">{fmtDateTime(event.ts)}</span>
                              <span className="font-medium">{stepLabel ? t(stepLabel) : event.step}</span>
                              <span className={failed ? 'text-destructive' : 'text-muted-foreground'}>
                                {failed ? t('grok.stepResultFailed') : t('grok.stepResultOk')}
                              </span>
                              {/* 失败原因必须原样铺开：截成「失败」会丢掉排查所需的上游回复 */}
                              {(event.detail || (failed && detail.error)) && (
                                <span
                                  className={`w-full whitespace-pre-wrap break-all ${
                                    failed ? 'text-destructive' : 'text-muted-foreground'
                                  }`}
                                >
                                  {event.detail || detail.error}
                                </span>
                              )}
                            </li>
                          );
                        })}
                      </ol>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
