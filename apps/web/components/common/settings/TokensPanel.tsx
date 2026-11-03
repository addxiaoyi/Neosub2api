'use client';

import {useCallback, useEffect, useState} from 'react';
import {KeyRound, Plus, Trash2} from 'lucide-react';
import {errText, tokenApi} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useI18n} from '@/lib/i18n/provider';
import {notify} from '@/lib/toast';
import type {ApiToken, CreatedApiToken} from '@/lib/types';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {CopyButton} from '@/components/ui/copy-button';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

/**
 * 管理面「访问令牌」面板（见 docs/api-tokens.md）。
 *
 * 与「API 密钥」页的区别：密钥是发给**下游调模型**的网关凭据；令牌是给
 * **脚本 / CI 免登录调管理接口**用的。两者都只在创建时显示一次明文。
 *
 * 注意这里刻意**不做**成「令牌能管令牌」——创建 / 停用 / 删除令牌都必须用
 * 会话登录（服务端 `require_session_admin`），所以本页只有登录用户看得到。
 */
export function TokensPanel() {
  const {t} = useI18n();
  const {isAdmin} = useAuth();
  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const [busy, setBusy] = useState(false);
  const [created, setCreated] = useState<CreatedApiToken | null>(null);
  const [form, setForm] = useState<{name: string}>({name: ''});

  const load = useCallback(async () => {
    try {
      setTokens(await tokenApi.list());
    } catch (e) {
      notify.err(errText(e));
    }
  }, []);

  useEffect(() => {
    // 非管理员不拉列表：这几个接口都是 require_session_admin，拉了只会吃 403，
    // 弹出的报错对用户没有任何可行动信息（与「设置」页其它面板的处置一致：
    // 只读账号看到的是「为什么看不到」而不是一次失败请求）。
    if (isAdmin) void load();
  }, [load, isAdmin]);

  if (!isAdmin) {
    return (
      <div className="rounded-[20px] bg-muted p-8 text-center text-sm">
        <div className="font-medium">{t('settings.tokensAdminRequired')}</div>
        <div className="mx-auto mt-2 max-w-[38rem] text-xs text-muted-foreground">
          {t('settings.tokensAdminRequiredDesc')}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {/* ── 新建 ── */}
      <div className="rounded-[20px] bg-muted p-4">
        <div className="mb-1 text-sm font-medium">{t('settings.tokensNew')}</div>
        <div className="mb-3 text-[11px] text-muted-foreground">{t('settings.tokensNewDesc')}</div>
        <div className="grid grid-cols-1 items-end gap-3 sm:grid-cols-[minmax(0,1fr)_auto]">
          <div className="space-y-1.5">
            <Label className="text-[11px] text-muted-foreground">{t('settings.tokensName')}</Label>
            <Input
              value={form.name}
              onChange={(e) => setForm({name: e.target.value})}
              className="bg-background"
            />
          </div>
          <Button
            className="rounded-full"
            disabled={busy}
            onClick={async () => {
              if (!form.name.trim()) {
                notify.err(t('settings.tokensNameRequired'));
                return;
              }
              setBusy(true);
              try {
                // 后端只接受只读作用域与不设到期，表单不再提供无效选项
                const r = await tokenApi.create({name: form.name.trim()});
                setCreated(r);
                notify.ok(t('settings.tokensCreated'));
                setForm({name: ''});
                void load();
              } catch (e) {
                notify.err(errText(e));
              } finally {
                setBusy(false);
              }
            }}
          >
            <Plus />
            {t('keys.create')}
          </Button>
        </div>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          <span>{t('settings.tokensScope')}: {t('settings.tokensScopeReadonly')}</span>
          <span>{t('settings.tokensExpires')}: {t('settings.tokensExpiresNever')}</span>
        </div>
      </div>

      {/* ── 明文（仅此一次）── */}
      {created && (
        <div className="rounded-[20px] border border-amber-500/40 bg-amber-500/5 p-4">
          <div className="mb-1 text-sm font-medium">{t('settings.tokensCreated')}</div>
          <div className="mb-2 text-[11px] text-muted-foreground">
            {t('settings.tokensCreatedDesc')}
          </div>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded-lg bg-background px-3 py-2 font-mono text-xs">
              {created.token}
            </code>
            <CopyButton value={created.token} label={t('settings.tokensCopy')} />
            <Button
              variant="ghost"
              size="sm"
              className="rounded-full text-xs"
              onClick={() => setCreated(null)}
            >
              {t('common.close')}
            </Button>
          </div>
        </div>
      )}

      <div className="rounded-[20px] bg-muted p-4 text-[11px] text-muted-foreground">
        {t('settings.tokensSessionOnlyHint')}
      </div>

      {/* ── 列表 ── */}
      <div className="overflow-hidden rounded-[20px] bg-muted">
        <Table>
          <TableHeader>
            <TableRow className="border-b border-border/60 hover:bg-transparent">
              <TableHead className="pl-4 text-[11px] text-muted-foreground">
                {t('settings.tokensColName')}
              </TableHead>
              <TableHead className="text-[11px] text-muted-foreground">
                {t('settings.tokensColPrefix')}
              </TableHead>
              <TableHead className="text-[11px] text-muted-foreground">
                {t('settings.tokensColScope')}
              </TableHead>
              <TableHead className="text-[11px] text-muted-foreground">
                {t('settings.tokensColExpires')}
              </TableHead>
              <TableHead className="text-[11px] text-muted-foreground">
                {t('settings.tokensColLastUsed')}
              </TableHead>
              <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">
                {t('accounts.colActions')}
              </TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {tokens.map((tk) => (
              <TableRow key={tk.id} className="border-b border-border/40">
                <TableCell className="pl-4 text-sm font-medium">{tk.name}</TableCell>
                <TableCell className="font-mono text-xs text-muted-foreground">
                  {tk.prefix}…
                </TableCell>
                <TableCell>
                  <Badge
                    variant={tk.scope === 'admin' ? 'default' : 'secondary'}
                    className="rounded-full"
                  >
                    {tk.scope === 'admin'
                      ? t('settings.tokensScopeAdmin')
                      : t('settings.tokensScopeReadonly')}
                  </Badge>
                  {!tk.enabled && (
                    <Badge variant="secondary" className="ml-1 rounded-full">
                      {t('settings.tokensDisable')}
                    </Badge>
                  )}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {tk.expires_at
                    ? new Date(tk.expires_at * 1000).toLocaleString()
                    : t('settings.tokensExpiresNever')}
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {tk.last_used_at
                    ? `${new Date(tk.last_used_at * 1000).toLocaleString()}${
                        tk.last_used_ip ? ` · ${tk.last_used_ip}` : ''
                      }`
                    : t('settings.tokensNeverUsed')}
                </TableCell>
                <TableCell className="pr-4 text-right">
                  <div className="flex justify-end gap-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-7 rounded-full text-xs"
                      onClick={async () => {
                        try {
                          await tokenApi.update(tk.id, {enabled: !tk.enabled});
                          void load();
                        } catch (e) {
                          notify.err(errText(e));
                        }
                      }}
                    >
                      {tk.enabled ? t('settings.tokensDisable') : t('settings.tokensEnable')}
                    </Button>
                    <ConfirmDialog
                      title={t('settings.tokensDeleteTitle', {name: tk.name})}
                      description={t('settings.tokensDeleteDesc')}
                      confirmText={t('keys.delete')}
                      destructive
                      onConfirm={async () => {
                        try {
                          await tokenApi.remove(tk.id);
                          notify.ok(t('keys.deleted'));
                          void load();
                        } catch (e) {
                          notify.err(errText(e));
                        }
                      }}
                      trigger={
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 rounded-md text-red-500 hover:text-red-600"
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      }
                    />
                  </div>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
        {!tokens.length && (
          <EmptyState
            icon={KeyRound}
            title={t('settings.tokensEmpty')}
            description={t('settings.tokensEmptyDesc')}
            className="flex flex-col items-center justify-center py-12 text-center"
          />
        )}
      </div>
    </div>
  );
}
