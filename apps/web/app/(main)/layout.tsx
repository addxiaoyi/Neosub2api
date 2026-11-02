'use client';

import {Children, memo, useEffect} from 'react';
import {useRouter} from 'next/navigation';
import {ManagementBar} from '@/components/common/layout/ManagementBar';
import {LanguageToggle} from '@/components/common/layout/LanguageToggle';
import {RealmToggle} from '@/components/common/layout/RealmToggle';
import {RealmProvider} from '@/lib/realm-context';
import {useAuth} from '@/lib/auth-context';

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

  // 仅在确认未登录时跳转；不阻塞内容渲染，避免每次切页闪一下
  useEffect(() => {
    if (!loading && !me) router.replace('/login');
  }, [loading, me, router]);

  return (
    <RealmProvider>
      <div className="min-h-screen flex flex-col">
        <MemoizedManagementBar />
        <div className="flex flex-1 flex-col">
          <div className="@container/main flex flex-1 flex-col gap-2">
            <div className="flex min-h-0 flex-1 flex-col px-4 pt-12 py-8 sm:px-6 md:px-8 lg:px-12">
              <div className="mx-auto flex min-h-0 w-full max-w-7xl flex-1 flex-col gap-4 pb-24 md:gap-6">
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
      </div>
    </RealmProvider>
  );
}
