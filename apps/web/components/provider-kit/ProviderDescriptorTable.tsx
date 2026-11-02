'use client';

import {useT} from '@/lib/i18n/provider';
import {CAPABILITY_LABELS, STATUS_LABEL_KEY, type ProviderDescriptor} from '@/lib/provider-schema';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {RiskBadge} from '@/components/states/RiskBadge';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

/**
 * 由 descriptor 渲染的服务商表格：列结构与 providers 页一致，
 * 新增 provider 只要在 provider-schema.ts 里加一条，表格自动出现。
 */
export function ProviderDescriptorTable({
  descriptors,
  onSelect,
}: {
  descriptors: ProviderDescriptor[];
  onSelect?: (descriptor: ProviderDescriptor) => void;
}) {
  const t = useT();

  return (
    <div className="overflow-x-auto rounded-[20px] bg-muted">
      <Table>
        <TableHeader>
          <TableRow className="border-b border-border/60 hover:bg-transparent">
            <TableHead className="pl-4 text-[11px] text-muted-foreground">{t('providers.colName')}</TableHead>
            <TableHead className="text-[11px] text-muted-foreground">{t('providers.colStatus')}</TableHead>
            <TableHead className="text-[11px] text-muted-foreground">{t('providers.colLicense')}</TableHead>
            <TableHead className="text-[11px] text-muted-foreground">{t('providers.colCapabilities')}</TableHead>
            <TableHead className="text-[11px] text-muted-foreground">{t('providers.colBaseUrl')}</TableHead>
            <TableHead className="pr-4 text-right text-[11px] text-muted-foreground">
              {t('providers.colEnabled')}
            </TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {descriptors.map((descriptor) => (
            <TableRow key={descriptor.id} className="border-b border-border/40">
              <TableCell className="pl-4">
                <div className="flex flex-col gap-0.5">
                  <div className="flex items-center gap-1.5">
                    <span className="text-sm font-medium">{descriptor.displayName}</span>
                    {descriptor.riskLevel !== 'safe' && (
                      <RiskBadge level={descriptor.riskLevel} note={descriptor.riskNote} />
                    )}
                  </div>
                  <div className="text-[10px] text-muted-foreground">{descriptor.vendor}</div>
                  <div className="text-[10px] text-muted-foreground">{descriptor.id}</div>
                </div>
              </TableCell>
              <TableCell>
                <Badge
                  variant={descriptor.status === 'implemented' ? 'default' : 'secondary'}
                  className="rounded-full"
                >
                  {t(STATUS_LABEL_KEY[descriptor.status])}
                </Badge>
                <div className="mt-1 text-[10px] text-muted-foreground">{descriptor.runtime}</div>
              </TableCell>
              <TableCell className="font-mono text-[11px] text-muted-foreground">{descriptor.license}</TableCell>
              <TableCell>
                <div className="flex max-w-[170px] flex-wrap gap-1">
                  {CAPABILITY_LABELS.filter(([key]) => descriptor.capabilities[key]).map(([key, label]) => (
                    <span
                      key={key}
                      className="rounded-full bg-background px-1.5 py-0.5 text-[10px] text-muted-foreground"
                    >
                      {t(label)}
                    </span>
                  ))}
                  {descriptor.protocols.length > 0 && (
                    <span className="rounded-full bg-background px-1.5 py-0.5 text-[10px] text-muted-foreground">
                      {descriptor.protocols.join(' / ')}
                    </span>
                  )}
                </div>
              </TableCell>
              <TableCell className="font-mono text-[11px] text-muted-foreground">
                {descriptor.defaultBaseUrl || '—'}
              </TableCell>
              <TableCell className="pr-4 text-right">
                {onSelect && (
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-7 rounded-full text-[11px]"
                    onClick={() => onSelect(descriptor)}
                  >
                    详情
                  </Button>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
