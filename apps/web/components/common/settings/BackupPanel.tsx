'use client';

import {useCallback, useEffect, useState} from 'react';
import {
  AlertTriangle,
  Archive,
  CircleCheck,
  DatabaseBackup,
  Loader2,
  RefreshCw,
} from 'lucide-react';
import {errText, systemApi} from '@/lib/api';
import type {BackupCreated, BackupItem, BackupRestored} from '@/lib/types';
import {notify} from '@/lib/toast';
import {fmtDateTime} from '@/lib/format';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
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
import {useT} from '@/lib/i18n/provider';

const SIZE_UNITS = ['B', 'KB', 'MB', 'GB'];

function fmtBytes(bytes: number): string {
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < SIZE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${SIZE_UNITS[unit]}`;
}

type Outcome =
  | {kind: 'backup'; data: BackupCreated}
  | {kind: 'restore'; data: BackupRestored};

export function BackupPanel() {
  const t = useT();
  const [items, setItems] = useState<BackupItem[]>([]);
  const [dir, setDir] = useState('');
  /** 绝对路径默认收起：面板截图常被分享，不该把部署目录一起带出去 */
  const [showPath, setShowPath] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [creating, setCreating] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);

  // 恢复二次确认：目标归档 + 用户输入的确认词 + 执行中状态
  const [target, setTarget] = useState<BackupItem | null>(null);
  const [typed, setTyped] = useState('');
  const [restoring, setRestoring] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const r = await systemApi.backups();
      setItems(r.backups);
      setDir(r.dir);
    } catch (e) {
      setError(errText(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const create = useCallback(async () => {
    setCreating(true);
    try {
      const r = await systemApi.createBackup();
      setOutcome({kind: 'backup', data: r});
      notify.ok(t('backupPanel.backupDone'), `${r.name} · ${fmtBytes(r.size_bytes)} · ${r.duration_ms}ms`);
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setCreating(false);
    }
  }, [load, t]);

  const restore = useCallback(async () => {
    if (!target) return;
    setRestoring(true);
    try {
      const r = await systemApi.restoreBackup(target.name);
      setOutcome({kind: 'restore', data: r});
      notify.ok(t('backupPanel.restoreDone'), t('backupPanel.snapshotSaved', {name: r.pre_restore_backup}));
      setTarget(null);
      setTyped('');
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setRestoring(false);
    }
  }, [load, target, t]);

  const confirmWord = t('backupPanel.confirmWord');
  const canRestore = typed.trim() === confirmWord && !restoring;

  return (
    <section className="rounded-[20px] bg-muted p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-2.5">
          <DatabaseBackup className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <div>
            <div className="text-sm font-medium">{t('backupPanel.title')}</div>
            <div className="mt-1 text-[11px] leading-5 text-muted-foreground">
              {t('backupPanel.desc')}
            </div>
            <div className="mt-1 text-[11px] leading-5 text-muted-foreground">
              {t('backupPanel.dirLocate')}
              {dir && (
                <button
                  type="button"
                  className="ml-1 underline underline-offset-4 hover:text-foreground"
                  aria-expanded={showPath}
                  onClick={() => setShowPath((v) => !v)}
                >
                  {showPath ? t('backupPanel.dirHide') : t('backupPanel.dirShow')}
                </button>
              )}
            </div>
            {showPath && dir && (
              <div className="mt-1 break-all rounded-md bg-background/60 px-2 py-1 font-mono text-[10px] leading-4 text-muted-foreground">
                {dir}
              </div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            variant="ghost"
            className="rounded-full"
            onClick={() => void load()}
            disabled={loading || creating || restoring}
            title={t('backupPanel.refreshTitle')}
          >
            <RefreshCw className={`mr-1 h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />
            {t('backupPanel.refresh')}
          </Button>
          <Button
            size="sm"
            className="rounded-full"
            onClick={() => void create()}
            disabled={creating || restoring}
          >
            {creating ? (
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
            ) : (
              <Archive className="mr-1 h-3.5 w-3.5" />
            )}
            {creating ? t('backupPanel.backingUp') : t('backupPanel.backupNow')}
          </Button>
        </div>
      </div>

      {outcome && (
        <div className="mt-4 flex items-start gap-2 rounded-[16px] border border-emerald-500/30 bg-emerald-500/8 px-3 py-2.5 text-[11.5px] leading-5">
          <CircleCheck className="mt-0.5 h-3.5 w-3.5 shrink-0 text-emerald-500" />
          <div className="min-w-0">
            {outcome.kind === 'backup' ? (
              <>
                <div className="font-medium">{t('backupPanel.backupDone')}</div>
                <div className="text-muted-foreground">
                  {outcome.data.name} · {fmtBytes(outcome.data.size_bytes)} · {outcome.data.duration_ms}ms
                </div>
              </>
            ) : (
              <>
                <div className="font-medium">{t('backupPanel.restoreDone')}</div>
                <div className="text-muted-foreground">
                  {t('backupPanel.restoredFrom', {name: outcome.data.restored})}
                </div>
                <div className="text-muted-foreground">
                  {t('backupPanel.snapshotSaved', {name: outcome.data.pre_restore_backup})}
                </div>
                <div className="text-muted-foreground">
                  {t('backupPanel.restoreTook', {ms: outcome.data.duration_ms})}
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {loading ? (
        <div className="mt-4 flex items-center gap-2 text-[11.5px] text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          {t('backupPanel.loading')}
        </div>
      ) : error ? (
        <div className="mt-4 flex items-start gap-2 rounded-[16px] border border-destructive/30 bg-destructive/5 px-3 py-2.5">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive-text" />
          <div className="min-w-0 flex-1">
            <div className="text-[11.5px] leading-5 whitespace-pre-wrap break-words">{error}</div>
            <Button
              size="sm"
              variant="outline"
              className="mt-2 rounded-full"
              onClick={() => void load()}
            >
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
              {t('backupPanel.retry')}
            </Button>
          </div>
        </div>
      ) : items.length === 0 ? (
        <div className="mt-4 rounded-[16px] border border-dashed border-border/70 px-3 py-6 text-center">
          <div className="text-xs font-medium">{t('backupPanel.empty')}</div>
          <div className="mt-1 text-[11px] leading-5 text-muted-foreground">
            {t('backupPanel.emptyDesc')}
          </div>
        </div>
      ) : (
        <div className="mt-4 divide-y divide-border/60 overflow-hidden rounded-[16px] border border-border/60 bg-background/40">
          {items.map((item) => (
            <div key={item.name} className="flex flex-wrap items-center gap-3 px-3 py-2.5">
              <div className="min-w-0 flex-1">
                <div className="truncate font-mono text-[11.5px]">{item.name}</div>
                <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[11px] text-muted-foreground">
                  <span>{fmtDateTime(item.created_at)}</span>
                  <span>·</span>
                  <span>{fmtBytes(item.size_bytes)}</span>
                  {!item.has_checksum && (
                    <Badge variant="outline" className="ml-1 text-[10px]">
                      {t('backupPanel.noChecksum')}
                    </Badge>
                  )}
                </div>
              </div>
              <Button
                size="sm"
                variant="outline"
                className="rounded-full"
                disabled={creating || restoring}
                onClick={() => {
                  setTarget(item);
                  setTyped('');
                }}
              >
                {t('backupPanel.restore')}
              </Button>
            </div>
          ))}
        </div>
      )}

      <AlertDialog
        open={!!target}
        onOpenChange={(open) => {
          if (!open && !restoring) {
            setTarget(null);
            setTyped('');
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('backupPanel.confirmTitle')}</AlertDialogTitle>
            <AlertDialogDescription className="space-y-2">
              <span className="block">{t('backupPanel.confirmWarn')}</span>
              <span className="block">
                {t('backupPanel.confirmSnapshot')}
              </span>
              {target && (
                <span className="block break-all font-mono text-[11.5px] text-foreground">
                  {target.name}
                </span>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="mt-1 space-y-1.5">
            <label
              htmlFor="backup-confirm-word"
              className="block text-[11.5px] leading-5 text-muted-foreground"
            >
              {t('backupPanel.typeWord', {word: confirmWord})}
            </label>
            <Input
              id="backup-confirm-word"
              autoFocus
              autoComplete="off"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={confirmWord}
              disabled={restoring}
              className="bg-background"
            />
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={restoring} onClick={() => setTyped('')}>
              {t('common.cancel')}
            </AlertDialogCancel>
            <AlertDialogAction
              disabled={!canRestore}
              className={canRestore ? 'bg-destructive text-white hover:bg-destructive/90' : ''}
              onClick={(e) => {
                // 不用 Radix 的自动关闭：恢复请求可能很久，表单要先禁用
                e.preventDefault();
                if (canRestore) void restore();
              }}
            >
              {restoring ? (
                <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
              ) : null}
              {restoring ? t('backupPanel.restoring') : t('backupPanel.confirmRestore')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
