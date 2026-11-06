'use client';

/**
 * 导入账号（贴凭证式）：按服务商登记一份凭据，保存前可先测连通性。
 * 探测结果只提示不拦截——很多上游要额外配置，一次探测失败不该拒绝入库。
 */
import {useEffect, useMemo, useState} from 'react';
import {Loader2, TriangleAlert} from 'lucide-react';
import {Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle} from '@/components/animate-ui/radix/dialog';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Select, SelectContent, SelectItem, SelectTrigger, SelectValue} from '@/components/ui/select';
import {Textarea} from '@/components/ui/textarea';
import {errText} from '@/lib/api';
import {useT} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import {
  accountOpsApi,
  type AccountImportInput,
  type AccountImportProbeResult,
  type ImportCredentialType,
} from './account-ops';

const CRED_TYPES: ImportCredentialType[] = ['api_key', 'bearer', 'cookie', 'json', 'env'];
// 凭证类型标签走字面键，check_i18n 才能静态扫到
const CRED_TYPE_KEYS: Record<ImportCredentialType, string> = {
  api_key: 'accounts.credType_apiKey',
  bearer: 'accounts.credType_bearer',
  cookie: 'accounts.credType_cookie',
  json: 'accounts.credType_json',
  env: 'accounts.credType_env',
};

const PROBE_BAR: Record<AccountImportProbeResult['status'], string> = {
  ok: 'border-emerald-500/40 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  fail: 'border-red-500/40 bg-red-500/10 text-red-600 dark:text-red-400',
  unsupported: 'border-amber-500/40 bg-amber-500/10 text-amber-600 dark:text-amber-400',
};

type ProbeState = {phase: 'idle'} | {phase: 'running'} | {phase: 'done'; result: AccountImportProbeResult};

export function ImportAccountDialog({
  open,
  onOpenChange,
  providers,
  defaultProvider,
  onSaved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 可导入的服务商候选（由页面按 managed 过滤后传入；readiness 用于顶部警示） */
  providers: {id: string; name: string; readiness: 'ready' | 'needs_config'; requires: string[]}[];
  defaultProvider?: string;
  onSaved?: () => void;
}) {
  const t = useT();
  const [provider, setProvider] = useState('');
  const [label, setLabel] = useState('');
  const [credType, setCredType] = useState<ImportCredentialType>('api_key');
  const [credential, setCredential] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [note, setNote] = useState('');
  const [probe, setProbe] = useState<ProbeState>({phase: 'idle'});
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (!open) return;
    setProvider(defaultProvider ?? providers[0]?.id ?? '');
    setLabel('');
    setCredType('api_key');
    setCredential('');
    setBaseUrl('');
    setNote('');
    setProbe({phase: 'idle'});
    setSaving(false);
  }, [open, defaultProvider, providers]);

  const providerName = useMemo(() => providers.find((p) => p.id === provider)?.name ?? provider, [providers, provider]);
  const providerMeta = useMemo(() => providers.find((p) => p.id === provider), [providers, provider]);
  const requiresText = providerMeta?.requires.join(t('common.listSeparator')) ?? '';
  const buildBody = (): AccountImportInput => ({
    provider,
    label: label.trim(),
    credential_type: credType,
    credential,
    base_url: baseUrl.trim(),
    note: note.trim(),
  });
  const canSubmit = provider && credential.trim().length > 0;

  const onProbe = async () => {
    if (!canSubmit || probe.phase === 'running') return;
    setProbe({phase: 'running'});
    try {
      const result = await accountOpsApi.probeImport(buildBody());
      setProbe({phase: 'done', result});
    } catch (error) {
      setProbe({
        phase: 'done',
        result: {status: 'fail', connected: false, message: errText(error), upstream_status: null},
      });
    }
  };

  const onSave = async () => {
    if (!canSubmit || saving) return;
    setSaving(true);
    try {
      const created = await accountOpsApi.importAccount(buildBody());
      notify.ok(t('accounts.importSaved', {name: created.nickname || providerName}));
      onSaved?.();
      onOpenChange(false);
    } catch (error) {
      notify.err(errText(error));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{t('accounts.importTitle')}</DialogTitle>
          <DialogDescription>{t('accounts.importDesc')}</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          {/* 未就绪不封死入口（前置可能只是运行时未起），但必须说清保存后也不会生效 */}
          {providerMeta?.readiness === 'needs_config' && (
            <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-700 dark:text-amber-400">
              <TriangleAlert className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{t('accounts.importNotReadyWarning', {provider: providerName, requires: requiresText})}</span>
            </div>
          )}
          <div className="space-y-2">
            <Label>{t('accounts.importProvider')}</Label>
            <Select value={provider} onValueChange={setProvider}>
              <SelectTrigger className="w-full">
                <SelectValue placeholder={t('accounts.importProvider')} />
              </SelectTrigger>
              <SelectContent>
                {providers.map((p) => (
                  <SelectItem key={p.id} value={p.id}>
                    {p.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label>{t('accounts.importName')}</Label>
            <Input value={label} onChange={(e) => setLabel(e.target.value)} placeholder={t('accounts.importNamePlaceholder')} />
          </div>
          <div className="space-y-2">
            <Label>{t('accounts.importCredType')}</Label>
            <Select value={credType} onValueChange={(value) => setCredType(value as ImportCredentialType)}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {CRED_TYPES.map((kind) => (
                  <SelectItem key={kind} value={kind}>
                    {t(CRED_TYPE_KEYS[kind])}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label>{t('accounts.importCredential')}</Label>
            <Textarea
              value={credential}
              onChange={(e) => setCredential(e.target.value)}
              rows={credType === 'json' ? 6 : 3}
              placeholder={t('accounts.importCredentialPlaceholder')}
            />
            {credType === 'json' && <p className="text-xs text-muted-foreground">{t('accounts.importJsonHint')}</p>}
          </div>
          <div className="space-y-2">
            <Label>{t('accounts.importBaseUrl')}</Label>
            <Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={t('accounts.importBaseUrlPlaceholder')} />
          </div>
          <div className="space-y-2">
            <Label>{t('accounts.importNote')}</Label>
            <Input value={note} onChange={(e) => setNote(e.target.value)} placeholder={t('accounts.importNotePlaceholder')} />
          </div>
          {probe.phase === 'done' && (
            <div className={`rounded-md border px-3 py-2 text-xs ${PROBE_BAR[probe.result.status]}`}>
              <span className="font-medium">
                {probe.result.status === 'ok'
                  ? t('accounts.importProbeOk')
                  : probe.result.status === 'fail'
                    ? t('accounts.importProbeFail')
                    : t('accounts.importProbeUnsupported')}
              </span>
              {probe.result.message && <span className="ml-2">{probe.result.message}</span>}
            </div>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="outline" onClick={onProbe} disabled={!canSubmit || probe.phase === 'running'}>
              {probe.phase === 'running' && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
              {probe.phase === 'running' ? t('accounts.importTesting') : t('accounts.importTest')}
            </Button>
            <Button onClick={onSave} disabled={!canSubmit || saving}>
              {saving && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />}
              {t('accounts.importSave')}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
