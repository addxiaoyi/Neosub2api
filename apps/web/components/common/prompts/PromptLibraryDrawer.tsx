'use client';

import {useCallback, useEffect, useMemo, useState} from 'react';
import {BookText, ChevronLeft, ChevronRight, Loader2, Pencil, Plus, Search, Trash2} from 'lucide-react';

import {Badge} from '@/components/ui/badge';
import {Button} from '@/components/ui/button';
import {Input} from '@/components/ui/input';
import {Label} from '@/components/ui/label';
import {Textarea} from '@/components/ui/textarea';
import {Select, SelectContent, SelectItem, SelectTrigger, SelectValue} from '@/components/ui/select';
import {Table, TableBody, TableCell, TableHead, TableHeader, TableRow} from '@/components/ui/table';
import {Drawer, DrawerContent, DrawerDescription, DrawerHeader, DrawerTitle} from '@/components/ui/drawer';
import {ConfirmDialog} from '@/components/common/layout/ConfirmDialog';
import {EmptyState} from '@/components/common/layout/EmptyState';
import {CopyButton} from '@/components/ui/copy-button';
import {promptApi, errText} from '@/lib/api';
import {useAuth} from '@/lib/auth-context';
import {useT} from '@/lib/i18n/provider';
import {fmtDateTime} from '@/lib/format';
import {notify} from '@/lib/toast';
import type {PromptDetail, PromptInput, PromptSummary} from '@/lib/types';

const PAGE_SIZE = 10;
const ALL = '__all__';

type View =
  | {kind: 'list'}
  | {kind: 'detail'; id: number}
  | {kind: 'edit'; prompt: PromptDetail | null};

interface Draft {
  title: string;
  description: string;
  prompt: string;
  category: string;
  sub_category: string;
  mode: string;
  image_size: string;
}

const EMPTY_DRAFT: Draft = {
  title: '',
  description: '',
  prompt: '',
  category: '',
  sub_category: '',
  mode: '',
  image_size: '',
};

function toDraft(item: PromptDetail): Draft {
  return {
    title: item.title,
    description: item.description,
    prompt: item.prompt,
    category: item.category,
    sub_category: item.sub_category,
    mode: item.mode,
    image_size: item.image_size,
  };
}

