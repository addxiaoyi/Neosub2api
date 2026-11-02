'use client';

import {useT} from '@/lib/i18n/provider';
import type {ProviderConfigField, ProviderDescriptor} from '@/lib/provider-schema';
import {Input} from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {Switch} from '@/components/ui/switch';
import {Textarea} from '@/components/ui/textarea';

type Values = Record<string, unknown>;

function controlOf(
  field: ProviderConfigField,
  values: Values,
  onChange: (key: string, value: unknown) => void,
  disabled?: boolean,
) {
  const value = values[field.key];

  if (field.type === 'switch') {
    return (
      <Switch
        checked={Boolean(value)}
        disabled={disabled}
        onCheckedChange={(checked) => onChange(field.key, checked)}
      />
    );
  }

  if (field.type === 'select') {
    return (
      <Select
        value={typeof value === 'string' ? value : ''}
        disabled={disabled}
        onValueChange={(next) => onChange(field.key, next)}
      >
        <SelectTrigger size="sm" className="w-[220px] text-[11px]">
          <SelectValue placeholder={field.placeholder} />
        </SelectTrigger>
        <SelectContent>
          {(field.options ?? []).map((option) => (
            <SelectItem key={option.value} value={option.value}>
              {option.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }

  if (field.type === 'json') {
    return (
      <Textarea
        rows={2}
        value={typeof value === 'string' ? value : JSON.stringify(value ?? {})}
        disabled={disabled}
        onChange={(event) => onChange(field.key, event.target.value)}
        placeholder={field.placeholder}
        className="w-[380px] bg-background font-mono text-[11px]"
      />
    );
  }

  return (
    <Input
      type={field.type === 'password' ? 'password' : field.type === 'number' ? 'number' : 'text'}
      value={String(value ?? '')}
      disabled={disabled}
      onChange={(event) => onChange(field.key, event.target.value)}
      placeholder={field.placeholder}
      className="h-8 w-[280px] bg-background font-mono text-[11px]"
    />
  );
}

/** 按 descriptor 的 configFields 渲染表单：类型、必填、敏感、说明全部来自配置。 */
export function ProviderConfigForm({
  descriptor,
  values,
  onChange,
  disabled,
}: {
  descriptor: ProviderDescriptor;
  values: Values;
  onChange: (key: string, value: unknown) => void;
  disabled?: boolean;
}) {
  const t = useT();

  if (!descriptor.configFields.length) {
    return <p className="text-xs text-muted-foreground">该服务商没有可配置字段。</p>;
  }

  return (
    <div className="flex flex-col gap-3">
      {descriptor.configFields.map((field) => (
        <div key={field.key} className="flex flex-col gap-1 sm:flex-row sm:items-start sm:gap-2">
          <span className="w-28 shrink-0 pt-1.5 text-[11px] text-muted-foreground">
            {t(field.label)}
            {field.required && <span className="ml-0.5 text-red-600 dark:text-red-400">*</span>}
          </span>
          <div className="flex min-w-0 flex-col gap-1">
            {controlOf(field, values, onChange, disabled)}
            {field.description && (
              <span className="max-w-[380px] text-[10px] text-muted-foreground">{field.description}</span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
