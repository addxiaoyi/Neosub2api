'use client';

import {useEffect} from 'react';
import {AlertTriangle, RotateCw, Home} from 'lucide-react';
import Link from 'next/link';
import {Button} from '@/components/ui/button';
import {useT} from '@/lib/i18n/provider';

/**
 * 主应用区的兜底页。
 *
 * 批量授权会往页里塞第三方登录页，一旦它把客户端搞崩，React 会卸载整棵树——
 * 用户看到的就是纯白屏，连「重试」都没地方点，只能手动刷新。这个边界把那种
 * 情况换成一张能自愈的提示页：reset() 原地重渲染，不必丢掉当前路由。
 */
export default function MainError({
  error,
  reset,
}: {
  error: Error & {digest?: string};
  reset: () => void;
}) {
  const t = useT();

  // 页面已经崩了，日志是唯一能留下线索的地方
  useEffect(() => {
    console.error('[workbuddy] 渲染异常', error);
  }, [error]);

  return (
    <div className="flex min-h-[60vh] flex-col items-center justify-center gap-4 px-6 text-center">
      <AlertTriangle className="h-8 w-8 text-amber-500" />
      <div className="space-y-1">
        <p className="text-base font-semibold">{t('crash.title')}</p>
        <p className="max-w-md text-xs leading-relaxed text-muted-foreground">{t('crash.desc')}</p>
      </div>
      {error.digest && (
        <code className="rounded bg-muted px-2 py-1 text-[10px] text-muted-foreground">
          {error.digest}
        </code>
      )}
      <div className="flex items-center gap-2">
        <Button size="sm" className="gap-1.5 rounded-full" onClick={reset}>
          <RotateCw className="h-3.5 w-3.5" />
          {t('common.refresh')}
        </Button>
        <Link href="/dashboard">
          <Button size="sm" variant="outline" className="gap-1.5 rounded-full">
            <Home className="h-3.5 w-3.5" />
            {t('notFound.back')}
          </Button>
        </Link>
      </div>
    </div>
  );
}
