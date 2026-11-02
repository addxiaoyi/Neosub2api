import type {ReactNode} from 'react';
import {AlertCircle, RefreshCw} from 'lucide-react';
import {useT} from '@/lib/i18n/provider';
import {Button} from '@/components/ui/button';
import {cn} from '@/lib/utils';

/**
 * 请求失败态：必须给出中文原因与重试入口，
 * 不能把 axios 的 Network Error / Method Not Allowed 原样甩给用户。
 */
export function ErrorState({
  title,
  description,
  onRetry,
  retryLabel,
  details,
  className,
}: {
  title?: string;
  description?: string;
  onRetry: () => void;
  retryLabel?: string;
  /** 折叠的诊断信息（URL / 原始错误），默认不展示。 */
  details?: ReactNode;
  className?: string;
}) {
  const t = useT();

  return (
    <div
      role="alert"
      className={cn('flex flex-col items-center gap-3 px-4 py-12 text-center', className)}
    >
      <AlertCircle className="size-5 text-red-600 dark:text-red-400" aria-hidden />
      <div className="text-sm">{title ?? t('skeleton.errorTitle')}</div>
      <div className="max-w-md text-xs text-muted-foreground">{description ?? t('skeleton.errorDesc')}</div>
      <Button variant="outline" size="sm" className="rounded-full" onClick={onRetry}>
        <RefreshCw className="mr-1.5 size-3.5" aria-hidden />
        {retryLabel ?? t('skeleton.retry')}
      </Button>
      {details && (
        <details className="mt-1 max-w-md text-left text-[11px] text-muted-foreground">
          <summary className="cursor-pointer select-none">{t('skeleton.details')}</summary>
          <div className="mt-1 break-all">{details}</div>
        </details>
      )}
    </div>
  );
}
