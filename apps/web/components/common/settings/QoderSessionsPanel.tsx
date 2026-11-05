'use client';

import {useMemo, useState} from 'react';
import {ArrowDownToLine, ArrowUpFromLine} from 'lucide-react';
import {qoderSessionsApi, errText} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';

type Reply = {kind: 'export' | 'import'; status: number; ok: boolean; body: string};

/**
 * qoder_sidecar 的会话搬运（上游 SessionCopy 能力）。
 *
 * 这一块只做一件事：原样转发 body，原样显示 sidecar 的回包。
 * 特别要说明的是导出——包落在 **sidecar 主机**的 outPath 上，本仓不产生可下载文件，
 * 所以不给「下载」按钮，也不假装能拿到内容。
 */
export function QoderSessionsPanel() {
  const t = useT();
  const [outPath, setOutPath] = useState('');
  const [exportUid, setExportUid] = useState('');
  const [sessionIds, setSessionIds] = useState('');
  const [exportRegion, setExportRegion] = useState('');
  const [importPath, setImportPath] = useState('');
  const [importUid, setImportUid] = useState('');
  const [importRegion, setImportRegion] = useState('');
  const [busy, setBusy] = useState<'export' | 'import' | null>(null);
  const [reply, setReply] = useState<Reply | null>(null);
  const [lastExportPath, setLastExportPath] = useState('');

  const pretty = useMemo(() => {
    if (!reply) return '';
    try {
      return JSON.stringify(JSON.parse(reply.body), null, 2);
    } catch {
      return reply.body;
    }
  }, [reply]);

  async function runExport() {
    if (busy || !outPath.trim()) return;
    setBusy('export');
    try {
      const ids = sessionIds.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
      const r = await qoderSessionsApi.export({
        outPath: outPath.trim(),
        ...(exportUid.trim() ? {uid: exportUid.trim()} : {}),
        ...(ids.length ? {sessionIds: ids} : {}),
        ...(exportRegion.trim() ? {region: exportRegion.trim()} : {}),
      });
      setReply({kind: 'export', ...r});
      if (r.ok) {
        setLastExportPath(outPath.trim());
        notify.ok(t('providers.qoderExportDone'));
      } else {
        notify.err(t('providers.qoderFailed', {status: r.status}));
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(null);
    }
  }

  async function runImport() {
    if (busy || !importPath.trim()) return;
    setBusy('import');
    try {
      const r = await qoderSessionsApi.import({
        path: importPath.trim(),
        ...(importUid.trim() ? {uid: importUid.trim()} : {}),
        ...(importRegion.trim() ? {region: importRegion.trim()} : {}),
      });
      setReply({kind: 'import', ...r});
      if (r.ok) {
        notify.ok(t('providers.qoderImportDone'));
      } else {
        notify.err(t('providers.qoderFailed', {status: r.status}));
      }
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mt-3 rounded-2xl bg-background/60 p-3">
      <div className="text-[11px] font-medium">{t('providers.qoderTitle')}</div>
      <div className="mt-1 rounded-xl bg-muted px-3 py-2 text-[10px] leading-4 text-muted-foreground">
        {t('providers.qoderHint')}
      </div>

      <div className="mt-3 grid grid-cols-1 gap-3 md:grid-cols-2">
        {/* 导出 */}
        <div className="space-y-2">
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <ArrowDownToLine className="h-3.5 w-3.5" />
            {t('providers.qoderExportTitle')}
          </div>
          <Input
            value={outPath}
            onChange={(e) => setOutPath(e.target.value)}
            placeholder={t('providers.qoderOutPathPh')}
            aria-label={t('providers.qoderOutPath')}
          />
          <div className="grid grid-cols-2 gap-2">
            <Input
              value={exportUid}
              onChange={(e) => setExportUid(e.target.value)}
              placeholder={t('providers.qoderUidPh')}
              aria-label={t('providers.qoderUid')}
            />
            <Input
              value={exportRegion}
              onChange={(e) => setExportRegion(e.target.value)}
              placeholder={t('providers.qoderRegionPh')}
              aria-label={t('providers.qoderRegion')}
            />
          </div>
          <Input
            value={sessionIds}
            onChange={(e) => setSessionIds(e.target.value)}
            placeholder={t('providers.qoderSessionIdsPh')}
            aria-label={t('providers.qoderSessionIds')}
          />
          <Button
            size="sm"
            className="w-full rounded-full text-[11px]"
            disabled={busy !== null || !outPath.trim()}
            onClick={runExport}
          >
            {busy === 'export' ? t('providers.qoderRunning') : t('providers.qoderRun')}
          </Button>
        </div>

        {/* 导入 */}
        <div className="space-y-2">
          <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
            <ArrowUpFromLine className="h-3.5 w-3.5" />
            {t('providers.qoderImportTitle')}
          </div>
          <Input
            value={importPath}
            onChange={(e) => setImportPath(e.target.value)}
            placeholder={t('providers.qoderImportPathPh')}
            aria-label={t('providers.qoderImportPath')}
          />
          <div className="grid grid-cols-2 gap-2">
            <Input
              value={importUid}
              onChange={(e) => setImportUid(e.target.value)}
              placeholder={t('providers.qoderUidPh')}
              aria-label={t('providers.qoderUid')}
            />
            <Input
              value={importRegion}
              onChange={(e) => setImportRegion(e.target.value)}
              placeholder={t('providers.qoderRegionPh')}
              aria-label={t('providers.qoderRegion')}
            />
          </div>
          <div className="rounded-xl bg-muted px-3 py-2 text-[10px] leading-4 text-muted-foreground">
            {t('providers.qoderImportHint')}
          </div>
          <Button
            size="sm"
            variant="outline"
            className="w-full rounded-full text-[11px]"
            disabled={busy !== null || !importPath.trim()}
            onClick={runImport}
          >
            {busy === 'import' ? t('providers.qoderRunning') : t('providers.qoderRun')}
          </Button>
        </div>
      </div>

      {/* sidecar 原始回包：成功失败都照抄，不替上游改写措辞 */}
      {reply && (
        <div className="mt-3 border-t border-border/40 pt-2">
          <div className="flex flex-wrap items-center gap-2 text-[10px] text-muted-foreground">
            <span>{t('providers.qoderReply')}</span>
            <Badge variant={reply.ok ? 'secondary' : 'destructive'} className="rounded-full tabular-nums">
              {t('providers.qoderStatus', {status: reply.status})}
            </Badge>
            {reply.ok && reply.kind === 'export' && (
              <span>{t('providers.qoderExportNote', {path: lastExportPath})}</span>
            )}
          </div>
          {reply.body ? (
            <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-all rounded-xl bg-muted px-3 py-2 text-[10px] leading-4">
              {pretty}
            </pre>
          ) : (
            <div className="mt-1 text-[10px] text-muted-foreground">{t('providers.qoderEmptyReply')}</div>
          )}
        </div>
      )}
    </div>
  );
}
