'use client';

import {useCallback, useEffect, useState} from 'react';
import {
  AlertTriangle,
  CloudOff,
  CloudUpload,
  KeyRound,
  Loader2,
  RefreshCw,
  Save,
  ShieldAlert,
  Trash2,
} from 'lucide-react';
import {errText, runtimeConfigApi, systemApi} from '@/lib/api';
import type {
  RemoteBackupConfig,
  RemoteBackupList,
  RemoteBackupListFailure,
  RemoteBackupObject,
  RemoteBackupState,
} from '@/lib/types';
import {notify} from '@/lib/toast';
import {fmtDateTime} from '@/lib/format';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Switch} from '@/components/ui/switch';
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
const KEEP_MIN = 1;
const KEEP_MAX = 90;
const KEEP_FALLBACK = 14;

function fmtBytes(bytes: number): string {
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < SIZE_UNITS.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${SIZE_UNITS[unit]}`;
}

/** last_modified 是 S3 的 ISO 串；状态里的时间戳是 epoch 秒，两种都要能显示 */
function fmtRemoteTime(value: number | string | undefined): string {
  if (typeof value === 'number') return fmtDateTime(value);
  if (!value) return '—';
  const ms = Date.parse(value);
  return Number.isNaN(ms) ? '—' : fmtDateTime(ms);
}

/** errText 碰到 {ok:false,error:{message}} 这类对象体会回吐对象；这里统一收敛成字符串 */
function requestError(e: unknown, fallback: string): string {
  const body = (e as {response?: {data?: unknown}})?.response?.data;
  const detail = (body as {detail?: unknown})?.detail;
  const message = (body as {error?: {message?: unknown}})?.error?.message;
  if (typeof detail === 'string' && detail) return detail;
  if (typeof message === 'string' && message) return message;
  const text: unknown = errText(e);
  return typeof text === 'string' && text ? text : fallback;
}

/** 状态可能在顶层平铺，也可能嵌在 state 里，两种响应形态都认 */
function statusOf(cfg: RemoteBackupConfig): RemoteBackupState {
  return cfg.state ?? cfg;
}

/** 列表给的是带 prefix 的完整 key（...zip.enc）；恢复/删除端点收的是归档名（...zip） */
function archiveNameOf(object: RemoteBackupObject): string {
  const tail = object.key.split('/').pop() ?? object.key;
  return tail.endsWith('.enc') ? tail.slice(0, -4) : tail;
}

interface FormState {
  endpoint: string;
  bucket: string;
  region: string;
  prefix: string;
  path_style: boolean;
  keep: string;
}

function formOf(cfg: RemoteBackupConfig): FormState {
  return {
    endpoint: cfg.endpoint ?? '',
    bucket: cfg.bucket ?? '',
    region: cfg.region ?? '',
    prefix: cfg.prefix ?? '',
    path_style: cfg.path_style ?? true,
    keep: String(cfg.keep ?? cfg.settings?.keep ?? KEEP_FALLBACK),
  };
}

export function BackupRemoteSection() {
  const t = useT();
  const [cfg, setCfg] = useState<RemoteBackupConfig | null>(null);
  const [form, setForm] = useState<FormState>({
    endpoint: '',
    bucket: '',
    region: '',
    prefix: '',
    path_style: true,
    keep: String(KEEP_FALLBACK),
  });
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState('');
  const [saving, setSaving] = useState(false);

  const [objects, setObjects] = useState<RemoteBackupObject[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState('');
  const [uploading, setUploading] = useState(false);

  // 恢复/删除都走二次确认；恢复必须手动输入对象名
  const [restoreTarget, setRestoreTarget] = useState<RemoteBackupObject | null>(null);
  const [typed, setTyped] = useState('');
  const [restoring, setRestoring] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<RemoteBackupObject | null>(null);
  const [deleting, setDeleting] = useState(false);

  const loadConfig = useCallback(async () => {
    setLoading(true);
    setLoadError('');
    try {
      const r = await systemApi.remoteConfig();
      setCfg(r);
      setForm(formOf(r));
    } catch (e) {
      setLoadError(requestError(e, t('backupPanel.remote.requestFailed')));
    } finally {
      setLoading(false);
    }
  }, [t]);

  const loadObjects = useCallback(async () => {
    setListLoading(true);
    setListError('');
    try {
      const r = await systemApi.remoteBackups();
      const failed = r.status >= 400 ? (r.data as RemoteBackupListFailure).detail : undefined;
      const body = failed ?? (r.data as RemoteBackupList);
      setObjects(body?.objects ?? []);
      if (r.status >= 400) {
        setListError(
          failed?.error?.message || `${t('backupPanel.remote.listFailed')} · HTTP ${r.status}`,
        );
      }
      // 列表接口同时回状态：就地刷新，省一次配置请求
      if (body) {
        setCfg((prev) =>
          prev
            ? {
                ...prev,
                state: body.state ?? prev.state,
                configured: body.configured ?? prev.configured,
                encryption_key_configured:
                  body.encryption_key_configured ?? prev.encryption_key_configured,
                s3_reachable:
                  body.s3_reachable === undefined
                    ? prev.s3_reachable
                    : body.s3_reachable ?? undefined,
              }
            : prev,
        );
      }
    } catch (e) {
      setListError(requestError(e, t('backupPanel.remote.requestFailed')));
    } finally {
      setListLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void loadConfig();
    void loadObjects();
  }, [loadConfig, loadObjects]);

  const refreshAll = () => {
    void loadConfig();
    void loadObjects();
  };

  const save = useCallback(async () => {
    const keep = Math.min(KEEP_MAX, Math.max(KEEP_MIN, Math.round(Number(form.keep) || KEEP_FALLBACK)));
    setSaving(true);
    try {
      await runtimeConfigApi.save({backup_remote_keep: keep});
      const saved = await systemApi.saveRemoteConfig({
        endpoint: form.endpoint.trim(),
        bucket: form.bucket.trim(),
        region: form.region.trim(),
        prefix: form.prefix.trim(),
        path_style: form.path_style,
      });
      setCfg(saved);
      setForm(formOf(saved));
      notify.ok(t('backupPanel.remote.saveDone'));
      void loadObjects();
    } catch (e) {
      notify.err(requestError(e, t('backupPanel.remote.requestFailed')));
      // keep 可能已保存而连接参数没保存：以服务端为准回显，避免界面说谎
      void loadConfig();
    } finally {
      setSaving(false);
    }
  }, [form, loadConfig, loadObjects, t]);

  const upload = useCallback(async () => {
    setUploading(true);
    try {
      const r = await systemApi.uploadRemoteBackup();
      notify.ok(t('backupPanel.remote.uploadDone'), r.object ?? '');
      await Promise.all([loadConfig(), loadObjects()]);
    } catch (e) {
      notify.err(requestError(e, t('backupPanel.remote.requestFailed')));
    } finally {
      setUploading(false);
    }
  }, [loadConfig, loadObjects, t]);

  const restore = useCallback(async () => {
    if (!restoreTarget) return;
    const name = archiveNameOf(restoreTarget);
    setRestoring(true);
    try {
      const r = await systemApi.restoreRemoteBackup(name);
      notify.ok(t('backupPanel.remote.restoreDone'), r.restored ?? r.name ?? name);
      setRestoreTarget(null);
      setTyped('');
      await Promise.all([loadConfig(), loadObjects()]);
    } catch (e) {
      notify.err(requestError(e, t('backupPanel.remote.requestFailed')));
    } finally {
      setRestoring(false);
    }
  }, [loadConfig, loadObjects, restoreTarget, t]);

  const remove = useCallback(async () => {
    if (!deleteTarget) return;
    const name = archiveNameOf(deleteTarget);
    setDeleting(true);
    try {
      await systemApi.deleteRemoteBackup(name);
      notify.ok(t('backupPanel.remote.deleteDone'), name);
      setDeleteTarget(null);
      await Promise.all([loadConfig(), loadObjects()]);
    } catch (e) {
      notify.err(requestError(e, t('backupPanel.remote.requestFailed')));
    } finally {
      setDeleting(false);
    }
  }, [deleteTarget, loadConfig, loadObjects, t]);

  const st = cfg ? statusOf(cfg) : null;
  const maskedKey = cfg?.access_key_id_masked ?? '';
  // endpoint/bucket 存了但环境变量没凭据：这时的「未配置」原因不同，要指名该设哪几个变量
  const credsMissing = !!cfg && !cfg.configured && !!cfg.endpoint && !!cfg.bucket && !maskedKey;
  const uploadBlocked = cfg
    ? !cfg.encryption_key_configured
      ? t('backupPanel.remote.uploadNoKey')
      : !cfg.configured
        ? t('backupPanel.remote.uploadNotConfigured')
        : ''
    : '';
  const uploadDisabled = !cfg || uploading || restoring || deleting || !!uploadBlocked;
  const reachability = cfg ? cfg.s3_reachable ?? st?.s3_reachable : undefined;
  const canRestore = !!restoreTarget && typed === archiveNameOf(restoreTarget) && !restoring;

  return (
    <div className="mt-5 border-t border-border/60 pt-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-2.5">
          <CloudUpload className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <div>
            <div className="text-sm font-medium">{t('backupPanel.remote.title')}</div>
            <div className="mt-1 text-[11px] leading-5 text-muted-foreground">
              {t('backupPanel.remote.desc')}
            </div>
          </div>
        </div>
        <Button
          size="sm"
          variant="ghost"
          className="rounded-full"
          onClick={refreshAll}
          disabled={loading || listLoading || saving || uploading || restoring || deleting}
          title={t('backupPanel.remote.refreshTitle')}
        >
          <RefreshCw className={`mr-1 h-3.5 w-3.5 ${loading || listLoading ? 'animate-spin' : ''}`} />
          {t('backupPanel.refresh')}
        </Button>
      </div>

      {loading ? (
        <div className="mt-4 flex items-center gap-2 text-[11.5px] text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          {t('backupPanel.remote.loadingConfig')}
        </div>
      ) : loadError || !cfg ? (
        <div className="mt-4 flex items-start gap-2 rounded-[16px] border border-destructive/30 bg-destructive/5 px-3 py-2.5">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive-text" />
          <div className="min-w-0 flex-1">
            <div className="text-[11.5px] leading-5 whitespace-pre-wrap break-words">
              {loadError || t('backupPanel.remote.loadFailed')}
            </div>
            <Button size="sm" variant="outline" className="mt-2 rounded-full" onClick={refreshAll}>
              <RefreshCw className="mr-1 h-3.5 w-3.5" />
              {t('backupPanel.retry')}
            </Button>
          </div>
        </div>
      ) : (
        <div className="mt-4 space-y-3">
          {/* 三类故障分开提示：缺密钥 / 认证被拒 / 连不上存储 */}
          {!cfg.encryption_key_configured && (
            <div className="flex items-start gap-2 rounded-[16px] border border-amber-500/30 bg-amber-500/10 px-3 py-2.5">
              <KeyRound className="mt-0.5 h-3.5 w-3.5 shrink-0 text-warning-text" />
              <div className="min-w-0">
                <div className="text-[11.5px] font-medium leading-5">
                  {t('backupPanel.remote.keyMissingTitle')}
                </div>
                <div className="mt-0.5 text-[11px] leading-5 text-muted-foreground">
                  {t('backupPanel.remote.keyMissingBody', {env: 'UNIFIED_BACKUP_ENCRYPTION_KEY'})}
                </div>
              </div>
            </div>
          )}

          {st?.auth_blocked && (
            <div className="flex items-start gap-2 rounded-[16px] border border-red-500/30 bg-red-500/10 px-3 py-2.5">
              <ShieldAlert className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive-text" />
              <div className="min-w-0">
                <div className="text-[11.5px] font-medium leading-5">
                  {t('backupPanel.remote.authBlockedTitle')}
                </div>
                <div className="mt-0.5 text-[11px] leading-5 text-muted-foreground">
                  {t('backupPanel.remote.authBlockedBody')}
                </div>
                {st.last_error && (
                  <div className="mt-1 whitespace-pre-wrap break-words font-mono text-[11px] leading-5 text-destructive-text">
                    {st.last_error}
                  </div>
                )}
                {st.last_error_code && (
                  <Badge variant="outline" className="mt-1 text-[10px]">
                    {st.last_error_code}
                  </Badge>
                )}
              </div>
            </div>
          )}

          {reachability === false && (
            <div className="flex items-start gap-2 rounded-[16px] border border-blue-500/30 bg-blue-500/10 px-3 py-2.5">
              <CloudOff className="mt-0.5 h-3.5 w-3.5 shrink-0 text-blue-500" />
              <div className="min-w-0">
                <div className="text-[11.5px] font-medium leading-5">
                  {t('backupPanel.remote.unreachableTitle')}
                </div>
                <div className="mt-0.5 text-[11px] leading-5 text-muted-foreground">
                  {t('backupPanel.remote.unreachableBody')}
                </div>
                {st?.s3_last_error && (
                  <div className="mt-1 whitespace-pre-wrap break-words font-mono text-[11px] leading-5 text-blue-600 dark:text-blue-400">
                    {t('backupPanel.remote.unreachableReason')}：{st.s3_last_error}
                  </div>
                )}
              </div>
            </div>
          )}

          {!cfg.configured && (
            <div className="rounded-[16px] border border-border/70 bg-background/40 px-3 py-2.5">
              <div className="text-[11.5px] font-medium leading-5">
                {credsMissing
                  ? t('backupPanel.remote.credsMissingTitle')
                  : t('backupPanel.remote.notConfiguredTitle')}
              </div>
              <div className="mt-0.5 text-[11px] leading-5 text-muted-foreground">
                {credsMissing
                  ? t('backupPanel.remote.credsMissingBody', {
                      access: 'UNIFIED_BACKUP_S3_ACCESS_KEY_ID',
                      secret: 'UNIFIED_BACKUP_S3_SECRET_ACCESS_KEY',
                      token: 'UNIFIED_BACKUP_S3_SESSION_TOKEN',
                    })
                  : t('backupPanel.remote.notConfiguredBody')}
              </div>
            </div>
          )}

          {/* 状态 */}
          <div className="rounded-[16px] border border-border/60 bg-background/40 px-3 py-2.5">
            <div className="text-[11.5px] font-medium">{t('backupPanel.remote.statusTitle')}</div>
            <div className="mt-1.5 grid gap-x-4 gap-y-1 text-[11px] leading-5 text-muted-foreground sm:grid-cols-2">
              <div>
                {t('backupPanel.remote.statusLastAttempt')}
                <span className="ml-1 text-foreground">{fmtRemoteTime(st?.last_attempt_at)}</span>
              </div>
              <div>
                {t('backupPanel.remote.statusLastOk')}
                <span className="ml-1 text-foreground">{fmtRemoteTime(st?.last_ok_at)}</span>
              </div>
              <div className="break-all">
                {t('backupPanel.remote.statusLastUploaded')}
                <span className="ml-1 font-mono text-foreground">
                  {st?.last_uploaded || t('backupPanel.remote.never')}
                </span>
              </div>
              <div>
                {t('backupPanel.remote.statusNextRetry')}
                <span className="ml-1 text-foreground">{fmtRemoteTime(st?.next_retry_at)}</span>
              </div>
            </div>
            {st?.last_error && !st.auth_blocked && (
              <div className="mt-1.5 text-[11px] leading-5">
                <span className="text-muted-foreground">{t('backupPanel.remote.lastError')}</span>
                <div className="whitespace-pre-wrap break-words font-mono text-destructive-text">
                  {st.last_error}
                </div>
              </div>
            )}
          </div>

          {/* 连接参数 */}
          <div className="rounded-[16px] border border-border/60 bg-background/40 p-3">
            <div className="text-[11.5px] font-medium">{t('backupPanel.remote.configTitle')}</div>
            <div className="mt-2.5 grid gap-3 sm:grid-cols-2">
              <label className="block">
                <span className="text-[11px] text-muted-foreground">
                  {t('backupPanel.remote.endpoint')}
                </span>
                <Input
                  className="mt-1"
                  value={form.endpoint}
                  autoComplete="off"
                  placeholder="https://s3.example.com"
                  onChange={(e) => setForm((f) => ({...f, endpoint: e.target.value}))}
                />
                <span className="mt-1 block text-[10.5px] leading-4 text-muted-foreground">
                  {t('backupPanel.remote.endpointHint')}
                </span>
              </label>
              <label className="block">
                <span className="text-[11px] text-muted-foreground">
                  {t('backupPanel.remote.bucket')}
                </span>
                <Input
                  className="mt-1"
                  value={form.bucket}
                  autoComplete="off"
                  onChange={(e) => setForm((f) => ({...f, bucket: e.target.value}))}
                />
              </label>
              <label className="block">
                <span className="text-[11px] text-muted-foreground">
                  {t('backupPanel.remote.region')}
                </span>
                <Input
                  className="mt-1"
                  value={form.region}
                  autoComplete="off"
                  onChange={(e) => setForm((f) => ({...f, region: e.target.value}))}
                />
              </label>
              <label className="block">
                <span className="text-[11px] text-muted-foreground">
                  {t('backupPanel.remote.prefix')}
                </span>
                <Input
                  className="mt-1"
                  value={form.prefix}
                  autoComplete="off"
                  onChange={(e) => setForm((f) => ({...f, prefix: e.target.value}))}
                />
              </label>
              <label className="block">
                <span className="text-[11px] text-muted-foreground">
                  {t('backupPanel.remote.keep')}
                </span>
                <Input
                  className="mt-1"
                  type="number"
                  min={KEEP_MIN}
                  max={KEEP_MAX}
                  value={form.keep}
                  onChange={(e) => setForm((f) => ({...f, keep: e.target.value}))}
                />
                <span className="mt-1 block text-[10.5px] leading-4 text-muted-foreground">
                  {t('backupPanel.remote.keepHint')}
                </span>
              </label>
              <div className="flex items-center justify-between gap-3 self-start rounded-xl bg-muted/55 px-3 py-2">
                <span className="text-[11px] text-muted-foreground">
                  {t('backupPanel.remote.pathStyle')}
                </span>
                <Switch
                  aria-label={t('backupPanel.remote.pathStyle')}
                  checked={form.path_style}
                  onCheckedChange={(v) => setForm((f) => ({...f, path_style: v}))}
                />
              </div>
            </div>

            <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-xl bg-muted/55 px-3 py-2">
              <span className="text-[11px] text-muted-foreground">
                {t('backupPanel.remote.accessKey')}
              </span>
              <span className="flex items-center gap-1.5">
                <span className="font-mono text-[11px]">
                  {maskedKey || t('backupPanel.remote.accessKeyEmpty')}
                </span>
                {cfg.credential_source === 'env' && (
                  <Badge variant="outline" className="text-[10px]">
                    {t('backupPanel.remote.credentialSourceEnv')}
                  </Badge>
                )}
              </span>
            </div>
            <div className="mt-2 text-[10.5px] leading-4 text-muted-foreground">
              {t('backupPanel.remote.keysNotice')}
            </div>

            <div className="mt-3 flex justify-end">
              <Button size="sm" className="rounded-full" onClick={() => void save()} disabled={saving}>
                {saving ? (
                  <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                ) : (
                  <Save className="mr-1 h-3.5 w-3.5" />
                )}
                {saving ? t('backupPanel.remote.saving') : t('backupPanel.remote.save')}
              </Button>
            </div>
          </div>

          {/* 远端对象 */}
          <div className="overflow-hidden rounded-[16px] border border-border/60 bg-background/40">
            <div className="flex flex-wrap items-center justify-between gap-2 px-3 py-2.5">
              <div className="text-[11.5px] font-medium">{t('backupPanel.remote.listTitle')}</div>
              <div className="flex flex-wrap items-center justify-end gap-2">
                {uploadBlocked && (
                  <span className="text-[10.5px] leading-4 text-muted-foreground">{uploadBlocked}</span>
                )}
                <Button
                  size="sm"
                  variant="outline"
                  className="rounded-full"
                  disabled={uploadDisabled}
                  title={uploadBlocked || undefined}
                  onClick={() => void upload()}
                >
                  {uploading ? (
                    <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                  ) : (
                    <CloudUpload className="mr-1 h-3.5 w-3.5" />
                  )}
                  {uploading ? t('backupPanel.remote.uploading') : t('backupPanel.remote.uploadNow')}
                </Button>
              </div>
            </div>

            {listLoading ? (
              <div className="flex items-center gap-2 border-t border-border/60 px-3 py-6 text-[11.5px] text-muted-foreground">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                {t('backupPanel.remote.loadingList')}
              </div>
            ) : listError ? (
              <div className="flex items-start gap-2 border-t border-border/60 px-3 py-2.5">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive-text" />
                <div className="min-w-0 flex-1">
                  <div className="text-[11.5px] leading-5 whitespace-pre-wrap break-words">
                    {listError}
                  </div>
                  <Button
                    size="sm"
                    variant="outline"
                    className="mt-2 rounded-full"
                    onClick={() => void loadObjects()}
                  >
                    <RefreshCw className="mr-1 h-3.5 w-3.5" />
                    {t('backupPanel.retry')}
                  </Button>
                </div>
              </div>
            ) : objects.length === 0 ? (
              <div className="border-t border-border/60 px-3 py-6 text-center">
                <div className="text-xs font-medium">{t('backupPanel.remote.empty')}</div>
                <div className="mt-1 text-[11px] leading-5 text-muted-foreground">
                  {t('backupPanel.remote.emptyDesc')}
                </div>
              </div>
            ) : (
              <div className="divide-y divide-border/60 border-t border-border/60">
                {objects.map((obj) => (
                  <div key={obj.key} className="flex flex-wrap items-center gap-2 px-3 py-2.5">
                    <div className="min-w-0 flex-1">
                      <div className="truncate font-mono text-[11.5px]" title={obj.key}>
                        {archiveNameOf(obj)}
                      </div>
                      <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-muted-foreground">
                        <span>{fmtRemoteTime(obj.last_modified)}</span>
                        <span>·</span>
                        <span>{fmtBytes(obj.size_bytes)}</span>
                        {obj.has_sidecar === true && (
                          <Badge variant="outline" className="text-[10px]">
                            {t('backupPanel.remote.sidecarYes')}
                          </Badge>
                        )}
                        {obj.has_sidecar === false && (
                          <Badge variant="outline" className="text-[10px]">
                            {t('backupPanel.remote.sidecarNo')}
                          </Badge>
                        )}
                      </div>
                    </div>
                    <Button
                      size="sm"
                      variant="outline"
                      className="rounded-full"
                      disabled={restoring || deleting}
                      onClick={() => {
                        setRestoreTarget(obj);
                        setTyped('');
                      }}
                    >
                      {t('backupPanel.remote.restore')}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="rounded-full text-destructive-text"
                      disabled={restoring || deleting}
                      onClick={() => setDeleteTarget(obj)}
                    >
                      <Trash2 className="mr-1 h-3.5 w-3.5" />
                      {t('backupPanel.remote.delete')}
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}

      {/* 从远端恢复：必须手动输入对象名，逐字不符则禁用 */}
      <AlertDialog
        open={!!restoreTarget}
        onOpenChange={(open) => {
          if (!open && !restoring) {
            setRestoreTarget(null);
            setTyped('');
          }
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('backupPanel.remote.restoreTitle')}</AlertDialogTitle>
            <AlertDialogDescription className="space-y-2">
              <span className="block">{t('backupPanel.remote.restoreWarn')}</span>
              <span className="block">{t('backupPanel.remote.restoreLocalWarn')}</span>
              {restoreTarget && (
                <span className="block break-all font-mono text-[11.5px] text-foreground">
                  {archiveNameOf(restoreTarget)}
                </span>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="mt-1 space-y-1.5">
            <label
              htmlFor="backup-remote-confirm-name"
              className="block text-[11.5px] leading-5 text-muted-foreground"
            >
              {t('backupPanel.remote.restoreTypeName')}
            </label>
            <Input
              id="backup-remote-confirm-name"
              autoFocus
              autoComplete="off"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              placeholder={restoreTarget ? archiveNameOf(restoreTarget) : ''}
              disabled={restoring}
              className="bg-background font-mono"
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
                e.preventDefault();
                if (canRestore) void restore();
              }}
            >
              {restoring ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
              {restoring ? t('backupPanel.remote.restoring') : t('backupPanel.remote.restoreConfirm')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>

      {/* 删除远端对象：删除后会连旁注一起消失 */}
      <AlertDialog
        open={!!deleteTarget}
        onOpenChange={(open) => {
          if (!open && !deleting) setDeleteTarget(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('backupPanel.remote.deleteTitle')}</AlertDialogTitle>
            <AlertDialogDescription className="space-y-2">
              <span className="block">{t('backupPanel.remote.deleteWarn')}</span>
              {deleteTarget && (
                <span className="block break-all font-mono text-[11.5px] text-foreground">
                  {archiveNameOf(deleteTarget)}
                </span>
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleting}>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              disabled={deleting}
              className="bg-destructive text-white hover:bg-destructive/90"
              onClick={(e) => {
                e.preventDefault();
                if (!deleting) void remove();
              }}
            >
              {deleting ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
              {deleting ? t('backupPanel.remote.deleting') : t('backupPanel.remote.deleteConfirm')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
