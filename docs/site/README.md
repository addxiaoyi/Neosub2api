# 离线文档站

`docs/site/index.html` 是本仓库文档的离线快照：单文件、无 CDN、无网络请求，双击即可浏览。
它由 `build.py` 从仓库里的 Markdown 生成，**index.html 是生成物，不要手改**；要改内容请改原 Markdown 后重建。

## 目录

| 文件 | 作用 |
| --- | --- |
| `index.html` | 生成后的站点（左侧目录树 + 右侧正文，含站内搜索） |
| `build.py` | 生成器，仅用 Python 标准库 |
| `build.ps1` | Windows 重建入口（PowerShell 5.1 / 7 均可），优先调用仓库自带的 `.runtime/python` |
| `.gitignore` | 忽略生成物 `index.html`，避免把大文件误入库 |

## 重建

仓库根目录执行：

```powershell
powershell -ExecutionPolicy Bypass -File docs\site\build.ps1
```

或直接调用仓库自带 Python（Windows 下推荐）：

```powershell
.\.runtime\python\python.exe docs\site\build.py
```

其他参数：

```powershell
# 指定仓库根与输出位置（输出到仓外也能构建，只是仓内文件链接会失去相对关系）
.\.runtime\python\python.exe docs\site\build.py --repo-root <仓库根> --out <输出路径>\index.html
```

构建成功时输出文档数、分组数与文件大小；有警告（坏了链接、未收录的 Markdown）会打印到 stderr，但站点仍会生成。
清单里写了但文件不存在时，构建直接失败并指出路径。

## 加一篇新文档

1. 把 Markdown 放到合适目录（例如 `docs/NEW-TOPIC.md`）。
2. 打开 `build.py`，在 `GROUPS` 里对应分组追加一条：

   ```python
   {"path": "docs/NEW-TOPIC.md", "slug": "new-topic", "label": "新主题"}
   ```

   - `path`：相对仓库根，必须与实际文件一致。
   - `slug`：站点内路由与锚点前缀，必须唯一（`#doc-new-topic`）。
   - `label`：左侧目录显示名；正文标题仍取 Markdown 的一级标题。
   - 归档文档加 `"archived": True`（目录与页头会显示「存档」）。
   - 需要严重度色块的文档加 `"severity": "audit"`（critical/high/medium/low）或 `"severity": "ux"`（blocker/major/minor）。
3. 重新运行 `build.ps1`。

文档之间的 `.md` 链接会自动改写成站内锚点；指向源码、YAML、图片等仓内文件的链接会转成**相对仓库的路径**（如 `../../apps/control/app/adapters/base.py`），生成物里不会出现本机绝对路径（`file:///…`）。写成 `file:///…` 的仓内 Markdown 链接同样会落到站内锚点；`.md` 文件存在但没进 `GROUPS` 时会给出 warning，避免留下静态托管下 404 的裸 `.md` 链接。

## 已知限制

- **搜索是纯子串匹配**：不做分词、不区分词形；中文按原文子串命中。搜索只过滤「当前打开的这篇正文」，左侧目录会列出全站命中的文档及次数，点进去后正文自动过滤并高亮。
- **宽表格 / 代码块在窄屏各自横向滚动**：页面本身不产生横向滚动，但列很多的表（如安全审计问题清单）在 768 下需要表内左右滑动。
- **无语法高亮**：代码块保留原文与换行，只有等宽字体与底色。
- **仓内文件链接是相对路径**：`index.html` 必须留在 `docs/site/`（或保持与仓库的层级关系）才能打开源码/图片链接；单独把 HTML 拷到别处时这些链接会失效。`https://` 链接保留原样，点击才会联网，站点本身加载不联网。
- **未内嵌的资产**：`docs/design/prototype.html` 与 `docs/ux-shots/*.png` 不进入站点，只在正文里以相对路径链接。
- **单文件体积**：目前约 1074 KB（43 篇），全部正文内嵌，换目录或加文档会线性增长。
- **服务商契约篇数**：`docs/providers/*.md` 按实际文件收录；未进清单的篇目会在构建时以 warning 列出。
