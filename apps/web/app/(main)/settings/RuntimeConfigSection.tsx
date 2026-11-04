'use client';

import {useCallback, useEffect, useRef, useState} from 'react';
import {Download, RotateCcw, Save, TriangleAlert, Upload} from 'lucide-react';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Switch} from '@/components/ui/switch';
import {http, errText} from '@/lib/api';
import {notify} from '@/lib/toast';
import {useI18n} from '@/lib/i18n/provider';

type RuntimeValue = number | boolean;

type RuntimeItem = {
  key: string;
  type: 'int' | 'bool';
  value: RuntimeValue;
  default: RuntimeValue;
  source: 'db' | 'default' | 'invalid';
  min: number | null;
  max: number | null;
  ui: boolean;
};

type RuntimeState = {
  ok: boolean;
  items: RuntimeItem[];
  excluded_secrets: string[];
  saved?: string[];
  imported?: string[];
};

type FieldError = {
  key: string;
  value?: unknown;
  reason: string;
  message: string;
};

const ENDPOINT = '/api/settings/runtime-config';

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function readErrors(error: unknown): {message: string; errors: FieldError[]} {
  if (error && typeof error === 'object' && 'response' in error) {
    const data = (error as {response?: {data?: unknown}}).response?.data;
    if (isRecord(data) && isRecord(data.error)) {
      const err = data.error;
      const errors = Array.isArray(err.errors) ? (err.errors as FieldError[]) : [];
      return {message: typeof err.message === 'string' ? err.message : '', errors};
    }
  }
  return {message: errText(error), errors: []};
}

