'use client';

import {Children, memo} from 'react';
import {ManagementBar} from '@/components/common/layout/ManagementBar';
import {LanguageToggle} from '@/components/common/layout/LanguageToggle';
import {RealmToggle} from '@/components/common/layout/RealmToggle';
import {RealmProvider} from '@/lib/realm-context';

const MemoizedManagementBar = memo(ManagementBar);

/**
 * 外壳基座：结构与 app/(main)/layout.tsx 完全一致——底部 dock、居中内容容器、
 * 右上角版本/语言切换。这里只是把外壳抽成可复用组件，视觉上不引入任何新导航形态。
 * 页面迁移时用 <AppShell>{children}</AppShell> 替换布局里的多层 div 即可。
 */
export function AppShell({children}: {children: React.ReactNode}) {
  return (
    <RealmProvider>
      <div className="min-h-screen flex flex-col">
        <MemoizedManagementBar />
        <div className="flex flex-1 flex-col">
          <div className="@container/main flex flex-1 flex-col gap-2">
            <div className="flex min-h-0 flex-1 flex-col px-4 pt-12 py-8 sm:px-6 md:px-8 lg:px-12">
              <div className="mx-auto flex min-h-0 w-full max-w-7xl flex-1 flex-col gap-4 pb-24 md:gap-6">
                <div className="flex flex-wrap items-center justify-end gap-2">
                  <LanguageToggle />
                  <RealmToggle />
                </div>
                {Children.toArray(children)}
              </div>
            </div>
          </div>
        </div>
      </div>
    </RealmProvider>
  );
}