/** 提示词库抽屉：列表、详情、编辑、删除都收在这一个面板里，不新增页面与入口 */
export function PromptLibraryDrawer({
  open,
  onOpenChange,
  onUse,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 把提示词正文塞进 playground 输入框；不传时只提供复制 */
  onUse?: (text: string) => void;
}) {
  const t = useT();
  const {isAdmin} = useAuth();

  const [view, setView] = useState<View>({kind: 'list'});
  const [items, setItems] = useState<PromptSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [search, setSearch] = useState('');
  const [applied, setApplied] = useState('');
  const [category, setCategory] = useState(ALL);
  const [categories, setCategories] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [detail, setDetail] = useState<PromptDetail | null>(null);
  const [detailError, setDetailError] = useState('');
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState('');

  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  const filter = useMemo(
    () => ({q: applied || undefined, category: category === ALL ? undefined : category}),
    [applied, category],
  );

  const loadList = useCallback(async () => {
    setLoading(true);
    setError('');
    try {
      const result = await promptApi.list({page, size: PAGE_SIZE, ...filter});
      setItems(result.items);
      setTotal(result.total);
    } catch (e) {
      setError(errText(e));
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [page, filter]);

  useEffect(() => {
    if (!open || view.kind !== 'list') return;
    void loadList();
  }, [open, view.kind, loadList]);

  useEffect(() => {
    if (!open) return;
    promptApi.categories().then((r) => setCategories(r.items)).catch(() => setCategories([]));
  }, [open]);

  useEffect(() => {
    if (view.kind !== 'detail') return;
    let alive = true;
    setDetailError('');
    promptApi
      .detail(view.id)
      .then((item) => {
        if (alive) setDetail(item);
      })
      .catch((e) => {
        if (alive) setDetailError(errText(e));
      });
    return () => {
      alive = false;
    };
  }, [view]);

  function openNew() {
    setFormError('');
    setDraft(EMPTY_DRAFT);
    setView({kind: 'edit', prompt: null});
  }

  function openEdit(item: PromptDetail) {
    setFormError('');
    setDraft(toDraft(item));
    setView({kind: 'edit', prompt: item});
  }

  async function save() {
    setSaving(true);
    setFormError('');
    const body: PromptInput = {
      title: draft.title.trim(),
      prompt: draft.prompt.trim(),
      description: draft.description.trim(),
      category: draft.category.trim(),
      sub_category: draft.sub_category.trim(),
      mode: draft.mode,
      image_size: draft.image_size.trim(),
    };
    try {
      const editing = view.kind === 'edit' ? view.prompt : null;
      const saved = editing ? await promptApi.update(editing.id, body) : await promptApi.create(body);
      notify.ok(t('prompts.saved'));
      setPage(1);
      setView({kind: 'detail', id: saved.id});
    } catch (e) {
      setFormError(errText(e));
    } finally {
      setSaving(false);
    }
  }

  async function remove(item: PromptDetail) {
    try {
      await promptApi.remove(item.id);
      notify.ok(t('prompts.deleted'));
      setView({kind: 'list'});
      setPage(1);
    } catch (e) {
      // 详情可能已被别的会话删掉：回到列表并刷新，避免停在失效详情
      notify.err(errText(e));
      setView({kind: 'list'});
    }
  }

  return (
    <Drawer open={open} onOpenChange={onOpenChange}>
      <DrawerContent className="sm:max-w-lg">
        <DrawerHeader>
          <div className="flex items-center gap-2">
            {view.kind !== 'list' && (
              <Button
                variant="ghost"
                size="icon"
                className="h-7 w-7 rounded-md"
                aria-label={t('prompts.back')}
                onClick={() => setView({kind: 'list'})}
              >
                <ChevronLeft className="h-4 w-4" />
              </Button>
            )}
            <div className="min-w-0 flex-1">
              <DrawerTitle>{t('prompts.title')}</DrawerTitle>
              <DrawerDescription>{t('prompts.description')}</DrawerDescription>
            </div>
            {isAdmin && view.kind === 'list' && (
              <Button size="sm" className="rounded-full" onClick={openNew}>
                <Plus className="h-3.5 w-3.5" />
                {t('prompts.new')}
              </Button>
            )}
          </div>
        </DrawerHeader>

        {view.kind === 'list' && (
          <div className="flex min-h-0 flex-1 flex-col gap-3 px-4 pb-6">
            <form
              className="flex gap-2"
              onSubmit={(e) => {
                e.preventDefault();
                setPage(1);
                setApplied(search.trim());
              }}
            >
              <div className="relative min-w-0 flex-1">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
                <Input
                  value={search}
                  placeholder={t('prompts.searchPlaceholder')}
                  className="pl-8"
                  onChange={(e) => setSearch(e.target.value)}
                />
              </div>
              <Select
                value={category}
                onValueChange={(v) => {
                  setPage(1);
                  setCategory(v);
                }}
              >
                <SelectTrigger className="w-32 shrink-0 bg-background">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={ALL}>{t('prompts.allCategories')}</SelectItem>
                  {categories.map((name) => (
                    <SelectItem key={name} value={name}>
                      {name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </form>

            {error ? (
              <div className="rounded-lg border border-destructive/40 p-4 text-xs text-destructive">
                <div className="font-medium">{t('prompts.loadFailed')}</div>
                <div className="mt-1 break-all">{error}</div>
                <Button variant="outline" size="sm" className="mt-3 rounded-full" onClick={loadList}>
                  {t('prompts.retry')}
                </Button>
              </div>
            ) : loading ? (
              <div className="flex flex-1 items-center justify-center py-10 text-xs text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                {t('prompts.loading')}
              </div>
            ) : items.length === 0 ? (
              <EmptyState
                icon={BookText}
                title={applied || category !== ALL ? t('prompts.emptyFiltered') : t('prompts.emptyTitle')}
                description={t('prompts.emptyDesc')}
                className="flex flex-1 flex-col items-center justify-center p-8 text-center"
              />
            ) : (
              <div className="min-h-0 flex-1 overflow-y-auto">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>{t('prompts.colTitle')}</TableHead>
                      <TableHead className="w-20">{t('prompts.colCategory')}</TableHead>
                      <TableHead className="w-24 text-right">{t('prompts.colUpdated')}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {items.map((item) => (
                      <TableRow key={item.id}>
                        <TableCell className="max-w-0">
                          <button
                            type="button"
                            className="block w-full truncate text-left hover:underline"
                            onClick={() => setView({kind: 'detail', id: item.id})}
                          >
                            {item.title}
                          </button>
                          <div className="truncate text-[11px] text-muted-foreground">{item.excerpt}</div>
                        </TableCell>
                        <TableCell>
                          {item.category ? (
                            <Badge variant="secondary" className="text-[10px]">
                              {item.category}
                            </Badge>
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </TableCell>
                        <TableCell className="text-right text-[11px] text-muted-foreground">
                          {fmtDateTime(item.updated_at)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}

            <div className="flex items-center justify-between text-[11px] text-muted-foreground">
              <span>{t('prompts.pageInfo', {page, pages, total})}</span>
              <div className="flex gap-1">
                <Button
                  variant="outline"
                  size="icon"
                  className="h-7 w-7 rounded-md"
                  disabled={loading || page <= 1}
                  aria-label={t('prompts.prev')}
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                >
                  <ChevronLeft className="h-3.5 w-3.5" />
                </Button>
                <Button
                  variant="outline"
                  size="icon"
                  className="h-7 w-7 rounded-md"
                  disabled={loading || page >= pages}
                  aria-label={t('prompts.next')}
                  onClick={() => setPage((p) => p + 1)}
                >
                  <ChevronRight className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          </div>
        )}

        {view.kind === 'detail' && (
          <div className="flex min-h-0 flex-1 flex-col gap-3 px-4 pb-6">
            {detailError ? (
              <div className="rounded-lg border border-destructive/40 p-4 text-xs text-destructive">
                <div className="font-medium">{t('prompts.loadDetailFailed')}</div>
                <div className="mt-1 break-all">{detailError}</div>
              </div>
            ) : !detail ? (
              <div className="flex flex-1 items-center justify-center py-10 text-xs text-muted-foreground">
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                {t('prompts.loading')}
              </div>
            ) : (
              <>
                <div className="space-y-2">
                  <div className="text-sm font-medium">{detail.title}</div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {detail.category && <Badge variant="secondary">{detail.category}</Badge>}
                    {detail.sub_category && <Badge variant="outline">{detail.sub_category}</Badge>}
                    {detail.mode && (
                      <Badge variant="outline">
                        {detail.mode === 'generate'
                          ? t('prompts.mode.generate')
                          : t('prompts.mode.edit')}
                      </Badge>
                    )}
                    {detail.image_size && <Badge variant="outline">{detail.image_size}</Badge>}
                  </div>
                  {detail.description && (
                    <p className="text-[11px] leading-5 text-muted-foreground">{detail.description}</p>
                  )}
                  <div className="text-[11px] text-muted-foreground">
                    {t('prompts.by', {user: detail.created_by})} · {fmtDateTime(detail.updated_at)}
                  </div>
                </div>
                <div className="min-h-0 flex-1 overflow-y-auto rounded-lg bg-muted p-3">
                  <pre className="whitespace-pre-wrap break-words text-xs leading-5">{detail.prompt}</pre>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <CopyButton value={detail.prompt} showLabel size="sm" variant="outline" label={t('prompts.copy')} />
                  {onUse && (
                    <Button
                      size="sm"
                      className="rounded-full"
                      onClick={() => {
                        onUse(detail.prompt);
                        onOpenChange(false);
                      }}
                    >
                      {t('prompts.use')}
                    </Button>
                  )}
                  {isAdmin && (
                    <>
                      <Button variant="outline" size="sm" className="rounded-full" onClick={() => openEdit(detail)}>
                        <Pencil className="h-3.5 w-3.5" />
                        {t('prompts.edit')}
                      </Button>
                      <ConfirmDialog
                        destructive
                        title={t('prompts.deleteTitle')}
                        description={t('prompts.deleteDesc', {title: detail.title})}
                        confirmText={t('prompts.delete')}
                        onConfirm={() => remove(detail)}
                        trigger={
                          <Button variant="outline" size="sm" className="rounded-full text-destructive">
                            <Trash2 className="h-3.5 w-3.5" />
                            {t('prompts.delete')}
                          </Button>
                        }
                      />
                    </>
                  )}
                </div>
              </>
            )}
          </div>
        )}

        {view.kind === 'edit' && (
          <form
            className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto px-4 pb-6"
            onSubmit={(e) => {
              e.preventDefault();
              if (!saving) void save();
            }}
          >
            <div className="space-y-1">
              <Label htmlFor="prompt-title" className="text-xs">
                {t('prompts.fTitle')}
              </Label>
              <Input
                id="prompt-title"
                value={draft.title}
                placeholder={t('prompts.titlePlaceholder')}
                onChange={(e) => setDraft({...draft, title: e.target.value})}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="prompt-desc" className="text-xs">
                {t('prompts.fDescription')}
              </Label>
              <Input
                id="prompt-desc"
                value={draft.description}
                placeholder={t('prompts.descriptionPlaceholder')}
                onChange={(e) => setDraft({...draft, description: e.target.value})}
              />
            </div>
            <div className="grid grid-cols-2 gap-2">
              <div className="space-y-1">
                <Label htmlFor="prompt-cat" className="text-xs">
                  {t('prompts.fCategory')}
                </Label>
                <Input
                  id="prompt-cat"
                  value={draft.category}
                  placeholder={t('prompts.categoryPlaceholder')}
                  onChange={(e) => setDraft({...draft, category: e.target.value})}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="prompt-sub" className="text-xs">
                  {t('prompts.fSubCategory')}
                </Label>
                <Input
                  id="prompt-sub"
                  value={draft.sub_category}
                  placeholder={t('prompts.subCategoryPlaceholder')}
                  onChange={(e) => setDraft({...draft, sub_category: e.target.value})}
                />
              </div>
              <div className="space-y-1">
                <Label className="text-xs">{t('prompts.fMode')}</Label>
                <Select value={draft.mode || ALL} onValueChange={(v) => setDraft({...draft, mode: v === ALL ? '' : v})}>
                  <SelectTrigger className="bg-background">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={ALL}>{t('prompts.modeAny')}</SelectItem>
                    <SelectItem value="generate">{t('prompts.mode.generate')}</SelectItem>
                    <SelectItem value="edit">{t('prompts.mode.edit')}</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1">
                <Label htmlFor="prompt-size" className="text-xs">
                  {t('prompts.fImageSize')}
                </Label>
                <Input
                  id="prompt-size"
                  value={draft.image_size}
                  placeholder={t('prompts.imageSizePlaceholder')}
                  onChange={(e) => setDraft({...draft, image_size: e.target.value})}
                />
              </div>
            </div>
            <div className="flex min-h-0 flex-1 flex-col space-y-1">
              <Label htmlFor="prompt-body" className="text-xs">
                {t('prompts.fPrompt')}
              </Label>
              <Textarea
                id="prompt-body"
                value={draft.prompt}
                placeholder={t('prompts.promptPlaceholder')}
                className="min-h-[180px] flex-1 resize-none font-mono text-xs"
                onChange={(e) => setDraft({...draft, prompt: e.target.value})}
              />
            </div>
            {formError && <div className="break-all text-xs text-destructive">{formError}</div>}
            <div className="flex justify-end gap-2">
              <Button type="button" variant="outline" size="sm" className="rounded-full" onClick={() => setView({kind: 'list'})}>
                {t('common.cancel')}
              </Button>
              <Button type="submit" size="sm" className="rounded-full" disabled={saving}>
                {saving && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                {saving ? t('prompts.saving') : t('prompts.save')}
              </Button>
            </div>
          </form>
        )}
      </DrawerContent>
    </Drawer>
  );
}
