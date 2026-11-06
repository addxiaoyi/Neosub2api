'use client';

import {useCallback, useRef, useState} from 'react';
import {AlertTriangle, CircleCheck, Download, FileJson, Loader2, Upload} from 'lucide-react';
import {configTransferApi, errText} from '@/lib/api';
import type {ConfigTransferEntry, ConfigTransferReport} from '@/lib/api';
import {notify} from '@/lib/toast';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
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

type Mode = 'merge' | 'overwrite';

function Section({
  title,
  entries,
  tone,
}: {
  title: string;
  entries: ConfigTransferEntry[];
  tone: string;
}) {
  if (entries.length === 0) return null;
  return (
    <div className="rounded-[14px] border border-border/60 bg-background/40 px-3 py-2">
      <div className="flex items-center gap-2">
        <span className={`h-1.5 w-1.5 rounded-full ${tone}`} />
        <div className="text-[11.5px] font-medium">
          {title} · {entries.length}
        </div>
      </div>
      <div className="mt-1 space-y-0.5">
        {entries.slice(0, 6).map((item) => (
          <div key={item.path} className="break-all font-mono text-[10.5px] leading-5 text-muted-foreground">
            {item.path} {item.key}
            {item.diff.length > 0 && ` — ${item.diff.join(',')}`}
          </div>
        ))}
        {entries.length > 6 && (
          <div className="text-[10.5px] text-muted-foreground">… 另有 {entries.length - 6} 条</div>
        )}
      </div>
    </div>
  );
}

