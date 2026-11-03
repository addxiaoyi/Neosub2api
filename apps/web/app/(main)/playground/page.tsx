'use client';

import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {
  Bot,
  Eraser,
  Info,
  KeyRound,
  Loader2,
  RefreshCw,
  User as UserIcon,
} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {AiChatInput, type ChatModelOption} from '@/components/ui/ai-chat-input';
import {CopyButton} from '@/components/ui/copy-button';
import {withBasePath} from '@/lib/base-path';
import {notify} from '@/lib/toast';
import {useT, type TFn} from '@/lib/i18n/provider';
import {fmtNumber, fmtDateTime, fmtMicroUsd} from '@/lib/format';
import {cn} from '@/lib/utils';

interface Msg {
  role: 'user' | 'assistant';
  content: string;
  tokens?: number;
  /** 是否由流式拼出来的（用于显示光标） */
  done?: boolean;
  error?: boolean;
}

/** 网关密钥只在当前标签页会话内有效，关掉即清 */
const KEY_STORE = 'u2a_playground_key';

/** 状态码 → 本地化文案键；没覆盖到的直接透传后端 detail */
function statusKey(status: number): string | null {
  if (status === 401) return 'playground.err401';
  if (status === 429) return 'playground.err429';
  if (status === 502) return 'playground.err502';
  if (status === 503) return 'playground.err503';
  return null;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return !!v && typeof v === 'object' && !Array.isArray(v);
}

/** 把 detail 归一成可读文本：字符串直用，数组取 msg，对象取 message */
function readableDetail(raw: unknown): string {
  if (typeof raw === 'string') return raw;
  if (Array.isArray(raw)) {
    return raw
      .map((item) => {
        if (typeof item === 'string') return item;
        if (isRecord(item) && typeof item.msg === 'string') return item.msg;
        return '';
      })
      .filter(Boolean)
      .join('；');
  }
  if (isRecord(raw) && typeof raw.message === 'string') return raw.message;
  return '';
}

/**
 * 配额超限的 detail 是结构化对象（含 used/limit/reset_at），直接当字符串渲染
 * 会显示成 [object Object]。这里只取字段拼文案，并跳过含原始 micro 数的 message。
 */
function quotaMessage(raw: Record<string, unknown>, t: TFn): string | null {
  const isQuota = raw.type === 'quota_exceeded' || raw.unit === 'micro_usd' || raw.metric === 'micro';
  if (!isQuota) return null;
  const money = raw.unit === 'micro_usd' || raw.metric === 'micro';
  const used = money ? fmtMicroUsd(raw.used) : typeof raw.used === 'number' ? fmtNumber(raw.used) : '—';
  const limit =
    money ? fmtMicroUsd(raw.limit) : typeof raw.limit === 'number' ? fmtNumber(raw.limit) : '—';
  const resetAt =
    typeof raw.reset_at === 'number' && raw.reset_at > 0
      ? raw.reset_at
      : typeof raw.reset_after === 'number'
        ? Date.now() / 1000 + raw.reset_after
        : null;
  return t('playground.quota429', {
    used,
    limit,
    reset: resetAt ? fmtDateTime(resetAt) : '—',
  });
}

/** 429 优先用结构化字段；其余状态码优先用状态码文案，避免把 HTML 错误页塞进气泡 */
function errorMessage(status: number, raw: unknown, t: TFn): string {
  if (status === 429 && isRecord(raw)) {
    const quota = quotaMessage(raw, t);
    if (quota) return quota;
  }
  const k = statusKey(status);
  if (k) return t(k);
  return readableDetail(raw) || `HTTP ${status}`;
}

