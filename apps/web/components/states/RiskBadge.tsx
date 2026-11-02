import {Check, ShieldAlert, TriangleAlert} from 'lucide-react';
import type {RiskLevel} from '@/lib/providers';
import {useT} from '@/lib/i18n/provider';
import {Badge} from '@/components/ui/badge';
import {cn} from '@/lib/utils';

const META: Record<
  RiskLevel,
  {icon: typeof ShieldAlert; labelKey: string; badgeClass: string}
> = {
  danger: {
    icon: ShieldAlert,
    labelKey: 'risk.danger',
    badgeClass: 'bg-destructive text-white',
  },
  warn: {
    icon: TriangleAlert,
    labelKey: 'risk.warn',
    badgeClass: 'bg-amber-100 text-amber-900 dark:bg-amber-900/40 dark:text-amber-200',
  },
  safe: {
    icon: Check,
    labelKey: 'risk.safe',
    badgeClass: 'bg-secondary text-secondary-foreground',
  },
};

/**
 * 许可证风险徽章：图标 + 文字 + 颜色三重编码。
 * 只靠颜色区分对色盲与低对比度屏幕不友好，所以每种等级都带图标和固定文案。
 */
export function RiskBadge({
  level,
  note,
  className,
}: {
  level: RiskLevel;
  /** 风险说明，作为 title 提示，不展开在徽章里。 */
  note?: string;
  className?: string;
}) {
  const t = useT();
  const meta = META[level];
  const Icon = meta.icon;

  return (
    <Badge
      className={cn('h-5 gap-1 rounded-md px-1.5 text-xs font-semibold', meta.badgeClass, className)}
      title={note}
    >
      <Icon className="size-3" aria-hidden />
      {t(meta.labelKey)}
    </Badge>
  );
}
