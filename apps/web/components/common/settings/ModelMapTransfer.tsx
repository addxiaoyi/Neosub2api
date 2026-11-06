'use client';

import {useMemo, useState} from 'react';
import {Download, RotateCcw, Upload} from 'lucide-react';
import {errText, settingsApi} from '@/lib/api';
import type {ModelMapEntryResult, ModelMapImportMode, ModelMapImportReply} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import {fmtDateTime} from '@/lib/format';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Select, SelectContent, SelectItem, SelectTrigger, SelectValue} from '@/components/ui/select';
import {Textarea} from '@/components/ui/textarea';

/** 后端 reason 枚举 → 文案键；没有对应项的（例如未来新增）落到「其它原因」 */
const REASON_KEY: Record<string, string> = {
  unknown_provider: 'settings.mmReasonUnknownProvider',
  unknown_model: 'settings.mmReasonUnknownModel',
  invalid_target: 'settings.mmReasonInvalidTarget',
  empty_alias: 'settings.mmReasonEmptyAlias',
  empty_target: 'settings.mmReasonEmptyTarget',
  duplicate_alias: 'settings.mmReasonDuplicateAlias',
  invalid_shape: 'settings.mmReasonInvalidShape',
  too_many_entries: 'settings.mmReasonTooManyEntries',
  unchanged: 'settings.mmReasonUnchanged',
  aborted_by_invalid_entries: 'settings.mmReasonAborted',
  invalid_mode: 'settings.mmReasonInvalidMode',
  write_failed: 'settings.mmReasonWriteFailed',
};

const STATUS_KEY: Record<string, string> = {
  applied: 'settings.mmStatusApplied',
  skipped: 'settings.mmStatusSkipped',
  failed: 'settings.mmStatusFailed',
};

const ACTION_KEY: Record<string, string> = {
  added: 'settings.mmActionAdded',
  updated: 'settings.mmActionUpdated',
  removed: 'settings.mmActionRemoved',
  unchanged: 'settings.mmActionUnchanged',
};

type ImportState =
  | {kind: 'idle'}
  | {kind: 'busy'}
  | {kind: 'done'; status: number; reply: ModelMapImportReply}
  | {kind: 'error'; message: string};

/**
 * 模型映射的批量导入 / 导出 / 回滚。
 *
 * 两个后端事实决定这里的界面形态：
 * 1. 只要有一条非法，整批拒绝（400），所以被拒时必须明说「一条都没写入」，
 *    不能把 results 里零散的成功项说成部分成功；
 * 2. 每条结果都有 reason，必须逐条展示，只报总数等于把排查信息扔掉。
 */
