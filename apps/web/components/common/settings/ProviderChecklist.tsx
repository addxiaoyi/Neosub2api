'use client';

import Link from 'next/link';
import {useT} from '@/lib/i18n/provider';
import {buildChecklist, type ChecklistStep} from '@/lib/provider-checklist';
import type {ProviderInfo} from '@/lib/providers';
import {cn} from '@/lib/utils';
import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';

interface Props {
  provider: ProviderInfo;
  healthStatus: string | null;
  healthError?: string | null;
  canEdit: boolean;
  onEnable: () => void;
  onRunHealth: () => void;
  onPick?: (kind: ChecklistStep['kind']) => void;
}

const ACTION_LABELS: Record<NonNullable<ChecklistStep['action']>, string> = {
  enable: 'providers.checklist.actionEnable',
  credential: 'providers.checklist.actionCredential',
  option: 'providers.checklist.actionOption',
  health: 'providers.checklist.actionHealth',
  console: 'providers.checklist.actionConsole',
  quota: 'providers.checklist.actionQuota',
  playground: 'providers.checklist.actionPlayground',
};

const EDIT_ACTIONS = new Set<NonNullable<ChecklistStep['action']>>(['enable', 'credential', 'option', 'health']);

export function ProviderChecklist({provider, healthStatus, healthError, canEdit, onEnable, onRunHealth, onPick}: Props) {
  const t = useT();
  const state = buildChecklist(provider, healthStatus);
  const lockedTitle = t('providers.requiresAdmin');
  const ready = provider.readiness === 'ready';

  function actionButton(step: ChecklistStep) {
    const action = step.action;
    if (!action) return null;
    const label = t(ACTION_LABELS[action]);

    if (action === 'quota' || action === 'playground') {
      return (
        <Button asChild size="sm" variant="outline" className="h-7 shrink-0 rounded-full text-[11px]">
          <Link href={action === 'quota' ? '/quota' : '/playground'}>{label}</Link>
        </Button>
      );
    }

    const locked = !canEdit && EDIT_ACTIONS.has(action);
    return (
      <Button
        size="sm"
        variant="outline"
        className="h-7 shrink-0 rounded-full text-[11px]"
        disabled={locked}
        title={locked ? lockedTitle : undefined}
        onClick={() => {
          if (action === 'console') {
            window.open(provider.base_url, '_blank', 'noopener');
            return;
          }
          if (action === 'enable') {
            onEnable();
            return;
          }
          if (action === 'health') {
            onRunHealth();
            return;
          }
          onPick?.(step.kind);
        }}
      >
        {label}
      </Button>
    );
  }

  return (
    <div className="rounded-xl bg-muted/60 px-3 py-2">
      <div className="mb-2 flex flex-wrap items-center gap-1.5">
        {state.remaining > 0 ? (
          <Badge
            variant="secondary"
            className="rounded-full bg-amber-500/10 text-[10px] text-amber-700 dark:text-amber-400"
          >
            {t('providers.checklist.remaining', {count: state.remaining})}
          </Badge>
        ) : (
          <Badge
            variant="secondary"
            className="rounded-full bg-emerald-500/10 text-[10px] text-emerald-700 dark:text-emerald-400"
          >
            {t('providers.checklist.ready')}
          </Badge>
        )}
        {/* 「代码已实现」只描述实现状态，不表示可用；缺配置单独一枚，两者不合并 */}
        <Badge variant="secondary" className="rounded-full text-[10px] text-muted-foreground">
          {t(provider.status === 'stub' ? 'providers.statusStub' : 'providers.checklist.implemented')}
        </Badge>
        {!ready && (
          <Badge variant="secondary" className="rounded-full text-[10px] text-muted-foreground">
            {t('providers.checklist.needsConfig')}
          </Badge>
        )}
      </div>

      <ol className={cn('space-y-1.5', ready && 'hidden')}>
        {state.steps.map((step, i) => {
          const isDone = state.done[i];
          const isCurrent = i === state.current;
          const failed = step.kind === 'health' && healthStatus !== null && healthStatus !== 'ok';
          return (
            <li
              key={step.id}
              aria-current={isCurrent ? 'step' : undefined}
              className={cn(
                'flex items-start gap-2 rounded-lg border border-border/40 px-2.5 py-2',
                isCurrent && 'border-primary/40 border-l-2 border-l-primary bg-primary/5',
                isDone && 'text-muted-foreground',
              )}
            >
              <span
                className={cn(
                  'mt-0.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full border text-[10px] font-medium',
                  isCurrent
                    ? 'border-primary bg-primary/10 text-primary'
                    : 'border-border text-muted-foreground',
                )}
              >
                {i + 1}
              </span>
              <div className="min-w-0 flex-1 text-[11px] leading-4">
                <span className={cn('whitespace-normal', isDone && 'line-through')}>{t(step.detail)}</span>
                {failed && healthError ? (
                  <div className="mt-1 whitespace-normal text-[10px] text-destructive">{healthError}</div>
                ) : null}
                {step.cli && (
                  <div className="mt-1">
                    <code className="rounded bg-background/60 px-1 py-0.5 font-mono text-[10px]">
                      {t('providers.checklist.cliHint', {cmd: step.cli})}
                    </code>
                  </div>
                )}
              </div>
              <Badge
                variant={isCurrent ? 'default' : 'secondary'}
                className={cn(
                  'mt-0.5 shrink-0 rounded-full text-[10px]',
                  isCurrent && 'bg-primary/10 text-primary',
                )}
              >
                {t(
                  isDone
                    ? 'providers.checklist.stepDone'
                    : failed
                      ? 'providers.checklist.stepFailed'
                      : isCurrent
                        ? 'providers.checklist.stepCurrent'
                        : 'providers.checklist.stepPending',
                )}
              </Badge>
              {actionButton(step)}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
