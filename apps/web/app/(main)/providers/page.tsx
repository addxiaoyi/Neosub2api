'use client';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {ProviderPanel} from '@/components/common/providers/ProviderPanel';
import {useT} from '@/lib/i18n/provider';

export default function ProvidersPage() {
  const t = useT();
  return (
    <div className="flex flex-col gap-4 md:gap-6">
      <PageHeader title={t('providers.title')} description={t('providers.desc')} />
      <ProviderPanel />
    </div>
  );
}
