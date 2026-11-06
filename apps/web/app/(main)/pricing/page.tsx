'use client';

import {useCallback, useEffect, useState} from 'react';
import {
  ChevronRight,
  FileUp,
  FlaskConical,
  Plus,
  Search,
  Tags,
} from 'lucide-react';

import {pricingApi, providerApi, errText} from '@/lib/api';
import type {PriceBookRow, PriceResolveResult} from '@/lib/types';
import type {ProviderInfo} from '@/lib/providers';
import {fmtDateTime, usdToMicro} from '@/lib/format';
import {notify} from '@/lib/toast';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {PageHeader} from '@/components/common/layout/PageHeader';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';

/** 与后端 billing.TOKEN_CLASSES 保持一致 */
const TOKEN_CLASSES = ['input', 'output', 'cache_read', 'cache_write', 'reasoning'] as const;

interface ImportPreview {
  added: number;
  skipped: number;
  errors: string[];
}

export default function PricingPage() {
  const t = useT();
  const {isAdmin} = useAuth();

  const [providers, setProviders] = useState<ProviderInfo[]>([]);

  /* ── 价格表 ─────────────────────────── */
  const [rows, setRows] = useState<PriceBookRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [fProvider, setFProvider] = useState('');
  const [fModel, setFModel] = useState('');
  const [fClass, setFClass] = useState('all');

  /* ── 干跑解析 ───────────────────────── */
  const [rProvider, setRProvider] = useState('');
  const [rModel, setRModel] = useState('');
  const [rAt, setRAt] = useState('');
  const [resolving, setResolving] = useState(false);
  const [resolved, setResolved] = useState<PriceResolveResult | null>(null);

  /* ── 新增价格行（admin） ─────────────── */
  const [formOpen, setFormOpen] = useState(false);
  const [form, setForm] = useState({provider: '', model: '', token_class: 'input', price: '', at: '', note: ''});
  const [saving, setSaving] = useState(false);

  /* ── YAML 导入（admin） ─────────────── */
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [importing, setImporting] = useState(false);
  const previewErrors = preview?.errors ?? [];

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const params: {provider?: string; model?: string; token_class?: string; limit: number} = {limit: 500};
      if (fProvider.trim()) params.provider = fProvider.trim();
      if (fModel.trim()) params.model = fModel.trim();
      if (fClass !== 'all') params.token_class = fClass;
      setRows(await pricingApi.books(params));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setLoading(false);
    }
  }, [fProvider, fModel, fClass]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    providerApi.list().then(setProviders).catch(() => {});
  }, []);

  const doResolve = async () => {
    if (!rProvider || !rModel.trim()) return;
    setResolving(true);
    try {
      const at = rAt ? Math.floor(new Date(rAt).getTime() / 1000) : undefined;
      setResolved(await pricingApi.resolve({provider: rProvider, model: rModel.trim(), at}));
    } catch (e) {
      notify.err(errText(e));
      setResolved(null);
    } finally {
      setResolving(false);
    }
  };

  const submitRow = async () => {
    const usd = parseFloat(form.price);
    if (!form.model.trim() || !Number.isFinite(usd) || usd < 0) return;
    setSaving(true);
    try {
      await pricingApi.create({
        model: form.model.trim(),
        provider: form.provider.trim() || '*',
        token_class: form.token_class,
        // 界面用美元报价，落库是微美元（1e-6 USD），避免用户数 0
        price_micro_per_1m: usdToMicro(usd),
        effective_from: form.at ? Math.floor(new Date(form.at).getTime() / 1000) : undefined,
        note: form.note.trim(),
      });
      notify.ok(t('pricing.rowAdded'));
      setFormOpen(false);
      setForm({provider: '', model: '', token_class: 'input', price: '', at: '', note: ''});
      load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setSaving(false);
    }
  };

  const dryRunImport = async () => {
    setImporting(true);
    try {
      setPreview(await pricingApi.importBooks(true));
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setImporting(false);
    }
  };

  const runImport = async () => {
    setImporting(true);
    try {
      const r = await pricingApi.importBooks(false);
      notify.ok(t('pricing.importDone', {added: r.added, skipped: r.skipped}));
      setPreview(null);
      load();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setImporting(false);
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4 md:gap-6">
      <PageHeader
        title={t('pricing.title')}
        description={t('pricing.description')}
        actions={
          isAdmin ? (
            <>
              <Button variant="outline" size="sm" className="rounded-full" disabled={importing} onClick={dryRunImport}>
                <FileUp />
                {t('pricing.importYaml')}
              </Button>
              <Button size="sm" className="rounded-full" onClick={() => setFormOpen(true)}>
                <Plus />
                {t('pricing.addRow')}
              </Button>
            </>
          ) : undefined
        }
      />

      {/* 干跑解析：看某模型此刻会命中哪些价格行 */}
      <section className="rounded-[20px] border p-4">
        <div className="mb-3 flex items-center gap-2 text-sm font-medium">
          <FlaskConical className="h-4 w-4 text-muted-foreground" />
          {t('pricing.resolveTitle')}
        </div>
        <div className="grid grid-cols-2 gap-3 md:grid-cols-[1fr_1fr_1fr_auto]">
          <div className="space-y-1.5">
            <Label className="text-xs">{t('pricing.colProvider')}</Label>
            <Select value={rProvider} onValueChange={setRProvider}>
              <SelectTrigger aria-label={t('pricing.colProvider')} className="h-9">
                <SelectValue placeholder={t('pricing.providerPlaceholder')} />
              </SelectTrigger>
              <SelectContent>
                {providers.map((p) => (
                  <SelectItem key={p.id} value={p.id}>{p.label || p.id}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">{t('pricing.colModel')}</Label>
            <Input
              className="h-9"
              value={rModel}
              onChange={(e) => setRModel(e.target.value)}
              placeholder={t('pricing.modelPlaceholder')}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">{t('pricing.resolveAt')}</Label>
            <Input
              className="h-9"
              type="datetime-local"
              aria-label={t('pricing.resolveAt')}
              value={rAt}
              onChange={(e) => setRAt(e.target.value)}
            />
          </div>
          <div className="flex items-end">
            <Button
              className="col-span-2 h-9 w-full rounded-full md:col-span-1 md:w-auto"
              disabled={resolving || !rProvider || !rModel.trim()}
              onClick={doResolve}
            >
              <ChevronRight />
              {t('pricing.resolveRun')}
            </Button>
          </div>
        </div>

        {resolved && (
          <div className="mt-4 space-y-2">
            <div className="text-[11px] text-muted-foreground">
              {t('pricing.resolveResultAt', {at: fmtDateTime(resolved.at)})}
            </div>
            <div className="overflow-x-auto rounded-2xl border">
              <Table>
                <TableHeader>
                  <TableRow className="border-b border-border/60 hover:bg-transparent">
                    <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('pricing.colClass')}</TableHead>
                    <TableHead className="text-[11px] text-muted-foreground">{t('pricing.resolveHitRow')}</TableHead>
                    <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">{t('pricing.colPrice')}</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {TOKEN_CLASSES.map((tc) => {
                    const line = resolved.lines[tc];
                    return (
                      <TableRow key={tc} className="border-b border-border/40">
                        <TableCell className="pl-4">
                          <Badge variant="secondary" className="rounded-full font-mono">{tc}</Badge>
                        </TableCell>
                        <TableCell className="font-mono text-xs">
                          {line ? `${line.provider} / ${line.model} (#${line.price_id})` : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </TableCell>
                        <TableCell className="pr-4 text-right tabular-nums">
                          {line ? `$${line.price_usd_per_1m}` : (
                            <Badge variant="secondary" className="rounded-full text-warning-text">
                              {t('pricing.resolveMissing')}
                            </Badge>
                          )}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
            {resolved.missing.length > 0 && (
              <p className="text-[11px] text-warning-text">
                {t('pricing.resolveMissingHint', {list: resolved.missing.join(', ')})}
              </p>
            )}
          </div>
        )}
      </section>

      {/* 价格表（只读列表；改价 = 新增一行） */}
      <section className="flex min-h-0 flex-1 flex-col gap-3 rounded-[20px] border p-4">
        <div className="grid grid-cols-2 gap-3 md:grid-cols-[1fr_1fr_1fr_auto]">
          <Input
            className="h-9"
            value={fProvider}
            onChange={(e) => setFProvider(e.target.value)}
            placeholder={t('pricing.filterProvider')}
          />
          <Input
            className="h-9"
            value={fModel}
            onChange={(e) => setFModel(e.target.value)}
            placeholder={t('pricing.filterModel')}
          />
          <Select value={fClass} onValueChange={setFClass}>
            <SelectTrigger aria-label={t('pricing.colClass')} className="h-9">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">{t('pricing.classAll')}</SelectItem>
              {TOKEN_CLASSES.map((tc) => (
                <SelectItem key={tc} value={tc}>{tc}</SelectItem>
              ))}
            </SelectContent>
          </Select>
          <Button variant="outline" className="col-span-2 h-9 rounded-full md:col-span-1" onClick={load}>
            <Search />
            {t('pricing.filter')}
          </Button>
        </div>

        {rows.length === 0 && !loading ? (
          <EmptyState
            icon={Tags}
            title={t('pricing.empty')}
            description={t('pricing.emptyDesc')}
          />
        ) : (
          <div className="scroll-slim min-h-0 flex-1 overflow-auto rounded-2xl border">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-border/60 hover:bg-transparent">
                  <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('pricing.colProvider')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('pricing.colModel')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('pricing.colClass')}</TableHead>
                  <TableHead className="text-right text-[11px] text-muted-foreground">{t('pricing.colPrice')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('pricing.colEffectiveFrom')}</TableHead>
                  <TableHead className="text-[11px] text-muted-foreground">{t('pricing.colCreatedBy')}</TableHead>
                  <TableHead className="pr-4 text-[11px] text-muted-foreground">{t('pricing.colNote')}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {rows.map((r) => (
                  <TableRow key={r.id} className="border-b border-border/40">
                    <TableCell className="pl-4">
                      <Badge variant="secondary" className="rounded-full">{r.provider}</Badge>
                    </TableCell>
                    <TableCell className="font-mono text-xs">{r.model}</TableCell>
                    <TableCell className="font-mono text-xs">{r.token_class}</TableCell>
                    <TableCell className="text-right tabular-nums">${r.price_usd_per_1m}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">{fmtDateTime(r.effective_from)}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">{r.created_by || '—'}</TableCell>
                    <TableCell className="max-w-[200px] truncate pr-4 text-xs text-muted-foreground">
                      {r.note || '—'}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        <p className="text-[11px] leading-5 text-muted-foreground">{t('pricing.appendOnlyHint')}</p>
      </section>

      {/* 新增价格行 */}
      <Dialog open={formOpen} onOpenChange={setFormOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t('pricing.addRow')}</DialogTitle>
            <DialogDescription>{t('pricing.addRowDesc')}</DialogDescription>
          </DialogHeader>
          <DialogBody>
            <div className="space-y-4 px-5 pb-4">
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label>{t('pricing.colProvider')}</Label>
                  <Input
                    value={form.provider}
                    onChange={(e) => setForm({...form, provider: e.target.value})}
                    placeholder="*"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label>{t('pricing.colModel')}</Label>
                  <Input
                    value={form.model}
                    onChange={(e) => setForm({...form, model: e.target.value})}
                    placeholder={t('pricing.modelPlaceholder')}
                  />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div className="space-y-1.5">
                  <Label>{t('pricing.colClass')}</Label>
                  <Select value={form.token_class} onValueChange={(v) => setForm({...form, token_class: v})}>
                    <SelectTrigger aria-label={t('pricing.colClass')}>
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {TOKEN_CLASSES.map((tc) => (
                        <SelectItem key={tc} value={tc}>{tc}</SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-1.5">
                  <Label>{t('pricing.fieldPrice')}</Label>
                  <Input
                    type="number"
                    min="0"
                    step="0.01"
                    value={form.price}
                    onChange={(e) => setForm({...form, price: e.target.value})}
                    placeholder="0.00"
                  />
                </div>
              </div>
              <div className="space-y-1.5">
                <Label>{t('pricing.colEffectiveFrom')}</Label>
                <Input
                  type="datetime-local"
                  aria-label={t('pricing.colEffectiveFrom')}
                  value={form.at}
                  onChange={(e) => setForm({...form, at: e.target.value})}
                />
                <p className="text-[11px] text-muted-foreground">{t('pricing.effectiveFromHint')}</p>
              </div>
              <div className="space-y-1.5">
                <Label>{t('pricing.colNote')}</Label>
                <Input
                  value={form.note}
                  onChange={(e) => setForm({...form, note: e.target.value})}
                  placeholder={t('pricing.notePlaceholder')}
                />
              </div>
            </div>
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setFormOpen(false)}>{t('pricing.cancel')}</Button>
            <Button
              onClick={submitRow}
              disabled={saving || !form.model.trim() || !form.price}
            >
              {t('pricing.save')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* YAML 导入：先干跑预览，确认后才真正落库 */}
      <Dialog open={!!preview} onOpenChange={(v) => !v && setPreview(null)}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{t('pricing.importPreviewTitle')}</DialogTitle>
            <DialogDescription>
              {preview
                ? t('pricing.importPreviewDesc', {
                    added: preview.added,
                    skipped: preview.skipped,
                    errors: previewErrors.length,
                  })
                : ''}
            </DialogDescription>
          </DialogHeader>
          <DialogBody>
            {previewErrors.length > 0 && (
              <div className="scroll-slim max-h-40 space-y-1 overflow-y-auto px-5 pb-4">
                {previewErrors.map((e, i) => (
                  <p key={i} className="font-mono text-[11px] text-destructive-text">{e}</p>
                ))}
              </div>
            )}
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setPreview(null)}>{t('pricing.cancel')}</Button>
            <Button onClick={runImport} disabled={importing || !preview || preview.added === 0}>
              {t('pricing.importConfirm')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
