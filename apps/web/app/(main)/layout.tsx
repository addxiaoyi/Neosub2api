'use client';

import {Children, memo, useEffect, useState} from 'react';
import {useRouter} from 'next/navigation';
import {ManagementBar} from '@/components/common/layout/ManagementBar';
import {LanguageToggle} from '@/components/common/layout/LanguageToggle';
import {RealmToggle} from '@/components/common/layout/RealmToggle';
import {OnboardingWizard} from '@/components/common/onboarding/OnboardingWizard';
import {RealmProvider} from '@/lib/realm-context';
import {useAuth} from '@/lib/auth-context';
import {onboardingApi} from '@/lib/api';
import {ONBOARDING_OPEN_EVENT, isOnboardingSnoozed} from '@/lib/onboarding';

const MemoizedManagementBar = memo(ManagementBar);

/**
 * 给布局收到的 children 补 key。
 *
 * Next 交给布局的 children 是数组，而且会嵌一层。直接渲染时内层数组在协调阶段会被
 * 包成 key 为 null 的 Fragment，里头的元素拿不到 key，React 就报 "Each child in a
 * list should have a unique key"——报的是本组件，因为它是这批元素的 debug owner，
 * 跟底栏那些无关。Children.toArray 会摊平嵌套并逐个补上 key。
 */
function keyed(node: React.ReactNode) {
  return Children.toArray(node);
}

export default function MainLayout({
  children,
}: {
  children: React.ReactNode
}) {
  const {me, loading} = useAuth();
  const router = useRouter();
  const [wizardOpen, setWizardOpen] = useState(false);

  // 仅在确认未登录时跳转；不阻塞内容渲染，避免每次切页闪一下
  useEffect(() => {
    if (!loading && !me) router.replace('/login');
  }, [loading, me, router]);

  // 首次进站自动打开一次：以库内标志为准，已完成的实例永远不再弹
  useEffect(() => {
    if (loading || !me || me.role !== 'admin') return;
    let cancelled = false;
    onboardingApi
      .get()
      .then((saved) => {
        if (cancelled || saved.completed || isOnboardingSnoozed()) return;
        setWizardOpen(true);
      })
      .catch(() => {
        /* 读不到状态就当作不可用，不阻塞正常使用 */
      });
    return () => {
      cancelled = true;
    };
  }, [loading, me]);

  // 首页待办条通过事件把向导调起来，避免再挂一层 context
  useEffect(() => {
    const open = () => setWizardOpen(true);
    window.addEventListener(ONBOARDING_OPEN_EVENT, open);
    return () => window.removeEventListener(ONBOARDING_OPEN_EVENT, open);
  }, []);

  return (
    <RealmProvider>
      <main className="min-h-screen flex flex-col">
        <MemoizedManagementBar />
        <div className="flex flex-1 flex-col">
          <div className="@container/main flex flex-1 flex-col gap-2">
            <div className="flex min-h-0 flex-1 flex-col px-4 pt-12 pb-[var(--dock-safe-area)] sm:px-6 md:px-8 lg:px-12">
              {/*
                底部安全区统一在布局层留，页面不再各自补 pb：dock 实际高度 64px
                （--dock-height）+ 离屏间距 16px（DOCK_MARGIN）+ 8px 余量 = 88px，
                滚到页底时最后一排控件不会被固定 dock 压住。
              */}
              <div className="mx-auto flex min-h-0 w-full max-w-7xl flex-1 flex-col gap-4 md:gap-6">
                {/*
                  版本切换固定在右上角：底栏是照 LDC 原样保留的，控件不往那里加。
                  移动端只显示图标（compact），避免窄屏被它占掉一行。
                */}
                <div className="flex flex-wrap items-center justify-end gap-2">
                  <LanguageToggle />
                  <RealmToggle />
                </div>
                {keyed(children)}
              </div>
            </div>
          </div>
        </div>
      </main>
      {/* 非管理员连组件都不挂载：即便有人误发事件也不会弹出 */}
      {me?.role === 'admin' && <OnboardingWizard open={wizardOpen} onOpenChange={setWizardOpen} />}
    </RealmProvider>
  );
}
