'use client';

import {useState} from 'react';
import {Inbox} from 'lucide-react';
import {AppShell} from '@/components/shell/AppShell';
import {PageHeader} from '@/components/shell/PageHeader';
import {ContentSection} from '@/components/shell/ContentSection';
import {LoadingSkeleton} from '@/components/states/LoadingSkeleton';
import {EmptyState} from '@/components/states/EmptyState';
import {ErrorState} from '@/components/states/ErrorState';
import {RiskBadge} from '@/components/states/RiskBadge';
import {ConfirmDialog} from '@/components/states/ConfirmDialog';
import {ProviderDescriptorTable} from '@/components/provider-kit/ProviderDescriptorTable';
import {ProviderConfigForm} from '@/components/provider-kit/ProviderConfigForm';
import {ProviderDetailDrawer} from '@/components/provider-kit/ProviderDetailDrawer';
import {PROVIDER_DESCRIPTORS, descriptorOf, type ProviderDescriptor} from '@/lib/provider-schema';
import {Button} from '@/components/ui/button';
import {Switch} from '@/components/ui/switch';

const FORM_SAMPLES = ['gptgrok', 'qoder', 'opencode'];

/**
 * 骨架验收页：只展示令牌、状态原语与 descriptor 渲染结果，
 * 不参与导航，也不改动任何现有页面的外观。
 */
export default function DesignPage() {
  const [selected, setSelected] = useState<ProviderDescriptor | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [formId, setFormId] = useState(FORM_SAMPLES[1]);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [anonymous, setAnonymous] = useState(false);

  const formDescriptor = descriptorOf(formId) ?? PROVIDER_DESCRIPTORS[0];

  return (
    <AppShell>
      <PageHeader
        title="前端骨架"
        description="配置驱动基座：令牌、状态原语与 provider descriptor 渲染。此页仅供验收，不进入导航。"
      />

      <ContentSection title="设计令牌" description="颜色 / 字号 / 间距统一从 app/tokens.css 读取。">
        <div className="flex flex-wrap gap-3">
          {[
            {name: '--surface', className: 'bg-surface'},
            {name: '--muted', className: 'bg-muted'},
            {name: '--background', className: 'bg-background'},
            {name: '--risk-danger', className: 'bg-risk-danger'},
            {name: '--risk-warn', className: 'bg-risk-warn'},
            {name: '--risk-safe', className: 'bg-risk-safe'},
          ].map((token) => (
            <div key={token.name} className="flex items-center gap-2 rounded-xl bg-background px-2 py-1.5">
              <span className={`size-5 rounded-md border border-border ${token.className}`} aria-hidden />
              <span className="font-mono text-[11px] text-muted-foreground">{token.name}</span>
            </div>
          ))}
        </div>
        <div className="mt-3 flex flex-wrap items-baseline gap-4">
          <span className="text-caption text-muted-foreground">text-caption</span>
          <span className="text-body">text-body</span>
          <span className="text-title font-semibold">text-title</span>
          <span className="text-display font-semibold">text-display</span>
        </div>
      </ContentSection>

      <ContentSection title="风险三档" description="图标 + 文字 + 颜色三重编码，不依赖颜色单独区分。">
        <div className="flex flex-wrap items-center gap-2">
          <RiskBadge level="safe" />
          <RiskBadge level="warn" note="GPL 传染性隔离" />
          <RiskBadge level="danger" note="上游无 LICENSE，仅内部评估" />
        </div>
      </ContentSection>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <ContentSection title="LoadingSkeleton · list">
          <LoadingSkeleton variant="list" rows={3} />
        </ContentSection>
        <ContentSection title="LoadingSkeleton · table">
          <LoadingSkeleton variant="table" rows={2} />
        </ContentSection>
        <ContentSection title="LoadingSkeleton · detail">
          <LoadingSkeleton variant="detail" />
        </ContentSection>
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <ContentSection title="EmptyState" description="真空数据，带引导动作。">
          <EmptyState
            icon={Inbox}
            title="还没有服务商"
            description="接入 first provider 后，这里会显示它的能力位与许可证风险。"
            action={
              <Button size="sm" variant="outline" className="rounded-full">
                去接入
              </Button>
            }
          />
        </ContentSection>
        <ContentSection title="ErrorState" description="请求失败必须给中文原因与重试入口。">
          <ErrorState
            description="无法连接控制面，请确认 8010 端口服务已启动。"
            onRetry={() => undefined}
            details="GET /api/providers → connect ECONNREFUSED 127.0.0.1:8010"
          />
        </ContentSection>
      </div>

      <ContentSection title="ConfirmDialog" description="影响面大的操作统一走二次确认。">
        <div className="flex items-center gap-3">
          <ConfirmDialog
            trigger={
              <Button size="sm" variant="outline" className="rounded-full">
                停用服务商
              </Button>
            }
            title="停用该服务商？"
            description="停用后它的上游地址与密钥会保留，但不再参与 /v1 路由。"
            confirmText="停用"
            destructive
            onConfirm={() => undefined}
          />
          <span className="text-[11px] text-muted-foreground">与密钥删除使用同一套弹窗与措辞。</span>
        </div>
      </ContentSection>

      <ContentSection
        title="Provider descriptor · 表格"
        description={`${PROVIDER_DESCRIPTORS.length} 家服务商全部由 lib/provider-schema.ts 驱动，新增 provider 不改组件。`}
      >
        <ProviderDescriptorTable
          descriptors={PROVIDER_DESCRIPTORS}
          onSelect={(descriptor) => {
            setSelected(descriptor);
            setDrawerOpen(true);
          }}
        />
      </ContentSection>

      <ContentSection
        title="Provider descriptor · 配置表单"
        description="字段类型、必填、敏感标记与说明都来自 descriptor。"
        actions={
          <div className="flex flex-wrap gap-1">
            {FORM_SAMPLES.map((id) => (
              <Button
                key={id}
                size="sm"
                variant={formId === id ? 'default' : 'outline'}
                className="h-7 rounded-full text-[11px]"
                onClick={() => {
                  setFormId(id);
                  setValues({});
                }}
              >
                {id}
              </Button>
            ))}
          </div>
        }
      >
        <ProviderConfigForm
          descriptor={formDescriptor}
          values={values}
          onChange={(key, value) => setValues((prev) => ({...prev, [key]: value}))}
        />
        <div className="mt-4 flex items-center gap-2 border-t border-border/60 pt-3">
          <Switch checked={anonymous} onCheckedChange={setAnonymous} aria-label="匿名访问开关示例" />
          <span className="text-[11px] text-muted-foreground">
            匿名访问示例：{anonymous ? '已开启' : '已关闭'}
          </span>
        </div>
      </ContentSection>

      <ProviderDetailDrawer descriptor={selected} open={drawerOpen} onOpenChange={setDrawerOpen} />
    </AppShell>
  );
}