export default function PlaygroundPage() {
  const t = useT();
  const [apiKey, setApiKey] = useState('');
  const [keyDraft, setKeyDraft] = useState('');
  const [models, setModels] = useState<ChatModelOption[]>([]);
  /** 模型归属的服务商（owned_by 去重），auto = 交给网关路由 */
  const [providers, setProviders] = useState<string[]>([]);
  const [provider, setProvider] = useState('auto');
  const [model, setModel] = useState('');
  const [effort, setEffort] = useState('');
  const [input, setInput] = useState('');
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [loadingModels, setLoadingModels] = useState(false);
  const [sessionTokens, setSessionTokens] = useState(0);

  const abortRef = useRef<AbortController | null>(null);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const pinToBottom = useRef(true);

  const loadModels = useCallback(async (key: string) => {
    if (!key) return;
    setLoadingModels(true);
    try {
      const res = await fetch(withBasePath('/v1/models'), {
        headers: {Authorization: `Bearer ${key}`},
      });
      if (!res.ok) {
        const k = statusKey(res.status);
        notify.err(k ? t(k) : `HTTP ${res.status}`);
        // 401 基本是密钥错了，直接退回输入页让人重填
        if (res.status === 401) {
          sessionStorage.removeItem(KEY_STORE);
          setApiKey('');
        }
        return;
      }
      const j = (await res.json()) as {data?: {id: string; owned_by?: string}[]};
      const rows = j.data || [];
      setModels(rows.map((m) => ({id: m.id, name: m.id, efforts: [], series: m.owned_by || ''})));
      setProviders([...new Set(rows.map((m) => m.owned_by).filter((v): v is string => !!v))]);
      setModel((cur) => (rows.some((m) => m.id === cur) ? cur : rows[0]?.id || ''));
      setEffort('');
    } catch (e) {
      notify.err(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingModels(false);
    }
  }, [t]);

  // 进入页面时恢复会话内保存的密钥
  useEffect(() => {
    const saved = sessionStorage.getItem(KEY_STORE) || '';
    if (saved) {
      setApiKey(saved);
      loadModels(saved);
    }
  }, [loadModels]);

  // 自动滚到底部，但用户主动向上翻看时不要抢滚动位置
  useEffect(() => {
    const el = scrollerRef.current;
    if (!el || !pinToBottom.current) return;
    el.scrollTop = el.scrollHeight;
  }, [msgs]);

  const onScroll = () => {
    const el = scrollerRef.current;
    if (!el) return;
    pinToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const saveKey = () => {
    const v = keyDraft.trim();
    if (!v) return;
    sessionStorage.setItem(KEY_STORE, v);
    setApiKey(v);
    setKeyDraft('');
    loadModels(v);
  };

  const resetKey = () => {
    if (streaming) return;
    sessionStorage.removeItem(KEY_STORE);
    setApiKey('');
    setModels([]);
    setProviders([]);
    setModel('');
    setMsgs([]);
    setSessionTokens(0);
  };

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || streaming || !apiKey || !model) return;
    setInput('');
    pinToBottom.current = true;

    const nextMsgs: Msg[] = [...msgs, {role: 'user', content: text}];
    setMsgs([...nextMsgs, {role: 'assistant', content: '', done: false}]);
    setStreaming(true);

    const ac = new AbortController();
    abortRef.current = ac;
    try {
      // 流式对话必须用原生 fetch（axios 拿不到 ReadableStream），
      // 因此不走 axios 的 baseURL，自己补 basePath。
      const headers: Record<string, string> = {
        'Content-Type': 'application/json',
        Authorization: `Bearer ${apiKey}`,
      };
      if (provider !== 'auto') headers['x-provider'] = provider;

      const res = await fetch(withBasePath('/v1/chat/completions'), {
        method: 'POST',
        headers,
        signal: ac.signal,
        body: JSON.stringify({
          model,
          stream: true,
          messages: nextMsgs.map((m) => ({role: m.role, content: m.content})),
        }),
      });
      if (!res.ok) {
        let detail: unknown = '';
        try {
          const j: unknown = await res.json();
          if (isRecord(j)) {
            detail = j.detail ?? (isRecord(j.error) ? j.error.message : j.error) ?? '';
          }
        } catch {
          /* 响应不是 JSON，用状态码文案 */
        }
        const text2 = errorMessage(res.status, detail, t);
        setMsgs((cur) => {
          const copy = [...cur];
          copy[copy.length - 1] = {role: 'assistant', content: text2, error: true, done: true};
          return copy;
        });
        return;
      }

      const reader = res.body?.getReader();
      if (!reader) throw new Error(t('playground.unreadable'));
      const decoder = new TextDecoder();
      let buf = '';
      let answer = '';
      let tokens = 0;

      // 边读边解析 SSE，逐段追加到气泡上（真实流式体验）
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        let idx: number;
        while ((idx = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 1);
          if (!line.startsWith('data:')) continue;
          const payload = line.slice(5).trim();
          if (!payload || payload === '[DONE]') continue;
          try {
            const obj = JSON.parse(payload);
            const delta = obj?.choices?.[0]?.delta?.content;
            if (typeof delta === 'string' && delta) {
              answer += delta;
              setMsgs((cur) => {
                const copy = [...cur];
                copy[copy.length - 1] = {...copy[copy.length - 1], content: answer, done: false};
                return copy;
              });
            }
            if (obj?.usage) {
              tokens = (obj.usage.prompt_tokens || 0) + (obj.usage.completion_tokens || 0);
            }
          } catch {
            /* 单个分片解析失败不影响后续 */
          }
        }
      }

      setMsgs((cur) => {
        const copy = [...cur];
        copy[copy.length - 1] = {
          role: 'assistant',
          content: answer || t('playground.emptyAnswer'),
          tokens,
          done: true,
        };
        return copy;
      });
      if (tokens) setSessionTokens((v) => v + tokens);
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        setMsgs((cur) => {
          const copy = [...cur];
          const last = copy[copy.length - 1];
          copy[copy.length - 1] = {...last, done: true, content: last.content || t('playground.stopped')};
          return copy;
        });
      } else {
        setMsgs((cur) => {
          const copy = [...cur];
          copy[copy.length - 1] = {
            role: 'assistant',
            content: e instanceof Error ? e.message : String(e),
            error: true,
            done: true,
          };
          return copy;
        });
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }, [input, msgs, model, provider, apiKey, streaming, t]);

  const stop = () => abortRef.current?.abort();

  const clear = () => {
    if (streaming) return;
    setMsgs([]);
    setSessionTokens(0);
  };

  const currentModel = useMemo(() => models.find((m) => m.id === model), [models, model]);

  // 密钥输入门：没有密钥就没有模型列表，直接给一个收敛的输入卡片
  if (!apiKey) {
    return (
      <div className="flex min-h-0 flex-1 flex-col gap-4 md:gap-6">
        <PageHeader title={t('playground.title')} description={t('playground.description')} />
        <div className="flex flex-1 items-center justify-center">
          <div className="w-full max-w-sm space-y-4 rounded-[20px] border p-6">
            <div className="flex items-center gap-2.5">
              <div className="flex h-9 w-9 items-center justify-center rounded-full bg-muted">
                <KeyRound className="h-4 w-4 text-muted-foreground" />
              </div>
              <div>
                <div className="text-sm font-medium">{t('playground.keyTitle')}</div>
                <div className="text-[11px] text-muted-foreground">{t('playground.keyHint')}</div>
              </div>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="pg-key" className="text-xs">{t('playground.keyLabel')}</Label>
              <Input
                id="pg-key"
                type="password"
                value={keyDraft}
                autoComplete="off"
                placeholder="u2a_…"
                onChange={(e) => setKeyDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') saveKey();
                }}
              />
            </div>
            <Button className="w-full rounded-full" disabled={!keyDraft.trim()} onClick={saveKey}>
              {t('playground.keySave')}
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    // flex-1 + min-h-0：让页面占满可用高度，对话区随剩余空间自适应
    <div className="flex min-h-0 flex-1 flex-col gap-4 md:gap-6">
      <PageHeader
        title={t('playground.title')}
        description={t('playground.description')}
        actions={
          <>
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              disabled={loadingModels}
              onClick={() => {
                loadModels(apiKey);
                notify.info(t('playground.modelsRefreshed'));
              }}
            >
              <RefreshCw className={cn(loadingModels && 'animate-spin')} />
              {t('playground.refreshModels')}
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              disabled={streaming || msgs.length === 0}
              onClick={clear}
            >
              <Eraser />
              {t('playground.clearChat')}
            </Button>
            <Button variant="outline" size="sm" className="rounded-full" disabled={streaming} onClick={resetKey}>
              <KeyRound />
              {t('playground.keyChange')}
            </Button>
          </>
        }
      />

      {/* 服务商选择：auto 交给网关按模型路由；指定后通过 x-provider 头强制走该上游 */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs text-muted-foreground">{t('playground.providerLabel')}</span>
        <div className="flex flex-wrap gap-1.5">
          {['auto', ...providers].map((p) => (
            <button
              key={p}
              type="button"
              onClick={() => setProvider(p)}
              className={cn(
                'rounded-full border px-3 py-1 text-[11px] font-medium transition-colors',
                provider === p
                  ? 'border-foreground bg-foreground text-background'
                  : 'border-border/60 text-muted-foreground hover:bg-muted',
              )}
            >
              {p === 'auto' ? t('playground.providerAuto') : p}
            </button>
          ))}
        </div>
      </div>

      {/* 对话区。不加卡片底色：消息直接落在页面背景上更清爽 */}
      <section className="flex min-h-[240px] min-w-0 flex-1 flex-col overflow-hidden">
        <div ref={scrollerRef} onScroll={onScroll} className="scroll-slim min-h-0 flex-1 overflow-y-auto px-1 py-4">
          {msgs.length === 0 ? (
            <div className="flex h-full items-center justify-center">
              <div className="max-w-md space-y-2 text-center">
                <Bot className="mx-auto h-7 w-7 text-muted-foreground/60" />
                <div className="text-sm font-medium">{t('playground.startTitle')}</div>
                <p className="text-[11px] leading-5 text-muted-foreground">
                  {t('playground.startDesc')}
                </p>
                {currentModel && (
                  <p className="text-[10px] text-muted-foreground/70">
                    {t('playground.currentModel')}{' '}
                    <span className="font-mono">{currentModel.id}</span>
                    {currentModel.series && ` · ${currentModel.series}`}
                  </p>
                )}
              </div>
            </div>
          ) : (
            <div className="mx-auto w-full max-w-3xl space-y-4">
              {msgs.map((m, i) => (
                <div
                  key={i}
                  className={cn('flex gap-2.5', m.role === 'user' ? 'justify-end' : 'justify-start')}
                >
                  {m.role === 'assistant' && (
                    <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-muted">
                      <Bot className="h-3.5 w-3.5 text-muted-foreground" />
                    </div>
                  )}
                  <div className={cn('min-w-0 max-w-[82%]', m.role === 'user' && 'flex flex-col items-end')}>
                    <div
                      className={cn(
                        'whitespace-pre-wrap break-words rounded-2xl px-3.5 py-2 text-xs leading-5',
                        m.role === 'user'
                          ? 'bg-foreground text-background'
                          : m.error
                            ? 'border border-red-500/30 bg-red-500/10 text-red-600 dark:text-red-400'
                            : 'bg-muted',
                      )}
                    >
                      {m.content || <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                      {m.role === 'assistant' && !m.done && m.content && (
                        <span className="ml-0.5 inline-block h-3 w-1.5 animate-pulse bg-foreground/50 align-middle" />
                      )}
                    </div>

                    {m.role === 'assistant' && m.done && !m.error && (
                      <div className="mt-1.5 flex items-center gap-2.5 px-1 text-muted-foreground">
                        <CopyButton
                          value={m.content}
                          label=""
                          title={t('playground.copyAnswer')}
                          className="h-6 w-6"
                        />
                        {!!m.tokens && (
                          <span className="text-[10px] tabular-nums text-muted-foreground/70">
                            {fmtNumber(m.tokens)} tokens
                          </span>
                        )}
                      </div>
                    )}
                  </div>
                  {m.role === 'user' && (
                    <div className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-muted">
                      <UserIcon className="h-3.5 w-3.5 text-muted-foreground" />
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="shrink-0 px-3 pb-3 pt-2.5">
          <div className="mx-auto w-full max-w-3xl">
            <AiChatInput
              value={input}
              onChange={setInput}
              onSend={send}
              onStop={stop}
              streaming={streaming}
              disabled={!model}
              models={models}
              model={model}
              onModelChange={setModel}
              effort={effort}
              onEffortChange={setEffort}
            />
            <div className="mt-1.5 flex items-center justify-between gap-2 px-1">
              <span className="truncate text-[11px] text-muted-foreground">
                {models.length > 0
                  ? t('playground.modelsAvailable', {count: models.length, n: models.length})
                  : t('playground.noModels')}
              </span>
              <Badge
                variant="secondary"
                className="shrink-0 rounded-full text-[10px] tabular-nums"
                title={t('playground.sessionTokensTitle')}
              >
                {t('playground.sessionTokens', {v: fmtNumber(sessionTokens)})}
              </Badge>
            </div>
          </div>
        </div>
      </section>

      <p className="mx-auto flex max-w-lg shrink-0 items-start justify-center gap-1.5 text-center text-[11px] leading-5 text-muted-foreground">
        <Info className="mt-0.5 h-3 w-3 shrink-0" />
        <span>{t('playground.footnote')}</span>
      </p>
    </div>
  );
}
