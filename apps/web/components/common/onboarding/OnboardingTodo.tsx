'use client';

/**
 * 首页的一条待办提示：向导没走完就一直在这里，但不再弹窗。
 * 只读库内状态；「已完成」由 /api/onboarding 决定，浏览器存储不参与判断。
 */
import {useEffect, useState} from 'react';
import {Sparkles} from 'lucide-react';
import {Button} from '@/components/ui/button';
import {onboardingApi, type OnboardingState} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {
  MISSING_KEY,
  ONBOARDING_UPDATED_EVENT,
  openOnboarding,
  type OnboardingStepId,
} from '@/lib/onboarding';

export function OnboardingTodo() {
  const t = useT();
  const {isAdmin, loading} = useAuth();
  const [state, setState] = useState<OnboardingState | null>(null);

  useEffect(() => {
    if (loading || !isAdmin) return;
    let cancelled = false;
    const load = () => {
      onboardingApi
        .get()
        .then((next) => {
          if (!cancelled) setState(next);
        })
        .catch(() => {
          /* 读不到就不提示，避免把「未完成」硬套在已完成的实例上 */
        });
    };
    load();
    window.addEventListener(ONBOARDING_UPDATED_EVENT, load);
    return () => {
      cancelled = true;
      window.removeEventListener(ONBOARDING_UPDATED_EVENT, load);
    };
  }, [loading, isAdmin]);

  if (!isAdmin || !state || state.completed) return null;

  const missing = state.skipped_steps as OnboardingStepId[];

  return (
    <div className="flex flex-wrap items-start justify-between gap-3 rounded-[20px] border border-amber-500/30 bg-amber-500/5 p-4">
      <div className="flex items-start gap-3">
        <Sparkles className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
        <div>
          <div className="text-sm font-medium">{t('onboarding.todo.title')}</div>
          <p className="mt-0.5 text-xs text-muted-foreground">{t('onboarding.todo.desc')}</p>
          {missing.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {missing.map((id) => (
                <span key={id} className="rounded-full bg-background/70 px-2 py-0.5 text-[11px] text-muted-foreground">
                  {t(MISSING_KEY[id])}
                </span>
              ))}
            </div>
          )}
        </div>
      </div>
      <Button size="sm" onClick={openOnboarding}>{t('onboarding.todo.continue')}</Button>
    </div>
  );
}
