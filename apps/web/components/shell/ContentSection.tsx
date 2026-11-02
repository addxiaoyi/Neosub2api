import type {ReactNode} from 'react';
import {cn} from '@/lib/utils';

/**
 * 内容分区：沿用现有页面的「20px 圆角 + muted 底」卡片语言。
 * 只在需要分组标题 / 操作时包一层，不改变页面本身的间距与配色。
 */
export function ContentSection({
  title,
  description,
  actions,
  children,
  className,
  bodyClassName,
}: {
  title?: string;
  description?: string;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={cn('overflow-hidden rounded-[20px] bg-muted', className)}>
      {(title || actions) && (
        <div className="flex flex-col gap-2 px-4 pb-2 pt-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            {title && <h2 className="text-sm font-medium">{title}</h2>}
            {description && <p className="mt-0.5 text-[11px] text-muted-foreground">{description}</p>}
          </div>
          {actions && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
        </div>
      )}
      <div className={cn('p-4', bodyClassName)}>{children}</div>
    </section>
  );
}