export function ConfigTransferPanel() {
  const t = useT();
  const fileRef = useRef<HTMLInputElement>(null);
  const [includeSecrets, setIncludeSecrets] = useState(false);
  const [exporting, setExporting] = useState(false);

  const [fileName, setFileName] = useState('');
  const [bundle, setBundle] = useState<unknown>(null);
  const [mode, setMode] = useState<Mode>('merge');
  const [planning, setPlanning] = useState(false);
  const [applying, setApplying] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [report, setReport] = useState<ConfigTransferReport | null>(null);
  const [error, setError] = useState('');

  const download = useCallback(async () => {
    setExporting(true);
    try {
      const r = await configTransferApi.download(includeSecrets);
      const url = URL.createObjectURL(r.data);
      const a = document.createElement('a');
      const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
      a.href = url;
      a.download = `unified-config-${stamp}.json`;
      a.click();
      URL.revokeObjectURL(url);
      notify.ok(t('configTransfer.downloaded'));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setExporting(false);
    }
  }, [includeSecrets, t]);

  const plan = useCallback(async (doc: unknown, nextMode: Mode) => {
    setPlanning(true);
    setError('');
    try {
      setReport(await configTransferApi.plan(doc, nextMode));
    } catch (e) {
      setReport(null);
      setError(errText(e));
    } finally {
      setPlanning(false);
    }
  }, []);

  const pick = useCallback(
    async (file: File) => {
      setFileName(file.name);
      setBundle(null);
      setReport(null);
      let doc: unknown;
      try {
        doc = JSON.parse(await file.text());
      } catch {
        setError(t('configTransfer.invalidFile'));
        return;
      }
      setBundle(doc);
      await plan(doc, mode);
    },
    [mode, plan, t],
  );

  const switchMode = useCallback(
    (nextMode: Mode) => {
      setMode(nextMode);
      if (bundle !== null) void plan(bundle, nextMode);
    },
    [bundle, plan],
  );

  const apply = useCallback(async () => {
    if (bundle === null) return;
    setApplying(true);
    try {
      const r = await configTransferApi.apply(bundle, mode);
      if (r.status === 200) {
        setReport(r.data);
        notify.ok(t('configTransfer.applied', {n: r.data.written}));
        setBundle(null);
        setFileName('');
        setConfirmOpen(false);
      } else {
        const body = r.data as unknown as {detail?: string; report?: ConfigTransferReport};
        if (body.report) setReport(body.report);
        setError(body.detail || t('configTransfer.invalidFile'));
        setConfirmOpen(false);
      }
    } catch (e) {
      setError(errText(e));
      setConfirmOpen(false);
    } finally {
      setApplying(false);
    }
  }, [bundle, mode, t]);

  const canApply = bundle !== null && report !== null && report.ok && !planning && !applying;

  return (
    <section className="rounded-[20px] bg-muted p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-start gap-2.5">
          <FileJson className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <div>
            <div className="text-sm font-medium">{t('configTransfer.title')}</div>
            <div className="mt-1 text-[11px] leading-5 text-muted-foreground">
              {t('configTransfer.desc')}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Button
            size="sm"
            className="rounded-full"
            onClick={() => void download()}
            disabled={exporting}
          >
            {exporting ? (
              <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
            ) : (
              <Download className="mr-1 h-3.5 w-3.5" />
            )}
            {exporting ? t('configTransfer.downloading') : t('configTransfer.download')}
          </Button>
        </div>
      </div>

      <label className="mt-3 flex items-start gap-2 text-[11px] leading-5 text-muted-foreground">
        <input
          type="checkbox"
          className="mt-1"
          checked={includeSecrets}
          onChange={(e) => setIncludeSecrets(e.target.checked)}
        />
        <span>
          {t('configTransfer.includeSecrets')}
          {includeSecrets && (
            <span className="ml-1 font-medium text-destructive-text">{t('configTransfer.includeSecretsWarn')}</span>
          )}
        </span>
      </label>

      <div className="mt-4 flex flex-wrap items-center gap-2">
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void pick(file);
            e.target.value = '';
          }}
        />
        <Button size="sm" variant="outline" className="rounded-full" onClick={() => fileRef.current?.click()}>
          <Upload className="mr-1 h-3.5 w-3.5" />
          {t('configTransfer.pickFile')}
        </Button>
        {fileName && <span className="break-all font-mono text-[11px]">{fileName}</span>}
        <span className="text-[11px] text-muted-foreground">{t('configTransfer.fileHint')}</span>
      </div>

      {!fileName && (
        <div className="mt-4 rounded-[16px] border border-dashed border-border/70 px-3 py-6 text-center">
          <div className="text-xs font-medium">{t('configTransfer.noFile')}</div>
          <div className="mt-1 text-[11px] leading-5 text-muted-foreground">
            {t('configTransfer.noFileDesc')}
          </div>
        </div>
      )}

      {planning && (
        <div className="mt-4 flex items-center gap-2 text-[11.5px] text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
          {t('configTransfer.planning')}
        </div>
      )}

      {error && (
        <div className="mt-4 flex items-start gap-2 rounded-[16px] border border-destructive/30 bg-destructive/5 px-3 py-2.5">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive-text" />
          <div className="min-w-0 flex-1 text-[11.5px] leading-5 whitespace-pre-wrap break-words">{error}</div>
        </div>
      )}

      {report && !planning && (
        <div className="mt-4 space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className="text-[10px]">
              {report.mode}
            </Badge>
            <div className="text-[11.5px] font-medium">
              {t('configTransfer.reportTitle')} ·{' '}
              {t('configTransfer.total', {n: report.create.length + report.overwrite.length + report.skip.length + report.fail.length})}
            </div>
            {report.ok ? (
              <CircleCheck className="h-3.5 w-3.5 text-emerald-500" />
            ) : (
              <AlertTriangle className="h-3.5 w-3.5 text-destructive-text" />
            )}
          </div>
          <Section title={t('configTransfer.create')} entries={report.create} tone="bg-emerald-500" />
          <Section title={t('configTransfer.overwrite')} entries={report.overwrite} tone="bg-amber-500" />
          <Section title={t('configTransfer.skip')} entries={report.skip} tone="bg-muted-foreground" />
          {report.issues
            .filter((issue) => issue.level === 'fail')
            .map((issue) => (
              <div
                key={`${issue.path}-${issue.column}`}
                className="rounded-[14px] border border-destructive/30 bg-destructive/5 px-3 py-1.5 text-[11px] leading-5"
              >
                <span className="font-medium">{t('configTransfer.fail')} · </span>
                <span className="font-mono">{issue.path}</span>
                {` ${t('configTransfer.failAt', {index: issue.index, column: issue.column, reason: issue.reason})}`}
              </div>
            ))}
          {report.fail.length > 0 && (
            <Section title={t('configTransfer.fail')} entries={report.fail} tone="bg-destructive" />
          )}

          <div className="flex flex-wrap items-center gap-2 pt-1">
            <span className="text-[11px] text-muted-foreground">{t('configTransfer.modeLabel')}</span>
            {(['merge', 'overwrite'] as const).map((m) => (
              <Button
                key={m}
                size="sm"
                variant={mode === m ? 'default' : 'outline'}
                className="rounded-full"
                disabled={planning || applying}
                onClick={() => switchMode(m)}
              >
                {m === 'merge' ? t('configTransfer.modeMerge') : t('configTransfer.modeOverwrite')}
              </Button>
            ))}
            <Button
              size="sm"
              className="rounded-full"
              disabled={!canApply}
              onClick={() => {
                if (mode === 'overwrite') setConfirmOpen(true);
                else void apply();
              }}
            >
              {applying ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
              {applying ? t('configTransfer.applying') : t('configTransfer.apply')}
            </Button>
          </div>
        </div>
      )}

      <AlertDialog
        open={confirmOpen}
        onOpenChange={(open) => {
          if (!open && !applying) setConfirmOpen(false);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('configTransfer.confirmTitle')}</AlertDialogTitle>
            <AlertDialogDescription>{t('configTransfer.confirmWarn')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={applying}>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              disabled={applying}
              onClick={(e) => {
                e.preventDefault();
                void apply();
              }}
            >
              {applying ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : null}
              {applying ? t('configTransfer.applying') : t('configTransfer.confirmApply')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  );
}
