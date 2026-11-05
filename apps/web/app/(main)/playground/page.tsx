'use client';

import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {
  AlertTriangle,
  BookText,
  Bot,
  Eraser,
  Info,
  KeyRound,
  Loader2,
  RefreshCw,
  User as UserIcon,
} from 'lucide-react';

import {PageHeader} from '@/components/common/layout/PageHeader';
import {PromptLibraryDrawer} from '@/components/common/prompts/PromptLibraryDrawer';
import {Button} from '@/components/ui/button';
import {Badge} from '@/components/ui/badge';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {AiChatInput, type ChatModelOption} from '@/components/ui/ai-chat-input';
import {CopyButton} from '@/components/ui/copy-button';
import {withBasePath} from '@/lib/base-path';
import {upstreamApi, errText} from '@/lib/api';
import type {ModelListResponse} from '@/lib/types';
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

/** 加载模型分两步：先探活 /v1/models，再取 /api/models 目录 */
type LoadPhase = 'idle' | 'probe' | 'catalog';

/** 探活要快；目录接口在上游不可达时会拖十几秒，8 秒后直接降级成手填模型名 */
const PROBE_TIMEOUT_MS = 5000;
const CATALOG_TIMEOUT_MS = 8000;

const TIMED_OUT = Symbol('timedOut');

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => reject(TIMED_OUT), ms);
    p.then(
      (v) => {
        clearTimeout(timer);
        resolve(v);
      },
      (e) => {
        clearTimeout(timer);
        reject(e);
      },
    );
  });
}

