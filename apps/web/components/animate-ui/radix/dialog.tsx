'use client';

import * as React from 'react';
import {Dialog as DialogPrimitive} from 'radix-ui';
import {X} from 'lucide-react';
import {
  AnimatePresence,
  motion,
  type HTMLMotionProps,
  type Transition,
} from 'motion/react';

import {cn} from '@/lib/utils';
import {t} from '@/lib/i18n';
import {ScrollArea} from '@/components/ui/scroll-area';

type DialogContextType = {
  isOpen: boolean;
  // 无 DialogTrigger 的受控弹窗需要自己记住触发元素，关闭时归还焦点
  openerRef: React.RefObject<HTMLElement | null>;
};

const DialogContext = React.createContext<DialogContextType | undefined>(
    undefined,
);

const useDialog = (): DialogContextType => {
  const context = React.useContext(DialogContext);
  if (!context) {
    throw new Error('useDialog must be used within a Dialog');
  }
  return context;
};

type DialogProps = React.ComponentProps<typeof DialogPrimitive.Root>;

function Dialog({children, ...props}: DialogProps) {
  const [isOpen, setIsOpen] = React.useState(
      props?.open ?? props?.defaultOpen ?? false,
  );

  React.useEffect(() => {
    if (props?.open !== undefined) setIsOpen(props.open);
  }, [props?.open]);

  // 弹窗打开期间焦点在弹窗内，关闭期间记录最后一次落在弹窗外的焦点元素，
  // 这就是要归还的触发元素。
  const openerRef = React.useRef<HTMLElement | null>(null);

  React.useEffect(() => {
    const remember = (event: FocusEvent) => {
      const element = event.target as HTMLElement | null;
      if (element && element !== document.body && !element.closest('[role="dialog"]')) {
        openerRef.current = element;
      }
    };
    document.addEventListener('focusin', remember);
    return () => document.removeEventListener('focusin', remember);
  }, []);

  const handleOpenChange = React.useCallback(
      (open: boolean) => {
        setIsOpen(open);
        if (!open) {
          // 退场动效是 spring（~650ms），等卸载再归还焦点会让焦点长时间停在 body。
          // 关闭当帧先归还，卸载时 onCloseAutoFocus 再兜底一次。
          requestAnimationFrame(() => {
            const opener = openerRef.current;
            if (opener?.isConnected) opener.focus();
          });
        }
        props.onOpenChange?.(open);
      },
      [props],
  );

  return (
    <DialogContext.Provider value={{isOpen, openerRef}}>
      <DialogPrimitive.Root
        data-slot="dialog"
        {...props}
        onOpenChange={handleOpenChange}
      >
        {children}
      </DialogPrimitive.Root>
    </DialogContext.Provider>
  );
}

type DialogTriggerProps = React.ComponentProps<typeof DialogPrimitive.Trigger>;

function DialogTrigger(props: DialogTriggerProps) {
  return <DialogPrimitive.Trigger data-slot="dialog-trigger" {...props} />;
}

type DialogPortalProps = React.ComponentProps<typeof DialogPrimitive.Portal>;

function DialogPortal(props: DialogPortalProps) {
  return <DialogPrimitive.Portal data-slot="dialog-portal" {...props} />;
}

type DialogCloseProps = React.ComponentProps<typeof DialogPrimitive.Close>;

function DialogClose(props: DialogCloseProps) {
  return <DialogPrimitive.Close data-slot="dialog-close" {...props} />;
}

type DialogOverlayProps = React.ComponentProps<typeof DialogPrimitive.Overlay>;

function DialogOverlay({className, ...props}: DialogOverlayProps) {
  return (
    <DialogPrimitive.Overlay
      data-slot="dialog-overlay"
      className={cn(
          'fixed inset-0 z-50 bg-black/16 backdrop-blur-[2px] data-[state=open]:animate-in data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 dark:bg-black/55',
          className,
      )}
      {...props}
    />
  );
}

type FlipDirection = 'top' | 'bottom' | 'left' | 'right';

type DialogContentProps = React.ComponentProps<typeof DialogPrimitive.Content> &
  HTMLMotionProps<'div'> & {
    from?: FlipDirection;
    transition?: Transition;
    showCloseButton?: boolean;
  };

