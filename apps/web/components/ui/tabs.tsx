'use client';

import * as React from 'react';
import * as TabsPrimitive from '@radix-ui/react-tabs';

import {cn} from '@/lib/utils';

type TabsVariant = 'default' | 'pill' | 'fill';

type TabsContextValue = {
  variant: TabsVariant;
  /** a11y 关联 id 的命名空间，来自 Tabs 的 `id`。空 = 沿用 Radix 自己那套。 */
  idPrefix?: string;
};

const TabsContext = React.createContext<TabsContextValue>({variant: 'default'});

/** Radix 的命名规则，沿用同一套拼法 */
function makeTriggerId(prefix: string, value: string) {
  return `${prefix}-trigger-${value}`;
}

function makeContentId(prefix: string, value: string) {
  return `${prefix}-content-${value}`;
}

/**
 * 标签容器。`id` 不只是给外层 div 用，它同时是 tab 与 panel 之间 a11y 关联 id 的
 * 命名空间，**建议总是传**。
 *
 * 为什么要自己拼 id：Radix 内部用 React.useId 生成 trigger 的 id/aria-controls 和
 * content 的 id/aria-labelledby，而 useId 的值取决于组件在整棵树里的位置。App
 * Router 给页面套的包装层数在服务端渲染与客户端 hydration 时并不完全一致，同一个
 * Tabs 两边就算出两个不同的 id，控制台报：
 *
 *   A tree hydrated but some attributes of the server rendered HTML didn't match …
 *   - aria-controls="radix-_R_1inebnaitmlb_-content-normal"   ← 服务端
 *   + aria-controls="radix-_R_35esnfaitmlb_-content-normal"   ← 客户端
 *
 * 用调用方给的 id 拼则只跟 id + value 有关，两端必定一致。
 */
function Tabs({
  id,
  className,
  variant = 'default',
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Root> & {
  variant?: TabsVariant;
}) {
  const ctx = React.useMemo<TabsContextValue>(
    () => ({variant, idPrefix: id}),
    [variant, id],
  );

  return (
    <TabsContext.Provider value={ctx}>
      <TabsPrimitive.Root
        data-slot="tabs"
        className={cn('flex flex-col gap-2', className)}
        id={id}
        {...props}
      />
    </TabsContext.Provider>
  );
}

function TabsList({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.List>) {
  const {variant} = React.useContext(TabsContext);

  return (
    <TabsPrimitive.List
      data-slot="tabs-list"
      className={cn(
          variant === 'fill' ?
            'inline-flex h-auto w-fit items-center justify-center gap-3 bg-transparent p-0 text-muted-foreground' :
          variant === 'pill' ?
            'inline-flex h-auto w-fit items-center justify-center gap-1 rounded-full bg-muted p-1 text-muted-foreground' :
            'bg-muted text-muted-foreground inline-flex h-9 w-fit items-center justify-center rounded-lg p-[3px]',
          className,
      )}
      {...props}
    />
  );
}

function TabsTrigger({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Trigger>) {
  const {variant, idPrefix} = React.useContext(TabsContext);

  return (
    <TabsPrimitive.Trigger
      data-slot="tabs-trigger"
      className={cn(
          variant === 'fill' ?
            'inline-flex flex-1 items-center justify-center gap-1 whitespace-nowrap border-b border-transparent px-0 py-0.5 text-xs font-medium text-muted-foreground transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 data-[state=active]:border-foreground/25 data-[state=active]:text-foreground [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*=\'size-\'])]:size-4' :
          variant === 'pill' ?
            'inline-flex flex-1 items-center justify-center whitespace-nowrap rounded-full px-3 py-1.5 text-xs font-medium text-muted-foreground transition-[background-color,color,box-shadow] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 data-[state=active]:bg-white data-[state=active]:text-foreground dark:data-[state=active]:bg-white/[0.08] [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*=\'size-\'])]:size-4' :
            'data-[state=active]:bg-background dark:data-[state=active]:text-foreground focus-visible:border-ring focus-visible:ring-ring/50 focus-visible:outline-ring dark:data-[state=active]:border-input dark:data-[state=active]:bg-input/30 text-foreground dark:text-muted-foreground inline-flex h-[calc(100%-1px)] flex-1 items-center justify-center gap-1.5 rounded-md border border-transparent px-2 py-1 text-sm font-medium whitespace-nowrap transition-[color,box-shadow] focus-visible:ring-[3px] focus-visible:outline-1 disabled:pointer-events-none disabled:opacity-50 data-[state=active]:shadow-sm [&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg:not([class*=\'size-\'])]:size-4',
          className,
      )}
      {...(idPrefix ?
        {id: makeTriggerId(idPrefix, props.value), 'aria-controls': makeContentId(idPrefix, props.value)} :
        null)}
      {...props}
    />
  );
}

function TabsContent({
  className,
  ...props
}: React.ComponentProps<typeof TabsPrimitive.Content>) {
  const {idPrefix} = React.useContext(TabsContext);

  return (
    <TabsPrimitive.Content
      data-slot="tabs-content"
      className={cn('flex-1 outline-none', className)}
      {...(idPrefix ?
        {id: makeContentId(idPrefix, props.value), 'aria-labelledby': makeTriggerId(idPrefix, props.value)} :
        null)}
      {...props}
    />
  );
}

export {Tabs, TabsList, TabsTrigger, TabsContent};
