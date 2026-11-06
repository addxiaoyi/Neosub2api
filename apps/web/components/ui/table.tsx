'use client';

import * as React from 'react';

import {cn} from '@/lib/utils';
import {useT} from '@/lib/i18n/provider';

const SCROLL_HINT_MIN_REST = 24;

function Table({className, ...props}: React.ComponentProps<'table'>) {
  const boxRef = React.useRef<HTMLDivElement>(null);
  const [rest, setRest] = React.useState(0);
  const t = useT();

  // 记录还有多少没露出来，供 globals.css 画边缘渐隐；只在跨过阈值时更新状态
  React.useEffect(() => {
    const box = boxRef.current;
    if (!box) return;

    const sync = () => {
      const rest = box.scrollWidth - box.clientWidth;
      const atStart = box.scrollLeft <= 1;
      const atEnd = box.scrollLeft >= rest - 1;
      box.dataset.overflow =
        rest <= 1 || (atStart && atEnd) ? 'none' : atStart ? 'end' : atEnd ? 'start' : 'both';
      setRest(rest > SCROLL_HINT_MIN_REST ? rest : 0);
    };
    sync();

    const observer = new ResizeObserver(sync);
    observer.observe(box);
    if (box.firstElementChild) observer.observe(box.firstElementChild);
    box.addEventListener('scroll', sync, {passive: true});
    return () => {
      box.removeEventListener('scroll', sync);
      observer.disconnect();
    };
  }, []);

  return (
    <div className="w-full min-w-0">
      <div
        ref={boxRef}
        data-slot="table-container"
        className="scroll-slim relative w-full overflow-x-auto"
      >
        <table
          data-slot="table"
          className={cn('w-full caption-bottom text-sm', className)}
          {...props}
        />
      </div>
      {rest > 0 && (
        <p className="text-muted-foreground mt-1 text-[11px] select-none" data-slot="table-scroll-hint">
          {t('common.tableScrollHint')}
        </p>
      )}
    </div>
  );
}

function TableHeader({className, ...props}: React.ComponentProps<'thead'>) {
  return (
    <thead
      data-slot="table-header"
      className={cn('[&_tr]:border-b', className)}
      {...props}
    />
  );
}

function TableBody({className, ...props}: React.ComponentProps<'tbody'>) {
  return (
    <tbody
      data-slot="table-body"
      className={cn('[&_tr:last-child]:border-0', className)}
      {...props}
    />
  );
}

function TableFooter({className, ...props}: React.ComponentProps<'tfoot'>) {
  return (
    <tfoot
      data-slot="table-footer"
      className={cn(
          'bg-muted/50 border-t font-medium [&>tr]:last:border-b-0',
          className,
      )}
      {...props}
    />
  );
}

function TableRow({className, ...props}: React.ComponentProps<'tr'>) {
  return (
    <tr
      data-slot="table-row"
      className={cn(
          'hover:bg-muted/50 data-[state=selected]:bg-muted border-b transition-colors',
          className,
      )}
      {...props}
    />
  );
}

function TableHead({className, ...props}: React.ComponentProps<'th'>) {
  return (
    <th
      data-slot="table-head"
      className={cn(
          'text-foreground h-10 px-2 text-left align-middle font-medium whitespace-nowrap [&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[2px]',
          className,
      )}
      {...props}
    />
  );
}

function TableCell({className, ...props}: React.ComponentProps<'td'>) {
  return (
    <td
      data-slot="table-cell"
      className={cn(
          'p-2 align-middle whitespace-nowrap [&:has([role=checkbox])]:pr-0 [&>[role=checkbox]]:translate-y-[2px]',
          className,
      )}
      {...props}
    />
  );
}

function TableCaption({
  className,
  ...props
}: React.ComponentProps<'caption'>) {
  return (
    <caption
      data-slot="table-caption"
      className={cn('text-muted-foreground mt-4 text-sm', className)}
      {...props}
    />
  );
}

export {
  Table,
  TableHeader,
  TableBody,
  TableFooter,
  TableHead,
  TableRow,
  TableCell,
  TableCaption,
};