export function RuntimeConfigSection({isAdmin}: {isAdmin: boolean}) {
  const {t} = useI18n();
  const [items, setItems] = useState<RuntimeItem[] | null>(null);
  const [draft, setDraft] = useState<Record<string, string | boolean>>({});
  const [baseline, setBaseline] = useState<Record<string, string | boolean>>({});
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<FieldError[]>([]);
  const fileRef = useRef<HTMLInputElement>(null);

  const adopt = useCallback((state: RuntimeState) => {
    const visible = state.items.filter((item) => item.ui);
    setItems(visible);
    const next: Record<string, string | boolean> = {};
    for (const item of visible) next[item.key] = item.type === 'int' ? String(item.value) : Boolean(item.value);
    setDraft(next);
    setBaseline(next);
    setErrors([]);
  }, []);

  const load = useCallback(async () => {
    try {
      const res = await http.get<RuntimeState>(ENDPOINT);
      adopt(res.data);
    } catch (e) {
      notify.err(t('settings.runtime.readFailed'), errText(e));
    }
  }, [adopt, t]);

  useEffect(() => {
    // 非 admin 完全不发请求，接口本身也会 403
    if (!isAdmin) return;
    void load();
  }, [isAdmin, load]);

  if (!isAdmin) return null;

  const dirty = items ? items.filter((item) => draft[item.key] !== baseline[item.key]) : [];
  const fmt = (value: RuntimeValue) => (typeof value === 'boolean' ? t(value ? 'settings.runtime.on' : 'settings.runtime.off') : String(value));

  /**
   * 状态徽章按「值 vs 默认值」表述，而不是按 source。
   *
   * source 的语义是「库里有没有存」（后端定义，正确），但存了不等于改过：
   * 把值改回默认再保存，source 仍是 db。只按 source 写「已自定义」会让
   * 用户以为配置被改了，而实际值就是默认值。所以 db + 等值默认 →「已保存
   * （与默认相同）」，db + 不同 →「已自定义」。
   */
  const sourceBadgeKey = (item: RuntimeItem) => {
    if (item.source === 'invalid') return 'settings.runtime.fromInvalid';
    if (item.source === 'default') return 'settings.runtime.fromDefault';
    return item.value === item.default
      ? 'settings.runtime.fromDbDefault'
      : 'settings.runtime.fromDb';
  };

  async function save() {
    if (!items || !dirty.length) {
      notify.info(t('settings.noChanges'));
      return;
    }
    const patch: Record<string, string | boolean> = {};
    for (const item of dirty) patch[item.key] = draft[item.key];
    setBusy(true);
    try {
      const res = await http.post<RuntimeState>(ENDPOINT, {items: patch});
      adopt(res.data);
      notify.ok(t('settings.runtime.saved'), t('settings.runtime.savedHint'));
    } catch (e) {
      const parsed = readErrors(e);
      setErrors(parsed.errors);
      notify.err(t('settings.runtime.saveFailed'), parsed.message);
    } finally {
      setBusy(false);
    }
  }

  async function exportConfig() {
    try {
      const res = await http.get<Record<string, unknown>>(`${ENDPOINT}/export`);
      const blob = new Blob([JSON.stringify(res.data, null, 2)], {type: 'application/json'});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `unified-runtime-config-${new Date().toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
      notify.ok(t('settings.runtime.exported'), t('settings.runtime.exportDesc'));
    } catch (e) {
      notify.err(t('settings.runtime.exportFailed'), errText(e));
    }
  }

  async function importFile(file: File) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(await file.text());
    } catch {
      notify.err(t('settings.runtime.badFile'));
      return;
    }
    if (!isRecord(parsed)) {
      notify.err(t('settings.runtime.badFile'));
      return;
    }
    setBusy(true);
    try {
      const res = await http.post<RuntimeState>(`${ENDPOINT}/import`, parsed);
      adopt(res.data);
      notify.ok(t('settings.runtime.imported', {count: (res.data.imported ?? []).length}));
    } catch (e) {
      const parsedError = readErrors(e);
      setErrors(parsedError.errors);
      notify.err(t('settings.runtime.importFailed'), parsedError.message);
    } finally {
      setBusy(false);
      if (fileRef.current) fileRef.current.value = '';
    }
  }

  return (
    <div className="space-y-3">
      <div className="rounded-[20px] bg-muted px-3.5 py-3">
        <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div>
            <div className="text-sm font-medium">{t('settings.runtime.title')}</div>
            <div className="mt-0.5 text-xs text-muted-foreground">{t('settings.runtime.desc')}</div>
          </div>
          <div className="flex items-center gap-2">
            {dirty.length > 0 && (
              <Button size="sm" variant="ghost" className="rounded-full" disabled={busy} onClick={() => setDraft(baseline)}>
                <RotateCcw className="h-3.5 w-3.5" />
                {t('settings.revert')}
              </Button>
            )}
            <Button size="sm" className="rounded-full" disabled={busy || !dirty.length} onClick={save}>
              <Save className="h-3.5 w-3.5" />
              {t('common.save')}
            </Button>
          </div>
        </div>

        {items === null ? (
          <div className="text-xs text-muted-foreground">{t('common.loading')}</div>
        ) : (
          <div className="grid grid-cols-1 gap-1.5 xl:grid-cols-2">
            {items.map((item) => (
              <div key={item.key} className="rounded-2xl bg-background/60 px-3 py-2">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="text-xs font-medium">{t(`settings.runtime.${item.key}.label`)}</span>
                      <span className="rounded-full border px-1.5 py-0.5 text-[10px] text-muted-foreground">
                        {t(sourceBadgeKey(item))}
                      </span>
                    </div>
                    <div className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
                      {t(`settings.runtime.${item.key}.desc`)}
                    </div>
                    <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">
                      {t('settings.runtime.defaultValue', {value: fmt(item.default)})}
                    </div>
                  </div>
                  {item.type === 'bool' ? (
                    <Switch
                      checked={Boolean(draft[item.key])}
                      onCheckedChange={(checked) => setDraft((prev) => ({...prev, [item.key]: checked}))}
                    />
                  ) : (
                    <Input
                      type="number"
                      className="h-8 w-24 rounded-xl text-right text-xs"
                      min={item.min ?? undefined}
                      max={item.max ?? undefined}
                      value={String(draft[item.key] ?? '')}
                      onChange={(e) => setDraft((prev) => ({...prev, [item.key]: e.target.value}))}
                    />
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      {errors.length > 0 && (
        <div className="rounded-[20px] border border-destructive/30 px-3.5 py-3">
          <div className="mb-1 flex items-center gap-1.5 text-xs font-medium text-destructive">
            <TriangleAlert className="h-3.5 w-3.5" />
            {t('settings.runtime.importErrors')}
          </div>
          <ul className="space-y-0.5 text-[11px] text-muted-foreground">
            {errors.map((err) => (
              <li key={`${err.key}-${err.reason}`} className="font-mono">
                {err.key || err.reason}: {err.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="rounded-[20px] bg-muted px-3.5 py-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="min-w-0">
            <div className="text-sm font-medium">{t('settings.runtime.transferTitle')}</div>
            <div className="mt-0.5 text-xs text-muted-foreground">{t('settings.runtime.transferDesc')}</div>
          </div>
          <div className="flex items-center gap-2">
            <Button size="sm" variant="outline" className="rounded-full" disabled={busy} onClick={exportConfig}>
              <Download className="h-3.5 w-3.5" />
              {t('settings.runtime.export')}
            </Button>
            <Button size="sm" variant="outline" className="rounded-full" disabled={busy} onClick={() => fileRef.current?.click()}>
              <Upload className="h-3.5 w-3.5" />
              {t('settings.runtime.import')}
            </Button>
          </div>
        </div>
        <div className="mt-1.5 text-[11px] text-muted-foreground">{t('settings.runtime.importHint')}</div>
        <input
          ref={fileRef}
          type="file"
          accept="application/json,.json"
          className="hidden"
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) void importFile(file);
          }}
        />
      </div>
    </div>
  );
}