/** 状态码 → 本地化文案键；没覆盖到的直接透传后端 detail */
function statusKey(status: number): string | null {
  if (status === 401) return 'playground.err401';
  if (status === 429) return 'playground.err429';
  if (status === 501) return 'playground.err501';
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

/* ── 三协议 ────────────────────────────────────────────
 * 字段名不同（messages / input / messages+max_tokens+system），
 * 流式事件与错误外壳也不同，解析逻辑集中在这里，避免三套混用。 */

type Protocol = 'chat' | 'responses' | 'messages';

const PROTOCOLS: {id: Protocol; label: string; path: string}[] = [
  {id: 'chat', label: 'playground.protocolChat', path: '/v1/chat/completions'},
  {id: 'responses', label: 'playground.protocolResponses', path: '/v1/responses'},
  {id: 'messages', label: 'playground.protocolMessages', path: '/v1/messages'},
];

/** 内容既可能是字符串，也可能是分段数组（chat 的 content / Anthropic 的 content blocks） */
function textOf(value: unknown): string {
  if (typeof value === 'string') return value;
  if (!Array.isArray(value)) return '';
  return value
    .map((part) => {
      if (typeof part === 'string') return part;
      if (isRecord(part) && typeof part.text === 'string') return part.text;
      return '';
    })
    .join('');
}

/** Responses 的正文在 output[].content[] 的 output_text 片段里 */
function responsesText(raw: unknown): string {
  if (!isRecord(raw)) return '';
  const output = raw.output;
  if (!Array.isArray(output)) {
    return typeof raw.output_text === 'string' ? raw.output_text : '';
  }
  return output.map((item) => (isRecord(item) ? textOf(item.content) : '')).join('');
}

interface UsageTick {
  prompt?: number;
  completion?: number;
  total?: number;
}

/** 各协议的 usage 字段名不同：chat 用 prompt/completion，responses/messages 用 input/output */
function usageOf(protocol: Protocol, usage: unknown): UsageTick {
  if (!isRecord(usage)) return {};
  const num = (v: unknown) => (typeof v === 'number' && Number.isFinite(v) ? v : undefined);
  if (protocol === 'chat') {
    return {
      prompt: num(usage.prompt_tokens),
      completion: num(usage.completion_tokens),
      total: num(usage.total_tokens),
    };
  }
  return {
    prompt: num(usage.input_tokens),
    completion: num(usage.output_tokens),
    total: num(usage.total_tokens),
  };
}

/** 非流式响应取正文：chat choices[0].message.content、responses output、messages content[0].text */
function answerText(protocol: Protocol, raw: unknown): string {
  if (!isRecord(raw)) return '';
  if (protocol === 'chat') {
    const choice = Array.isArray(raw.choices) ? raw.choices[0] : null;
    return isRecord(choice) && isRecord(choice.message) ? textOf(choice.message.content) : '';
  }
  if (protocol === 'responses') return responsesText(raw);
  return textOf(raw.content);
}

/** 错误外壳：messages 是 {type:"error", error:{type,message,...}}，其余是 {error:{...}, detail} */
function errorDetail(protocol: Protocol, raw: unknown): unknown {
  if (!isRecord(raw)) return '';
  if (protocol === 'messages') {
    return readableDetail(isRecord(raw.error) ? raw.error : raw);
  }
  return raw.detail ?? (isRecord(raw.error) ? raw.error.message : raw.error) ?? '';
}

interface SseTick extends UsageTick {
  delta?: string;
  error?: string;
}

/** 单个 SSE data 分片按协议取增量 / usage / 流内错误 */
function sseTick(protocol: Protocol, raw: unknown): SseTick {
  if (!isRecord(raw)) return {};
  if (protocol === 'chat') {
    // 中途失败时网关补的是 data: {"error": {...}}，随后才是 [DONE]
    if (isRecord(raw.error)) return {error: readableDetail(raw.error)};
    const choice = Array.isArray(raw.choices) ? raw.choices[0] : null;
    const content = isRecord(choice) && isRecord(choice.delta) ? choice.delta.content : null;
    const delta = typeof content === 'string' ? content : textOf(content);
    return {...usageOf('chat', raw.usage), delta: delta || undefined};
  }
  if (protocol === 'responses') {
    if (raw.type === 'response.output_text.delta') {
      return {delta: typeof raw.delta === 'string' ? raw.delta : undefined};
    }
    if (raw.type === 'response.completed' && isRecord(raw.response)) {
      return usageOf('responses', raw.response.usage);
    }
    if (raw.type === 'error') return {error: readableDetail(raw)};
    return {};
  }
  // Anthropic：正文在 content_block_delta 的 text_delta；usage 分两段到达
  if (raw.type === 'content_block_delta' && isRecord(raw.delta)) {
    const delta = raw.delta;
    return {
      delta: delta.type === 'text_delta' && typeof delta.text === 'string' ? delta.text : undefined,
    };
  }
  if (raw.type === 'message_start' && isRecord(raw.message)) {
    return usageOf('messages', raw.message.usage);
  }
  if (raw.type === 'message_delta') return usageOf('messages', raw.usage);
  if (raw.type === 'error') {
    return {error: readableDetail(isRecord(raw.error) ? raw.error : raw)};
  }
  return {};
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
  const [protocol, setProtocol] = useState<Protocol>('chat');
  const [stream, setStream] = useState(true);
  /** 仅 Anthropic Messages 必填；切回其它协议时保留用户上次的值 */
  const [maxTokens, setMaxTokens] = useState('1024');
  const [system, setSystem] = useState('');
  const [input, setInput] = useState('');
  const [promptOpen, setPromptOpen] = useState(false);
  const [msgs, setMsgs] = useState<Msg[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [loadPhase, setLoadPhase] = useState<LoadPhase>('idle');
  /** 加载失败或超时；此时不再转圈，降级为手填模型名 */
  const [degraded, setDegraded] = useState(false);
  const [loadNote, setLoadNote] = useState('');
  const [sessionTokens, setSessionTokens] = useState(0);

  const abortRef = useRef<AbortController | null>(null);
  const probeAbortRef = useRef<AbortController | null>(null);
  /** 每次加载递增；超时/跳过之后在途响应一律作废，避免回写旧状态 */
  const loadSeq = useRef(0);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const pinToBottom = useRef(true);

  const loadingModels = loadPhase !== 'idle';
  /** 列表为空且已降级 → 让用户直接手填模型名 */
  const manualMode = degraded && models.length === 0;

  // 提示词库是控制面能力，密钥与否都能开；填入正文只写回输入框
  const promptAction = (
    <Button variant="outline" size="sm" className="rounded-full" onClick={() => setPromptOpen(true)}>
      <BookText />
      {t('prompts.open')}
    </Button>
  );
  const promptDrawer = (
    <PromptLibraryDrawer open={promptOpen} onOpenChange={setPromptOpen} onUse={setInput} />
  );

  const loadModels = useCallback(
    async (key: string): Promise<'ok' | 'empty' | 'degraded' | 'auth'> => {
      if (!key) return 'degraded';
      const seq = ++loadSeq.current;
      setLoadPhase('probe');
      setLoadNote('');
      setDegraded(false);

      // 第一步：用网关密钥探活 /v1/models；401 直接退回密钥输入页
      const probeAc = new AbortController();
      probeAbortRef.current = probeAc;
      const probeTimer = setTimeout(() => probeAc.abort(), PROBE_TIMEOUT_MS);
      let probe: Response | undefined;
      try {
        probe = await fetch(withBasePath('/v1/models'), {
          headers: {Authorization: `Bearer ${key}`},
          signal: probeAc.signal,
        });
      } catch (e) {
        if (seq !== loadSeq.current) return 'degraded';
        const timedOut = e instanceof Error && e.name === 'AbortError';
        setLoadPhase('idle');
        setDegraded(true);
        setLoadNote(t(timedOut ? 'playground.probeTimeout' : 'playground.probeFailed', {
          msg: timedOut ? '' : e instanceof Error ? e.message : String(e),
        }));
        return 'degraded';
      } finally {
        clearTimeout(probeTimer);
        probeAbortRef.current = null;
      }
      if (seq !== loadSeq.current) return 'degraded';
      if (!probe) {
        setLoadPhase('idle');
        return 'degraded';
      }

      if (!probe.ok) {
        const k = statusKey(probe.status);
        const msg = k ? t(k) : `HTTP ${probe.status}`;
        if (probe.status === 401) {
          sessionStorage.removeItem(KEY_STORE);
          setApiKey('');
          setLoadPhase('idle');
          notify.err(msg);
          return 'auth';
        }
        // 非 401（如上游 502）：密钥先留着，用户可手填模型名继续试调
        setLoadPhase('idle');
        setDegraded(true);
        setLoadNote(t('playground.probeFailed', {msg}));
        return 'degraded';
      }

      // 第二步：下拉列表取管理面 /api/models，比 /v1/models 多带 provider 等元数据
      setLoadPhase('catalog');
      let catalog: ModelListResponse | undefined;
      try {
        catalog = await withTimeout(upstreamApi.models(), CATALOG_TIMEOUT_MS);
      } catch (e) {
        if (seq !== loadSeq.current) return 'degraded';
        setLoadPhase('idle');
        setDegraded(true);
        setLoadNote(
          e === TIMED_OUT
            ? t('playground.catalogTimeout')
            : t('playground.catalogFailed', {msg: errText(e)}),
        );
        return 'degraded';
      }
      if (seq !== loadSeq.current) return 'degraded';
      if (!catalog) {
        setLoadPhase('idle');
        return 'degraded';
      }

      const rows = catalog.models || [];
      setModels(
        rows.map((m) => ({
          id: m.id,
          name: m.id,
          efforts: [],
          series: m.owned_by || m.provider || '',
        })),
      );
      const live = new Set<string>();
      for (const m of rows) {
        const owners = m.providers?.length ? m.providers : [m.provider || m.owned_by];
        for (const p of owners) {
          // 只留启用中的归属家，指定 x-provider 时不会把请求打到停用服务商
          if (p && (m.provider_states?.[p]?.enabled ?? true)) live.add(p);
        }
      }
      setProviders([...live]);
      setModel((cur) => (rows.some((m) => m.id === cur) ? cur : rows[0]?.id || ''));
      setEffort('');
      setLoadPhase('idle');

      // 探活通过但没有模型：仍允许手填模型名，不把页面留成空白
      if (rows.length === 0) {
        setDegraded(true);
        setLoadNote(t('playground.catalogEmpty'));
        return 'empty';
      }
      setDegraded(false);
      setLoadNote('');
      return 'ok';
    },
    [t],
  );

  /** 不再等待：作废在途请求，直接进手填模式 */
  const skipWaiting = () => {
    loadSeq.current += 1;
    probeAbortRef.current?.abort();
    setLoadPhase('idle');
    setDegraded(true);
    setLoadNote(t('playground.loadSkipped'));
  };

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
    loadSeq.current += 1;
    probeAbortRef.current?.abort();
    sessionStorage.removeItem(KEY_STORE);
    setApiKey('');
    setModels([]);
    setProviders([]);
    setModel('');
    setMsgs([]);
    setSessionTokens(0);
    setLoadPhase('idle');
    setDegraded(false);
    setLoadNote('');
  };

  const send = useCallback(async () => {
    const text = input.trim();
    if (!text || streaming || !apiKey || !model) return;
    const spec = PROTOCOLS.find((p) => p.id === protocol) ?? PROTOCOLS[0];
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
      const headers: Record<string, string> = {'Content-Type': 'application/json'};
      if (protocol === 'messages') {
        // Anthropic 原生鉴权；网关也接受 Authorization，这里按协议走 x-api-key
        headers['x-api-key'] = apiKey;
        headers['anthropic-version'] = '2023-06-01';
      } else {
        headers.Authorization = `Bearer ${apiKey}`;
      }
      if (provider !== 'auto') headers['x-provider'] = provider;

      const history = nextMsgs.map((m) => ({role: m.role, content: m.content}));
      const body: Record<string, unknown> = {model, stream};
      if (protocol === 'chat') {
        body.messages = history;
      } else if (protocol === 'responses') {
        body.input = history;
      } else {
        body.max_tokens = Math.max(1, Math.floor(Number(maxTokens) || 1024));
        body.messages = history;
        if (system.trim()) body.system = system.trim();
      }

      const res = await fetch(withBasePath(spec.path), {
        method: 'POST',
        headers,
        signal: ac.signal,
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        let detail: unknown = '';
        try {
          detail = errorDetail(protocol, await res.json());
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

      const ctype = res.headers.get('content-type') || '';
      // 开了流但上游回了 JSON（部分服务商忽略 stream），按非流式字段取正文
      if (!stream || ctype.includes('application/json')) {
        const json: unknown = await res.json();
        const answer = answerText(protocol, json);
        const usage = usageOf(protocol, isRecord(json) ? json.usage : null);
        const tokens = usage.total ?? (usage.prompt ?? 0) + (usage.completion ?? 0);
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
        return;
      }

      const reader = res.body?.getReader();
      if (!reader) throw new Error(t('playground.unreadable'));
      const decoder = new TextDecoder();
      let buf = '';
      let answer = '';
      let promptTokens = 0;
      let completionTokens = 0;
      let totalTokens = 0;

      const consume = (line: string) => {
        if (!line.startsWith('data:')) return;
        const payload = line.slice(5).trim();
        if (!payload || payload === '[DONE]') return;
        let raw: unknown;
        try {
          raw = JSON.parse(payload);
        } catch {
          return; /* 单个分片解析失败不影响后续 */
        }
        const tick = sseTick(protocol, raw);
        if (tick.error) throw new Error(tick.error);
        if (tick.delta) {
          answer += tick.delta;
          setMsgs((cur) => {
            const copy = [...cur];
            copy[copy.length - 1] = {...copy[copy.length - 1], content: answer, done: false};
            return copy;
          });
        }
        promptTokens = Math.max(promptTokens, tick.prompt ?? 0);
        completionTokens = Math.max(completionTokens, tick.completion ?? 0);
        totalTokens = Math.max(totalTokens, tick.total ?? 0);
      };

      // 边读边按协议解析 SSE，逐段追加到气泡上（真实流式体验）
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        let idx: number;
        while ((idx = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 1);
          consume(line);
        }
      }
      consume(buf.trim());

      const tokens = totalTokens || promptTokens + completionTokens;
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
  }, [input, msgs, model, provider, apiKey, streaming, protocol, stream, maxTokens, system, t]);

  const stop = () => abortRef.current?.abort();

  const clear = () => {
    if (streaming) return;
    setMsgs([]);
    setSessionTokens(0);
  };

  const currentModel = useMemo(() => models.find((m) => m.id === model), [models, model]);

  /** 手填模型名时也塞进下拉，避免选择器一直显示「没有模型」 */
  const visibleModels = useMemo<ChatModelOption[]>(() => {
    if (models.length) return models;
    const name = model.trim();
    return name ? [{id: name, name, efforts: [], series: ''}] : [];
  }, [models, model]);

  // 请求体示例跟着协议走：切到 Responses / Messages 后不能继续展示 chat 的 JSON
  const requestExample = useMemo(() => {
    const sample = [
      ...msgs.filter((m) => m.content && !m.error),
      ...(input.trim() ? [{role: 'user' as const, content: input.trim()}] : []),
    ]
      .slice(-4)
      .map((m) => ({
        role: m.role,
        content: m.content.length > 80 ? `${m.content.slice(0, 80)}…` : m.content,
      }));
    if (sample.length === 0) sample.push({role: 'user', content: t('playground.examplePrompt')});

    const head: Record<string, unknown> = {model: model || '<model>', stream};
    if (protocol === 'messages') {
      head.max_tokens = Math.max(1, Math.floor(Number(maxTokens) || 1024));
      if (system.trim()) head.system = system.trim();
    }
    return JSON.stringify(
      protocol === 'responses' ? {...head, input: sample} : {...head, messages: sample},
      null,
      2,
    );
  }, [msgs, input, model, stream, protocol, maxTokens, system, t]);

  // 密钥输入门：没有密钥就没有模型列表，直接给一个收敛的输入卡片
  if (!apiKey) {
    return (
      <div className="flex min-h-0 flex-1 flex-col gap-4 md:gap-6">
        <PageHeader
          title={t('playground.title')}
          description={t('playground.description')}
          actions={promptAction}
        />
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
        {promptDrawer}
      </div>
    );
  }

  // 首次加载（列表还空着）时给独立卡片说明在等哪一步，避免只剩空白对话区
  if (loadingModels && models.length === 0) {
    return (
      <div className="flex min-h-0 flex-1 flex-col gap-4 md:gap-6">
        <PageHeader
          title={t('playground.title')}
          description={t('playground.description')}
          actions={promptAction}
        />
        <div className="flex flex-1 items-center justify-center">
          <div className="w-full max-w-sm space-y-3 rounded-[20px] border p-6 text-center">
            <Loader2 className="mx-auto h-5 w-5 animate-spin text-muted-foreground" />
            <div className="text-sm font-medium" role="status" aria-live="polite">
              {t(loadPhase === 'probe' ? 'playground.loadProbe' : 'playground.loadCatalog')}
            </div>
            <p className="text-[11px] leading-5 text-muted-foreground">{t('playground.loadSlowHint')}</p>
            <Button variant="outline" size="sm" className="rounded-full" onClick={skipWaiting}>
              {t('playground.loadSkip')}
            </Button>
          </div>
        </div>
        {promptDrawer}
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
            {promptAction}
            <Button
              variant="outline"
              size="sm"
              className="rounded-full"
              disabled={loadingModels}
              onClick={async () => {
                const r = await loadModels(apiKey);
                if (r === 'ok' || r === 'empty') notify.info(t('playground.modelsRefreshed'));
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

      {/* 协议选择：三种协议的字段与流式事件不同，请求体示例同步切换 */}
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs text-muted-foreground">{t('playground.protocolLabel')}</span>
          <div className="flex flex-wrap gap-1.5">
            {PROTOCOLS.map((p) => (
              <button
                key={p.id}
                type="button"
                disabled={streaming}
                onClick={() => setProtocol(p.id)}
                className={cn(
                  'rounded-full border px-3 py-1 text-[11px] font-medium transition-colors disabled:cursor-not-allowed',
                  protocol === p.id
                    ? 'border-foreground bg-foreground text-background'
                    : 'border-border/60 text-muted-foreground hover:bg-muted',
                )}
              >
                {t(p.label)}
              </button>
            ))}
          </div>
          <button
            type="button"
            disabled={streaming}
            onClick={() => setStream((v) => !v)}
            className={cn(
              'rounded-full border px-3 py-1 text-[11px] font-medium transition-colors disabled:cursor-not-allowed',
              stream
                ? 'border-foreground bg-foreground text-background'
                : 'border-border/60 text-muted-foreground hover:bg-muted',
            )}
          >
            {t('playground.streamLabel')}
          </button>
          {protocol === 'messages' && (
            <>
              <Label htmlFor="pg-max-tokens" className="text-[11px] text-muted-foreground">
                {t('playground.maxTokensLabel')}
              </Label>
              <Input
                id="pg-max-tokens"
                type="number"
                min={1}
                value={maxTokens}
                disabled={streaming}
                onChange={(e) => setMaxTokens(e.target.value)}
                className="h-7 w-24 text-[11px]"
              />
              <Input
                aria-label={t('playground.systemLabel')}
                value={system}
                disabled={streaming}
                placeholder={t('playground.systemPlaceholder')}
                onChange={(e) => setSystem(e.target.value)}
                className="h-7 w-64 text-[11px]"
              />
            </>
          )}
        </div>

        <details className="rounded-xl border border-border/60 px-3 py-2 text-[11px]">
          <summary className="cursor-pointer select-none text-muted-foreground">
            {t('playground.requestExample')}
          </summary>
          <pre className="scroll-slim mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-muted/50 p-2.5 font-mono text-[10px] leading-4">
            {requestExample}
          </pre>
        </details>
      </div>

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
                            ? 'border border-red-500/30 bg-red-500/10 text-destructive-text'
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
                            {t('playground.answerTokens', {n: fmtNumber(m.tokens)})}
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
            {degraded && loadNote && (
              <div
                className="mb-2 rounded-xl border border-amber-500/30 bg-amber-500/10 px-3 py-2.5 text-[11px] text-warning-text"
                role="status"
                aria-live="polite"
              >
                <div className="flex items-start gap-1.5">
                  <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0" />
                  <span className="min-w-0 flex-1 leading-4">{loadNote}</span>
                  <button
                    type="button"
                    disabled={loadingModels}
                    onClick={() => {
                      loadModels(apiKey);
                    }}
                    className="shrink-0 font-medium underline-offset-2 hover:underline disabled:cursor-not-allowed disabled:opacity-60"
                  >
                    {t('playground.retryLoad')}
                  </button>
                </div>
                {manualMode && (
                  <div className="mt-2 flex items-center gap-2">
                    <Input
                      value={model}
                      autoComplete="off"
                      aria-label={t('playground.manualModelLabel')}
                      placeholder={t('playground.manualModelPlaceholder')}
                      onChange={(e) => setModel(e.target.value)}
                      className="h-8 bg-background text-[11px]"
                    />
                    <span className="shrink-0 text-[10px] opacity-80">{t('playground.manualHint')}</span>
                  </div>
                )}
              </div>
            )}
            <AiChatInput
              value={input}
              onChange={setInput}
              onSend={send}
              onStop={stop}
              streaming={streaming}
              disabled={!model}
              models={visibleModels}
              model={model}
              onModelChange={setModel}
              effort={effort}
              onEffortChange={setEffort}
            />
            <div className="mt-1.5 flex items-center justify-between gap-2 px-1">
              <span className="truncate text-[11px] text-muted-foreground">
                {models.length > 0
                  ? t('playground.modelsAvailable', {count: models.length, n: models.length})
                  : manualMode
                    ? t('playground.manualModeStatus', {model: model || '—'})
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
      {promptDrawer}
    </div>
  );
}
