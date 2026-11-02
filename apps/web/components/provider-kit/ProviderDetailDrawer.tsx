'use client';

import {useT} from '@/lib/i18n/provider';
import {STATUS_LABEL_KEY, type ProviderDescriptor} from '@/lib/provider-schema';
import {Badge} from '@/components/ui/badge';
import {
  Drawer,
  DrawerContent,
  DrawerDescription,
  DrawerHeader,
  DrawerTitle,
} from '@/components/ui/drawer';
import {RiskBadge} from '@/components/states/RiskBadge';

function Row({label, value}: {label: string; value: React.ReactNode}) {
  return (
    <div className="flex gap-3 text-xs">
      <span className="w-20 shrink-0 text-muted-foreground">{label}</span>
      <span className="min-w-0 break-words">{value}</span>
    </div>
  );
}

/** 服务商详情抽屉：全部内容来自 descriptor，不在组件里写死任何 provider。 */
export function ProviderDetailDrawer({
  descriptor,
  open,
  onOpenChange,
}: {
  descriptor: ProviderDescriptor | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const t = useT();
  if (!descriptor) return null;

  return (
    <Drawer open={open} onOpenChange={onOpenChange} direction="right">
      <DrawerContent className="sm:max-w-md">
        <DrawerHeader>
          <div className="flex items-center gap-2">
            <DrawerTitle>{descriptor.displayName}</DrawerTitle>
            <Badge
              variant={descriptor.status === 'implemented' ? 'default' : 'secondary'}
              className="rounded-full"
            >
              {t(STATUS_LABEL_KEY[descriptor.status])}
            </Badge>
          </div>
          <DrawerDescription>{descriptor.description}</DrawerDescription>
          {descriptor.riskLevel !== 'safe' && (
            <div className="flex flex-col gap-1">
              <RiskBadge level={descriptor.riskLevel} note={descriptor.riskNote} className="w-fit" />
              <span className="text-[11px] text-red-700 dark:text-amber-200">{descriptor.riskNote}</span>
            </div>
          )}
        </DrawerHeader>

        <div className="flex flex-col gap-2 px-4 pb-2">
          <Row label="ID" value={<span className="font-mono text-[11px]">{descriptor.id}</span>} />
          <Row label="来源" value={descriptor.vendor} />
          <Row label="许可证" value={<span className="font-mono text-[11px]">{descriptor.license}</span>} />
          <Row label="运行时" value={descriptor.runtime} />
          <Row label="上游地址" value={<span className="font-mono text-[11px]">{descriptor.defaultBaseUrl || '—'}</span>} />
          <Row label="协议" value={descriptor.protocols.join(' / ') || '—'} />
          <Row
            label="能力位"
            value={
              <span className="flex flex-wrap gap-1">
                {Object.entries(descriptor.capabilities)
                  .filter(([, enabled]) => enabled)
                  .map(([key]) => (
                    <span
                      key={key}
                      className="rounded-full bg-background px-1.5 py-0.5 text-[10px] text-muted-foreground"
                    >
                      {t(`providers.cap${key.charAt(0).toUpperCase()}${key.slice(1)}`)}
                    </span>
                  ))}
              </span>
            }
          />
        </div>

        <div className="flex flex-col gap-2 px-4 pb-8">
          <div className="text-sm font-medium">配置字段</div>
          {descriptor.configFields.length === 0 && (
            <span className="text-[11px] text-muted-foreground">无</span>
          )}
          {descriptor.configFields.map((field) => (
            <div key={field.key} className="flex items-start gap-2 text-[11px]">
              <span className="w-28 shrink-0 text-muted-foreground">{t(field.label)}</span>
              <span className="min-w-0">
                <span className="font-mono">{field.key}</span>
                <span className="ml-1 text-muted-foreground">
                  {field.type}
                  {field.required ? ' · 必填' : ''}
                  {field.sensitive ? ' · 敏感' : ''}
                </span>
              </span>
            </div>
          ))}
        </div>
      </DrawerContent>
    </Drawer>
  );
}
