import type {ReactNode} from 'react';
import {Inbox, type LucideIcon} from 'lucide-react';
import {cn} from '@/lib/utils';

/** 真空数据：带一个引导动作，与「请求失败」严格区分。 */
export function EmptyState({
  icon: Icon = Inbox,
  title,
  description,
  action,
  className,
}: {
  icon?: LucideIcon;
  title: string;
  description?: string;
  action?: ReactNode;
  className?: string;
}) {
  return (
    <div role="status" className={cn('flex flex-col items-center justify-center p-8 text-center', className)}>
      <div className="mb-4 flex size-15 items-center justify-center rounded-full bg-muted">
        <Icon className="size-8 text-muted-foreground" aria-hidden />
      </div>
      <div className="mb-2 text-base font-bold">{title}</div>
      {description && <div className="mb-4 max-w-md text-xs text-muted-foreground">{description}</div>}
      {action}
    </div>
  );
}
