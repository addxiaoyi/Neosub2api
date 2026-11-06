'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {KeyRound, Plus, Trash2, Ban, CircleCheck, Pencil, AlertCircle, RotateCcw, Pause, Play} from 'lucide-react';
import {notify} from '@/lib/toast';
import {keyApi, errText} from '@/lib/api';
import {BASE_PATH} from '@/lib/base-path';
import type {ApiKey, BatchKeyResult} from '@/lib/types';
import {fmtDateTime} from '@/lib/format';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {useAuth} from '@/lib/auth-context';
import {WIZARD_KEY_NAME} from '@/lib/onboarding';
import {Button} from '@/components/ui/button';
import {CopyButton} from '@/components/ui/copy-button';
import {useT} from '@/lib/i18n/provider';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

// 与后端 BATCH_MAX_IDS 对齐：超限时前端先拦，别把一次必然 400 的请求发出去
const BATCH_LIMIT = 200;

export default function KeysPage() {
  const t = useT();
  const {isAdmin} = useAuth();
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  /** 列表拉取失败：与「真的没有密钥」分开展示，并给重试入口 */
  const [loadFailed, setLoadFailed] = useState(false);
  const [formOpen, setFormOpen] = useState(false);
  const [editing, setEditing] = useState<ApiKey | null>(null);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [issued, setIssued] = useState<string | null>(null);
  const submitting = useRef(false);
  const [selected, setSelected] = useState<number[]>([]);
  const [batchBusy, setBatchBusy] = useState(false);
  const [batchResult, setBatchResult] = useState<BatchKeyResult[] | null>(null);

  const load = useCallback(async (): Promise<boolean> => {
    setLoading(true);
    try {
      setKeys(await keyApi.list());
      setLoadFailed(false);
      return true;
    } catch {
      // 错误信息由页内错误块承载（走 i18n），不再把英文 "Network Error" 弹成 toast
      setLoadFailed(true);
      return false;
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // 选中项只保留当前列表还存在的密钥：刷新后消失的行不能被批量接口带走
  useEffect(() => {
    const alive = new Set(keys.map((k) => k.id));
    setSelected((prev) => {
      const next = prev.filter((id) => alive.has(id));
      return next.length === prev.length ? prev : next;
    });
  }, [keys]);

  function openCreate() {
    setEditing(null);
    setName('');
    setFormOpen(true);
  }

  function openEdit(k: ApiKey) {
    setEditing(k);
    setName(k.name);
    setFormOpen(true);
  }

  async function submit() {
    if (submitting.current) return;
    if (!name.trim()) {
      notify.err(t('keys.nameRequired'));
      return;
    }
    submitting.current = true;
    setBusy(true);
    try {
      if (editing) {
        await keyApi.update(editing.id, {name: name.trim()});
        notify.ok(t('keys.keyUpdated'));
      } else {
        const created = await keyApi.create({name: name.trim()});
        notify.ok(t('keys.keyCreated'));
        if (created.key) setIssued(created.key);
      }
      setFormOpen(false);
      if (!(await load())) {
        notify.warn(t('keys.createdButRefreshFailed'), t('keys.createdButRefreshFailedHint'));
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
      submitting.current = false;
    }
  }

  async function toggle(k: ApiKey) {
    try {
      await keyApi.update(k.id, {enabled: !k.enabled});
      notify.ok(k.enabled ? t('keys.disabled') : t('keys.enabled'));
      load();
    } catch (e) {
      notify.err(errText(e));
    }
  }

  const allSelected = keys.length > 0 && selected.length === keys.length;

  function toggleSelected(id: number) {
    setSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  }

  function toggleAll() {
    setSelected(allSelected ? [] : keys.map((k) => k.id));
  }

  async function runBatch(action: 'enable' | 'disable' | 'delete') {
    if (!selected.length || batchBusy) return;
    if (selected.length > BATCH_LIMIT) {
      notify.err(t('keys.batchTooMany', {max: BATCH_LIMIT}));
      return;
    }
    setBatchBusy(true);
    try {
      const r = await keyApi.batch(action, selected);
      const failed = r.results.filter((item) => !item.ok);
      setBatchResult(r.results);
      setSelected([]);
      const okCount = r.results.length - failed.length;
      if (failed.length) {
        notify.warn(t('keys.batchFailedTitle', {n: failed.length}), t('keys.batchSummary', {ok: okCount, fail: failed.length}));
      } else {
        notify.ok(t('keys.batchAllOk'), t('keys.batchSummary', {ok: okCount, fail: 0}));
      }
      await load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBatchBusy(false);
    }
  }

  // 客户端才能拿到当前 origin，静态导出阶段为空
  const baseUrl = typeof window !== 'undefined' ? `${window.location.origin}${BASE_PATH}` : '';

  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader
        title={t('keys.title')}
        description={t('keys.description')}
        actions={
          isAdmin ? (
            <Button size="sm" className="rounded-full" onClick={openCreate}>
              <Plus />
              {t('keys.newKey')}
            </Button>
          ) : undefined
        }
      />

      {/* 拉取失败单独成块：不能退化成「暂无密钥」，那会让用户以为密钥被删了 */}
      {loadFailed && !loading && (
        <section className="flex flex-wrap items-start justify-between gap-3 rounded-[20px] border border-destructive/30 bg-destructive/5 px-4 py-3.5">
          <div className="flex min-w-0 items-start gap-2.5">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive-text" />
            <div className="min-w-0">
              <div className="text-sm font-medium text-destructive-text">{t('keys.loadFailedTitle')}</div>
              <div className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                {t('keys.loadFailedDesc')}
              </div>
            </div>
          </div>
          <Button size="sm" variant="outline" className="shrink-0 rounded-full" onClick={() => load()} disabled={loading}>
            <RotateCcw className="mr-1.5 h-3 w-3" />
            {t('common.retry')}
          </Button>
        </section>
      )}

      {/* 批量操作条：admin-only，与账号页同一套视觉；viewer 不渲染任何批量元素 */}
      {isAdmin && (selected.length > 0 || batchResult) && (
        <section className="rounded-[20px] bg-muted px-4 py-3">
          {selected.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="mr-1 text-xs font-medium tabular-nums">
                {batchBusy ? t('keys.batchRunning') : t('keys.batchSelected', {n: selected.length})}
              </span>
              <Button
                size="sm"
                variant="outline"
                className="h-7 rounded-full text-[11px]"
                disabled={batchBusy}
                onClick={() => runBatch('disable')}
              >
                <Pause className="mr-1 h-3 w-3" />
                {t('keys.batchDisable')}
              </Button>
              <Button
                size="sm"
                variant="outline"
                className="h-7 rounded-full text-[11px]"
                disabled={batchBusy}
                onClick={() => runBatch('enable')}
              >
                <Play className="mr-1 h-3 w-3" />
                {t('keys.batchEnable')}
              </Button>
              <ConfirmDialog
                title={t('keys.batchDeleteTitle', {n: selected.length})}
                description={t('keys.batchDeleteDesc')}
                confirmText={t('keys.delete')}
                destructive
                onConfirm={() => runBatch('delete')}
                trigger={
                  <Button
                    size="sm"
                    variant="outline"
                    className="h-7 rounded-full text-[11px] text-destructive-text hover:text-destructive-text"
                    disabled={batchBusy}
                  >
                    <Trash2 className="mr-1 h-3 w-3" />
                    {t('keys.delete')}
                  </Button>
                }
              />
              <Button
                size="sm"
                variant="ghost"
                className="h-7 rounded-full text-[11px]"
                disabled={batchBusy}
                onClick={() => setSelected([])}
              >
                {t('keys.batchClear')}
              </Button>
            </div>
          )}

          {/* 逐条结果：失败按 id + 后端原因逐行列出，不做「部分成功」美化 */}
          {batchResult && (
            <div className={selected.length > 0 ? 'mt-2 border-t border-border/40 pt-2' : ''}>
              <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
                <span>
                  {t('keys.batchSummary', {
                    ok: batchResult.filter((item) => item.ok).length,
                    fail: batchResult.filter((item) => !item.ok).length,
                  })}
                </span>
                <span>{t('keys.batchLoadNote')}</span>
                <Button
                  variant="ghost"
                  size="sm"
                  className="ml-auto h-6 rounded-full text-[11px]"
                  onClick={() => setBatchResult(null)}
                >
                  {t('common.close')}
                </Button>
              </div>
              {batchResult.some((item) => !item.ok) && (
                <div className="mt-1 flex flex-col gap-0.5">
                  {batchResult.filter((item) => !item.ok).map((item) => (
                    <div key={item.id} className="flex flex-wrap items-baseline gap-1.5 text-[10px] leading-4">
                      <span className="font-mono text-foreground">{item.id}</span>
                      <span className="whitespace-normal text-destructive-text">
                        {item.error || t('keys.batchNoReason')}
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </section>
      )}

      <section className="overflow-hidden rounded-[20px] bg-muted">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              {isAdmin && (
                <TableHead scope="col" className="w-9 pl-4 pr-0">
                  <input
                    type="checkbox"
                    className="size-4 cursor-pointer align-middle accent-primary"
                    checked={allSelected}
                    onChange={toggleAll}
                    aria-label={t('keys.batchSelectAll')}
                  />
                </TableHead>
              )}
              <TableHead scope="col" className={`text-[11px] text-muted-foreground ${isAdmin ? 'pl-1' : 'pl-4'}`}>{t('metric.name')}</TableHead>
              <TableHead scope="col" className="hidden text-[11px] text-muted-foreground md:table-cell">{t('keys.colPrefix')}</TableHead>
              <TableHead scope="col" className="text-[11px] text-muted-foreground">{t('accounts.colStatus')}</TableHead>
              <TableHead scope="col" className="hidden text-[11px] text-muted-foreground md:table-cell">{t('keys.colCreated')}</TableHead>
              <TableHead scope="col" className="hidden text-[11px] text-muted-foreground md:table-cell">{t('keys.colLastUsed')}</TableHead>
              {isAdmin && <TableHead scope="col" className="pr-4 text-right text-[11px] text-muted-foreground">{t('accounts.colActions')}</TableHead>}
            </TableRow>
          </TableHeader>
          <TableBody>
            {keys.map((k) => (
              <TableRow key={k.id} className="border-b border-border/40">
                {isAdmin && (
                  <TableCell className="w-9 pl-4 pr-0">
                    <input
                      type="checkbox"
                      className="size-4 cursor-pointer align-middle accent-primary"
                      checked={selected.includes(k.id)}
                      onChange={() => toggleSelected(k.id)}
                      aria-label={t('keys.batchSelectRow', {name: k.name})}
                    />
                  </TableCell>
                )}
                <TableCell className={`text-sm font-medium ${isAdmin ? 'pl-1' : 'pl-4'}`}>
                  {k.name}
                  {k.name === WIZARD_KEY_NAME && (
                    <Badge variant="secondary" className="ml-1.5 rounded-full text-[10px]">
                      {t('keys.badgeFromWizard')}
                    </Badge>
                  )}
                </TableCell>
                <TableCell className="hidden font-mono text-xs text-muted-foreground md:table-cell">{k.prefix}…</TableCell>
                <TableCell>
                  {!k.enabled ? (
                    <Badge variant="secondary" className="rounded-full text-muted-foreground">{t('keys.badgeDisabled')}</Badge>
                  ) : (
                    <Badge variant="secondary" className="rounded-full text-emerald-700 dark:text-emerald-400">{t('keys.badgeOk')}</Badge>
                  )}
                </TableCell>
                <TableCell className="hidden text-xs text-muted-foreground md:table-cell">{fmtDateTime(k.created_at)}</TableCell>
                <TableCell className="hidden text-xs text-muted-foreground md:table-cell">
                  {k.last_used_at ? fmtDateTime(k.last_used_at) : t('keys.neverUsed')}
                </TableCell>
                {isAdmin && (
                  <TableCell className="pr-4">
                    <div className="flex justify-end gap-1">
                      <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md" title={t('keys.edit')} aria-label={t('keys.edit')} onClick={() => openEdit(k)}>
                        <Pencil className="h-3.5 w-3.5" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 rounded-md"
                        title={k.enabled ? t('keys.disable') : t('keys.enable')}
                        aria-label={k.enabled ? t('keys.disable') : t('keys.enable')}
                        onClick={() => toggle(k)}
                      >
                        {k.enabled ? <Ban className="h-3.5 w-3.5" /> : <CircleCheck className="h-3.5 w-3.5" />}
                      </Button>
                      <ConfirmDialog
                        title={t('keys.deleteTitle', {name: k.name})}
                        description={t('keys.deleteDesc')}
                        confirmText={t('keys.delete')}
                        destructive
                        onConfirm={async () => {
                          await keyApi.remove(k.id);
                          notify.ok(t('keys.deleted'));
                          load();
                        }}
                        trigger={
                          <Button variant="ghost" size="icon" className="h-7 w-7 rounded-md text-destructive-text hover:text-destructive-text" title={t('keys.delete')} aria-label={t('keys.delete')}>
                            <Trash2 className="h-3.5 w-3.5" />
                          </Button>
                        }
                      />
                    </div>
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>

        {!keys.length && !loading && !loadFailed && (
          <EmptyState
            icon={KeyRound}
            title={t('keys.emptyTitle')}
            description={t('keys.emptyDesc')}
            className="flex flex-col items-center justify-center py-16 text-center"
          >
            {isAdmin && (
              <Button className="mt-4 rounded-full" onClick={openCreate}>
                <Plus />
                {t('keys.newKey')}
              </Button>
            )}
          </EmptyState>
        )}
      </section>

      {/* 新建 / 改名 */}
      <Dialog open={formOpen} onOpenChange={setFormOpen}>
        <DialogContent className="max-w-[520px]">
          <DialogHeader>
            <DialogTitle>{editing ? t('keys.editTitle') : t('keys.createTitle')}</DialogTitle>
            <DialogDescription>
              {editing ? t('keys.editDesc') : t('keys.createDesc')}
            </DialogDescription>
          </DialogHeader>
          <DialogBody>
            <div className="space-y-1.5 px-6 pb-2">
              <Label htmlFor="key-name" className="text-[11px] text-muted-foreground">{t('keys.name')}</Label>
              <Input id="key-name" value={name} onChange={(e) => setName(e.target.value)} placeholder={t('keys.namePlaceholder')} />
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="outline" className="rounded-full" onClick={() => setFormOpen(false)}>
              {t('common.cancel')}
            </Button>
            <Button className="rounded-full" onClick={submit} disabled={busy}>
              {editing ? t('common.save') : t('keys.create')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 一次性展示新密钥 */}
      <Dialog open={!!issued} onOpenChange={(v) => !v && setIssued(null)}>
        <DialogContent className="max-w-[520px]">
          <DialogHeader>
            <DialogTitle>{t('keys.createdTitle')}</DialogTitle>
            <DialogDescription>{t('keys.createdDesc')}</DialogDescription>
          </DialogHeader>
          <div className="space-y-3 px-6 pb-2">
            <div className="flex items-center gap-2 rounded-2xl bg-muted p-3">
              <code className="min-w-0 flex-1 break-all font-mono text-xs">{issued}</code>
              <CopyButton value={issued || ''} size="sm" showLabel label={t('keys.copyKey')} />
            </div>
            <div className="flex items-center gap-2">
              <span className="text-[11px] text-muted-foreground">{t('keys.baseUrl')}</span>
              <code className="min-w-0 flex-1 break-all font-mono text-[11px]">{baseUrl}/v1</code>
              <CopyButton value={`${baseUrl}/v1`} title={t('keys.copyBaseUrl')} />
            </div>
          </div>
          <DialogFooter>
            <Button className="rounded-full" onClick={() => setIssued(null)}>
              {t('keys.savedIt')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