function DialogContent({
  className,
  children,
  from = 'top',
  transition = {type: 'spring', stiffness: 200, damping: 30},
  showCloseButton = true,
  ...props
}: DialogContentProps) {
  // 触发元素由 Dialog 根组件跟踪，关闭时归还焦点
  const {isOpen, openerRef} = useDialog();

  const initialRotation =
    from === 'top' || from === 'left' ? '20deg' : '-20deg';
  const isVertical = from === 'top' || from === 'bottom';
  const rotateAxis = isVertical ? 'rotateX' : 'rotateY';

  return (
    <AnimatePresence>
      {isOpen && (
        <DialogPortal forceMount data-slot="dialog-portal">
          <DialogOverlay asChild forceMount>
            <motion.div
              key="dialog-overlay"
              initial={{opacity: 0}}
              animate={{opacity: 1}}
              exit={{opacity: 0}}
              transition={{duration: 0}}
            />
          </DialogOverlay>
          <DialogPrimitive.Content
            asChild
            forceMount
            {...props}
            onCloseAutoFocus={(event) => {
              if (event.defaultPrevented) return;
              const opener = openerRef.current;
              if (opener?.isConnected) {
                event.preventDefault();
                opener.focus();
              }
            }}
          >
            <motion.div
              key="dialog-content"
              data-slot="dialog-content"
              initial={{
                opacity: 0,
                scale: 0.95,
                transform: `perspective(500px) ${rotateAxis}(${initialRotation})`,
              }}
              animate={{
                opacity: 1,
                scale: 1,
                transform: `perspective(500px) ${rotateAxis}(0deg)`,
              }}
              exit={{
                opacity: 0,
                scale: 0.95,
                transform: `perspective(500px) ${rotateAxis}(${initialRotation})`,
              }}
              transition={{...transition, duration: 0.15, ease: 'easeOut'}}
              className={cn(
                  // 背景使用不透明色：半透明会让背后遮罩透出，与不透明的 header/footer
                  // 形成明暗分界，看起来像「双层边框」。同时只保留一条 border，
                  // 不再叠加 ring，避免边框外侧多出一圈描边。
                  'fixed left-[50%] top-[50%] z-50 grid w-[calc(100%-2rem)] max-w-lg max-h-[calc(100dvh-1rem)] translate-x-[-50%] translate-y-[-50%] gap-0 overflow-x-hidden overflow-y-auto overscroll-contain rounded-[24px] border border-border/60 bg-background shadow-[0_24px_60px_rgba(15,23,42,0.10)] duration-200 sm:max-w-lg dark:border-border/70 dark:bg-background dark:shadow-[0_24px_60px_rgba(0,0,0,0.42)]',
                  className,
              )}
              {...props}
            >
              {children}
              {showCloseButton && (
                <DialogPrimitive.Close className="absolute right-4 top-4 rounded-sm opacity-70 ring-offset-background transition-opacity hover:opacity-100 focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2 disabled:pointer-events-none data-[state=open]:bg-accent data-[state=open]:text-muted-foreground">
                  <X className="h-4 w-4" />
                  <span className="sr-only">{t('common.close')}</span>
                </DialogPrimitive.Close>
              )}
            </motion.div>
          </DialogPrimitive.Content>
        </DialogPortal>
      )}
    </AnimatePresence>
  );
}

type DialogHeaderProps = React.ComponentProps<'div'>;

function DialogHeader({className, ...props}: DialogHeaderProps) {
  return (
    <div
      data-slot="dialog-header"
      className={cn(
          'flex flex-col gap-1.5 px-6 py-4 text-left bg-background',
          className,
      )}
      {...props}
    />
  );
}

type DialogFooterProps = React.ComponentProps<'div'>;

function DialogFooter({className, ...props}: DialogFooterProps) {
  return (
    <div
      data-slot="dialog-footer"
      className={cn(
          'flex flex-row justify-end gap-2 px-6 py-4 bg-background',
          className,
      )}
      {...props}
    />
  );
}

type DialogBodyProps = React.ComponentProps<typeof ScrollArea>;

function DialogBody({className, children, ...props}: DialogBodyProps) {
  return (
    <ScrollArea
      data-slot="dialog-body"
      className={cn('min-h-0 flex-1', className)}
      {...props}
    >
      {children}
    </ScrollArea>
  );
}

type DialogTitleProps = React.ComponentProps<typeof DialogPrimitive.Title>;

function DialogTitle({className, ...props}: DialogTitleProps) {
  return (
    <DialogPrimitive.Title
      data-slot="dialog-title"
      className={cn(
          'text-lg font-semibold leading-none tracking-tight',
          className,
      )}
      {...props}
    />
  );
}

type DialogDescriptionProps = React.ComponentProps<
  typeof DialogPrimitive.Description
>;

function DialogDescription({className, ...props}: DialogDescriptionProps) {
  return (
    <DialogPrimitive.Description
      data-slot="dialog-description"
      className={cn('text-xs text-muted-foreground pt-1', className)}
      {...props}
    />
  );
}

export {
  Dialog,
  DialogPortal,
  DialogOverlay,
  DialogClose,
  DialogTrigger,
  DialogContent,
  DialogHeader,
  DialogFooter,
  DialogBody,
  DialogTitle,
  DialogDescription,
  useDialog,
  type DialogContextType,
  type DialogProps,
  type DialogTriggerProps,
  type DialogPortalProps,
  type DialogCloseProps,
  type DialogOverlayProps,
  type DialogContentProps,
  type DialogHeaderProps,
  type DialogFooterProps,
  type DialogBodyProps,
  type DialogTitleProps,
  type DialogDescriptionProps,
};
