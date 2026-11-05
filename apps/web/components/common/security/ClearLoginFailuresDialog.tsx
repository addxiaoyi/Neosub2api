'use client';

import {useEffect, useState} from 'react';
import {notify} from '@/lib/toast';
import {useT} from '@/lib/i18n/provider';
import {securityApi, errText} from '@/lib/api';
import {Button} from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/animate-ui/radix/dialog';
import {Input} from '@/components/ui/input';

/**
 * 定向清除登录失败计数（admin-only）。
 *
 * 后端只认 ip / username，而且故意没给「清空全部」的通配：这里也就不做一键清空，
 * 必须至少填一个条件。语义要说清楚——清的是节流计数与锁定，audit_logs 里的
 * 失败记录不受影响，删除审计证据不是这个按钮的职责。
 */
export function ClearLoginFailuresDialog({
  open,
  onOpenChange,
  onCleared,
  prefill,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCleared: () => void;
  /** 从某一行点「清除」时带上该行的 ip / username */
  prefill?: {ip?: string; username?: string} | null;
}) {
  const t = useT();
  const [ip, setIp] = useState('');
  const [username, setUsername] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) {
      setIp(prefill?.ip ?? '');
      setUsername(prefill?.username ?? '');
      setBusy(false);
    }
  }, [open, prefill?.ip, prefill?.username]);

  const hasCondition = ip.trim().length > 0 || username.trim().length > 0;

  async function clear() {
    if (busy || !hasCondition) return;
    setBusy(true);
    try {
      const r = await securityApi.clearLoginFailures({
        ip: ip.trim(),
        username: username.trim(),
      });
      const keys = r.cleared?.throttle_keys ?? 0;
      const users = r.cleared?.users ?? 0;
      if (keys === 0 && users === 0) {
        // 无匹配是正常的 200 结果，不是错误，别弹成失败
        notify.info(t('security.clearNone'), t('security.clearNoneDesc'));
      } else {
        notify.ok(t('security.clearDone'), t('security.clearDoneDetail', {keys, users}));
      }
      onOpenChange(false);
      onCleared();
    } catch (e) {
      notify.err(errText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-[420px]" showCloseButton>
        <DialogHeader>
          <DialogTitle>{t('security.clearTitle')}</DialogTitle>
          <DialogDescription>{t('security.clearDesc')}</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <label className="block space-y-1">
            <span className="text-[11px] text-muted-foreground">{t('security.clearIp')}</span>
            <Input
              value={ip}
              onChange={(e) => setIp(e.target.value)}
              placeholder={t('security.clearIpPh')}
              autoComplete="off"
            />
          </label>
          <label className="block space-y-1">
            <span className="text-[11px] text-muted-foreground">{t('security.clearUsername')}</span>
            <Input
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              placeholder={t('security.clearUsernamePh')}
              autoComplete="off"
              onKeyDown={(e) => {
                if (e.key === 'Enter') void clear();
              }}
            />
          </label>
          <div className="rounded-xl bg-muted px-3 py-2 text-[11px] leading-5 text-muted-foreground">
            {t('security.clearNeedOne')}
          </div>
          <div className="flex justify-end gap-2">
            <Button variant="outline" className="rounded-full" onClick={() => onOpenChange(false)}>
              {t('common.cancel')}
            </Button>
            <Button className="rounded-full" onClick={clear} disabled={busy || !hasCondition}>
              {t('security.clearConfirm')}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
