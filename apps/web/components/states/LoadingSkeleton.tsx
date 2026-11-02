import {cn} from '@/lib/utils';
import {useT} from '@/lib/i18n/provider';

type Variant = 'list' | 'table' | 'detail';

function Bar({className}: {className?: string}) {
  return <div className={cn('animate-pulse rounded-md bg-muted-foreground/15', className)} />;
}

/**
 * 加载骨架：列表 / 表格 / 详情三种形态，共用同一外框尺寸，
 * 避免请求未返回时先渲染 0 或空状态。
 */
export function LoadingSkeleton({
  variant = 'list',
  rows = 4,
  className,
}: {
  variant?: Variant;
  rows?: number;
  className?: string;
}) {
  const t = useT();

  return (
    <div
      role="status"
      aria-busy="true"
      aria-live="polite"
      className={cn('w-full rounded-[20px] bg-muted p-4', className)}
    >
      <span className="sr-only">{t('skeleton.loading')}</span>

      {variant === 'list' && (
        <div className="flex flex-col gap-3">
          {Array.from({length: rows}).map((_, index) => (
            <div key={index} className="flex items-center gap-3">
              <Bar className="size-9 shrink-0 rounded-full" />
              <div className="flex min-w-0 flex-1 flex-col gap-2">
                <Bar className="h-3.5 w-2/5" />
                <Bar className="h-3 w-3/5" />
              </div>
            </div>
          ))}
        </div>
      )}

      {variant === 'table' && (
        <div className="flex flex-col gap-2">
          <div className="grid grid-cols-4 gap-3 pb-2">
            {Array.from({length: 4}).map((_, index) => (
              <Bar key={index} className="h-3 w-3/4" />
            ))}
          </div>
          {Array.from({length: rows}).map((_, index) => (
            <div key={index} className="grid grid-cols-4 gap-3">
              {Array.from({length: 4}).map((__, cell) => (
                <Bar key={cell} className="h-8" />
              ))}
            </div>
          ))}
        </div>
      )}

      {variant === 'detail' && (
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Bar className="h-5 w-1/3" />
            <Bar className="h-3 w-2/3" />
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {Array.from({length: 4}).map((_, index) => (
              <div key={index} className="flex flex-col gap-2">
                <Bar className="h-3 w-1/4" />
                <Bar className="h-9" />
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