export function ModelMapTransfer({onChanged}: {onChanged: () => void}) {
  const t = useT();
  const [mode, setMode] = useState<ModelMapImportMode>('merge');
  const [text, setText] = useState('');
  const [state, setState] = useState<ImportState>({kind: 'idle'});
  const [exporting, setExporting] = useState(false);
  const [rollingBack, setRollingBack] = useState(false);
  const [rollbackNote, setRollbackNote] = useState('');

  /** 接受整份导出文件（{mappings: ...}）或裸映射对象 */
  const parsed = useMemo(() => {
    const raw = text.trim();
    if (!raw) return {ok: false as const, key: 'settings.mmNeedInput'};
    let obj: unknown;
    try {
      obj = JSON.parse(raw);
    } catch {
      return {ok: false as const, key: 'settings.mmBadJson'};
    }
    if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
      return {ok: false as const, key: 'settings.mmNeedObject'};
    }
    const outer = obj as Record<string, unknown>;
    const inner = outer.mappings;
    if (inner && typeof inner === 'object' && !Array.isArray(inner)) {
      return {ok: true as const, mappings: inner as Record<string, unknown>};
    }
    return {ok: true as const, mappings: outer};
  }, [text]);

  async function doExport() {
    if (exporting) return;
    setExporting(true);
    try {
      const data = await settingsApi.exportModelMap();
      const stamp = new Date().toISOString().replace(/[^0-9]/g, '').slice(0, 14);
      const blob = new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `model-map-${stamp}.json`;
      a.click();
      URL.revokeObjectURL(url);
      notify.ok(t('settings.mmExported'), data.exported_at ? fmtDateTime(data.exported_at) : undefined);
    } catch (e) {
      notify.err(t('settings.mmExportFailed'), errText(e));
    } finally {
      setExporting(false);
    }
  }

  async function doImport() {
    if (state.kind === 'busy' || !parsed.ok) return;
    setState({kind: 'busy'});
    try {
      const r = await settingsApi.importModelMap(mode, parsed.mappings);
      setState({kind: 'done', status: r.status, reply: r.data});
      if (r.status >= 200 && r.status < 300 && r.data?.ok) {
        const failed = r.data.failed ?? 0;
        const applied = r.data.applied ?? 0;
        if (!r.data.changed) {
          notify.info(t('settings.mmNoChange'));
        } else if (failed > 0) {
          notify.warn(
            t('settings.mmSummary', {
              total: r.data.total ?? 0,
              applied,
              skipped: r.data.skipped ?? 0,
              removed: r.data.removed ?? 0,
              failed,
            }),
          );
        } else {
          notify.ok(t('settings.mmApplied'), t('settings.mmSnapshot'));
        }
        await onChanged();
      }
    } catch (e) {
      setState({kind: 'error', message: errText(e)});
      notify.err(t('settings.mmImportFailed'), errText(e));
    }
  }

  async function doRollback() {
    if (rollingBack) return;
    setRollingBack(true);
    setRollbackNote('');
    try {
      const r = await settingsApi.rollbackModelMap();
      setRollbackNote(
        t('settings.mmRollbackOk') +
          (r.snapshot_taken_at ? ' ' + t('settings.mmRollbackOkDetail', {at: fmtDateTime(r.snapshot_taken_at)}) : ''),
      );
      notify.ok(t('settings.mmRollbackOk'));
      await onChanged();
    } catch (e) {
      const message = errText(e);
      setRollbackNote(t('settings.mmRollbackFailed') + ' ' + message);
      notify.err(t('settings.mmRollbackFailed'), message);
    } finally {
      setRollingBack(false);
    }
  }

  const reply = state.kind === 'done' ? state.reply : null;
  const rejected = !!reply && reply.ok === false;
  const rows: ModelMapEntryResult[] =
    reply?.results?.length ? reply.results : (reply?.error?.errors ?? []);

  return (
    <section className="rounded-[20px] bg-muted p-4">
      <div className="mb-1 text-sm font-medium">{t('settings.mmTitle')}</div>
      <div className="mb-3 text-[11px] leading-5 text-muted-foreground">
        <div>{t('settings.mmDesc')}</div>
      </div>

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Button variant="outline" className="rounded-full" onClick={doExport} disabled={exporting}>
          <Download className="mr-1.5 h-4 w-4" />
          {t('settings.mmExport')}
        </Button>
        <Button variant="outline" className="rounded-full" onClick={doRollback} disabled={rollingBack}>
          <RotateCcw className="mr-1.5 h-4 w-4" />
          {t('settings.mmRollback')}
        </Button>
        {rollbackNote && <span className="text-[11px] text-muted-foreground">{rollbackNote}</span>}
      </div>

      <div className="grid grid-cols-1 items-start gap-3 md:grid-cols-[minmax(0,1fr)_280px]">
        <div className="space-y-2">
          <div className="text-[11px] text-muted-foreground">{t('settings.mmInput')}</div>
          <Textarea
            rows={7}
            spellCheck={false}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={t('settings.mmInputPh')}
            className="bg-background font-mono text-xs"
          />
          {!parsed.ok && text.trim() && (
            <div className="text-[11px] text-destructive-text">{t(parsed.key)}</div>
          )}
        </div>
        <div className="space-y-2">
          <div className="text-[11px] text-muted-foreground">{t('settings.mmMode')}</div>
          <Select value={mode} onValueChange={(v) => setMode(v as ModelMapImportMode)}>
            <SelectTrigger className="bg-background">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="merge">{t('settings.mmModeMerge')}</SelectItem>
              <SelectItem value="overwrite">{t('settings.mmModeOverwrite')}</SelectItem>
            </SelectContent>
          </Select>
          <Button
            className="w-full rounded-full"
            disabled={state.kind === 'busy' || !parsed.ok}
            onClick={doImport}
          >
            <Upload className="mr-1.5 h-4 w-4" />
            {state.kind === 'busy' ? t('settings.mmImporting') : t('settings.mmImport')}
          </Button>
        </div>
      </div>

      {/* 结果区：被拒 / 逐条 reason / 汇总，都不许合并成一句「部分成功」 */}
      {state.kind === 'error' && <div className="mt-3 text-[11px] text-destructive-text">{state.message}</div>}

      {reply && (
        <div className="mt-3 space-y-2 border-t border-border/40 pt-2">
          {rejected ? (
            <div className="rounded-xl bg-destructive/10 px-3 py-2 text-[11px] leading-5">
              <div className="font-medium text-destructive-text">{t('settings.mmRejected')}</div>
              <div className="text-muted-foreground">{t('settings.mmRejectedDesc')}</div>
            </div>
          ) : !reply.changed ? (
            <div className="text-[11px] text-muted-foreground">{t('settings.mmNoChange')}</div>
          ) : (
            <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
              <span>
                {t('settings.mmSummary', {
                  total: reply.total ?? 0,
                  applied: reply.applied ?? 0,
                  skipped: reply.skipped ?? 0,
                  removed: reply.removed ?? 0,
                  failed: reply.failed ?? 0,
                })}
              </span>
              <span>{reply.snapshot_taken ? t('settings.mmSnapshot') : t('settings.mmNoSnapshot')}</span>
            </div>
          )}

          {rows.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-[10px] leading-4">
                <thead>
                  <tr className="text-left text-muted-foreground">
                    <th className="py-1 pr-2 font-normal">{t('settings.mmColAlias')}</th>
                    <th className="py-1 pr-2 font-normal">{t('settings.mmColTarget')}</th>
                    <th className="py-1 pr-2 font-normal">{t('settings.mmColResult')}</th>
                    <th className="py-1 pr-2 font-normal">{t('settings.mmColAction')}</th>
                    <th className="py-1 font-normal">{t('settings.mmColReason')}</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row, index) => (
                    <tr key={`${row.alias}-${index}`} className="border-t border-border/30">
                      <td className="py-1 pr-2 font-mono">{row.alias || '—'}</td>
                      <td className="py-1 pr-2 font-mono text-muted-foreground">{row.target || '—'}</td>
                      <td className="py-1 pr-2">
                        {row.status ? (
                          <Badge
                            variant={row.status === 'failed' ? 'destructive' : 'secondary'}
                            className="rounded-full text-[10px]"
                          >
                            {STATUS_KEY[row.status] ? t(STATUS_KEY[row.status]) : row.status}
                          </Badge>
                        ) : (
                          t('settings.mmActionNone')
                        )}
                      </td>
                      <td className="py-1 pr-2 text-muted-foreground">
                        {row.action
                          ? ACTION_KEY[row.action]
                            ? t(ACTION_KEY[row.action])
                            : row.action
                          : t('settings.mmActionNone')}
                      </td>
                      <td className="py-1 text-muted-foreground">
                        {row.reason ? (
                          <span>
                            {REASON_KEY[row.reason] ? t(REASON_KEY[row.reason]) : t('settings.mmReasonOther')}
                            {row.message ? ` · ${row.message}` : ''}
                          </span>
                        ) : (
                          row.message || '—'
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
