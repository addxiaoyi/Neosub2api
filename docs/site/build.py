#!/usr/bin/env python3
"""Rebuild docs/site/index.html from the repository Markdown sources.

Standard library only, no network. Run through build.ps1 or directly:

    .runtime\\python\\python.exe docs\\site\\build.py

The generated index.html is a build artifact: edit the Markdown, not the HTML.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import posixpath
import re
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

SITE_DIR = Path(__file__).resolve().parent

# --------------------------------------------------------------------------
# Manifest: every nav entry maps to exactly one Markdown file in the repo.
# Add a document by appending an entry here, then rerun the build.
# --------------------------------------------------------------------------
GROUPS = [
    {
        "id": "start",
        "label": "快速开始",
        "docs": [
            {"path": "README.md", "slug": "overview", "label": "项目总览"},
            {"path": "docs/GETTING-STARTED.md", "slug": "getting-started", "label": "从零启动"},
            {"path": "CHANGELOG.md", "slug": "changelog", "label": "更新日志"},
        ],
    },
    {
        "id": "api",
        "label": "接口",
        "docs": [
            {"path": "docs/API-REFERENCE.md", "slug": "api-reference", "label": "API 参考"},
            {"path": "docs/model-routing.md", "slug": "model-routing", "label": "模型别名与路由矩阵"},
            {"path": "docs/model-map-migration.md", "slug": "model-map-migration", "label": "model_map 迁移方案"},
        ],
    },
    {
        "id": "providers",
        "label": "服务商",
        "docs": [
            {"path": "docs/PROVIDERS.md", "slug": "providers", "label": "服务商总览"},
            {"path": "docs/ADDING-A-PROVIDER.md", "slug": "adding-a-provider", "label": "新增一家 provider"},
            {"path": "docs/providers/codar2api.md", "slug": "provider-codar2api", "label": "codar2api"},
            {"path": "docs/providers/codar_sidecar.md", "slug": "provider-codar-sidecar", "label": "codar_sidecar（本仓实现）"},
            {"path": "docs/providers/grok-register.md", "slug": "provider-grok-register", "label": "grok-register"},
            {"path": "docs/providers/gptgrok2api.md", "slug": "provider-gptgrok2api", "label": "gptgrok2api"},
            {"path": "docs/providers/muse2api.md", "slug": "provider-muse2api", "label": "muse2api"},
            {"path": "docs/providers/muse2api-protocols.md", "slug": "provider-muse2api-protocols", "label": "muse2api 协议扩展"},
            {"path": "docs/providers/opencode2api.md", "slug": "provider-opencode2api", "label": "opencode2api"},
            {"path": "docs/providers/qoder.md", "slug": "provider-qoder", "label": "qoder"},
            {"path": "docs/providers/qoder-adapter.md", "slug": "provider-qoder-adapter", "label": "qoder（内置直连）"},
            {"path": "docs/providers/sub2api.md", "slug": "provider-sub2api", "label": "sub2api"},
            {"path": "docs/providers/trae2api.md", "slug": "provider-trae2api", "label": "trae2api"},
            {"path": "docs/providers/trae-adapter.md", "slug": "provider-trae-adapter", "label": "trae（内置直连）"},
            {"path": "docs/providers/zcode2api.md", "slug": "provider-zcode2api", "label": "zcode2api"},
            {"path": "docs/providers/deepseek-feasibility.md", "slug": "provider-deepseek-feasibility", "label": "Deepseek 可行性"},
            {"path": "docs/providers/deepseek2go-feasibility.md", "slug": "provider-deepseek2go", "label": "Deepseek2Go 可行性"},
        ],
    },
    {
        "id": "deploy",
        "label": "部署",
        "docs": [
            {"path": "docs/DEPLOYMENT.md", "slug": "deployment", "label": "本地与容器部署"},
            {"path": "docs/DEPLOYMENT-PROD.md", "slug": "deployment-prod", "label": "生产部署（硬化）"},
        ],
    },
    {
        "id": "ops",
        "label": "运维",
        "docs": [
            {"path": "docs/TROUBLESHOOTING.md", "slug": "troubleshooting", "label": "故障排查"},
            {"path": "docs/UPSTREAM-SYNC.md", "slug": "upstream-sync", "label": "上游同步流程"},
            {"path": "upstreams/README.md", "slug": "upstreams", "label": "上游追踪清单"},
            {"path": "docs/security-audit.md", "slug": "security-audit", "label": "安全与合规审计", "severity": "audit"},
            {"path": "THIRD-PARTY-NOTICES.md", "slug": "third-party-notices", "label": "第三方许可证"},
        ],
    },
    {
        "id": "design",
        "label": "设计决策",
        "docs": [
            {"path": "docs/FRONTEND-BASELINE.md", "slug": "frontend-baseline", "label": "前端基准与红线"},
            {"path": "docs/m3-billing-plan.md", "slug": "billing-plan", "label": "M3 配额与计费方案"},
            {"path": "docs/muse-usage-policy.md", "slug": "muse-usage-policy", "label": "muse usage 计费口径"},
            {"path": "docs/pricing-sources.md", "slug": "pricing-sources", "label": "价格表数据来源"},
            {"path": "docs/ux-review.md", "slug": "ux-review", "label": "前端 UX 走查", "severity": "ux"},
            {"path": "docs/ux-fix-triage.md", "slug": "ux-fix-triage", "label": "UX 修复分级"},
            {"path": "docs/design/frontend-integration-plan.md", "slug": "frontend-integration-plan", "label": "前端功能落位计划"},
            {"path": "docs/FRONTEND-SKELETON.md", "slug": "frontend-skeleton", "label": "前端骨架", "archived": True},
            {"path": "docs/design/README.md", "slug": "design-notes", "label": "设计落地说明", "archived": True},
            {"path": "docs/design/ia-proposal.md", "slug": "ia-proposal", "label": "信息架构提案", "archived": True},
        ],
    },
]

BADGES = {
    "audit": {
        "critical": ("Critical · 严重", "sev-critical"),
        "high": ("High · 高", "sev-high"),
        "medium": ("Medium · 中", "sev-medium"),
        "low": ("Low · 低", "sev-low"),
    },
    "ux": {
        "blocker": ("Blocker · 阻断", "sev-critical"),
        "major": ("Major · 主要", "sev-high"),
        "minor": ("Minor · 轻微", "sev-low"),
    },
}

LEGEND_LABELS = {
    "audit": "严重度（原文表格中的等级，站点用色块 + 文字标签呈现）",
    "ux": "问题等级（blocker 为阻断级；色块仅作辅助，以文字标签为准）",
}


def badge(text: str, cls: str) -> str:
    return f'<span class="badge {cls}">{html.escape(text)}</span>'


def strip_md(value: str) -> str:
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", value)
    value = value.replace("`", "").replace("**", "").replace("~~", "")
    value = re.sub(r"(?<![\w*])\*(?!\*)([^*]+)\*", r"\1", value)
    return value.strip()


class Markdown:
    """Small GFM-ish renderer, scoped to the constructs this repo actually uses."""

    FENCE = re.compile(r"^( {0,3})(`{3,}|~{3,})\s*([^`\s]*)")
    HEADING = re.compile(r"^( {0,3})(#{1,6})\s+(.*?)\s*#*\s*$")
    HR = re.compile(r"^ {0,3}([-*_])(?:\s*\1){2,}\s*$")
    LIST = re.compile(r"^( *)([-+*]|\d{1,9}[.)])\s+(.*)$")
    DELIM_CELL = re.compile(r"^\s*:?-{1,}:?\s*$")

    def __init__(
        self,
        repo_root: Path,
        doc_path: str,
        slug: str,
        docs_by_path: dict,
        severity: str | None,
        base_dir: Path,
    ):
        self.repo_root = repo_root
        self.doc_path = doc_path
        self.doc_dir = posixpath.dirname(doc_path)
        self.slug = slug
        self.docs_by_path = docs_by_path
        self.severity = severity
        self.base_dir = base_dir
        self.headings: list[tuple[int, str, str]] = []
        self.warnings: list[str] = []
        self.used_ids: dict[str, int] = {}

    # -- public ------------------------------------------------------------
    def render(self, lines: list[str]) -> str:
        return "\n".join(self.blocks(lines))

    # -- helpers -----------------------------------------------------------
    def heading_id(self, text: str) -> str:
        base = re.sub(r"[^\w\u4e00-\u9fff\s-]+", "", strip_md(text)).strip().lower()
        base = re.sub(r"\s+", "-", base) or "section"
        count = self.used_ids.get(base, 0) + 1
        self.used_ids[base] = count
        return f"doc-{self.slug}-{base}" + (f"-{count}" if count > 1 else "")

    def is_table_start(self, lines: list[str], i: int) -> bool:
        if i + 1 >= len(lines) or "|" not in lines[i]:
            return False
        cells = split_row(lines[i + 1])
        return bool(cells) and all(self.DELIM_CELL.match(c) for c in cells)

    def starts_block(self, lines: list[str], i: int) -> bool:
        line = lines[i]
        if not line.strip():
            return True
        if self.FENCE.match(line) or self.HEADING.match(line) or self.HR.match(line):
            return True
        if line.lstrip().startswith(">"):
            return True
        if self.LIST.match(line):
            return True
        return self.is_table_start(lines, i)

    # -- block level -------------------------------------------------------
    def blocks(self, lines: list[str]) -> list[str]:
        out: list[str] = []
        i = 0
        n = len(lines)
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue

            fence = self.FENCE.match(line)
            if fence:
                i = self.fenced(lines, i, fence, out)
                continue

            heading = self.HEADING.match(line)
            if heading:
                out.append(self.render_heading(heading))
                i += 1
                continue

            if self.HR.match(line):
                out.append("<hr>")
                i += 1
                continue

            if line.lstrip().startswith(">"):
                i = self.blockquote(lines, i, out)
                continue

            if self.is_table_start(lines, i):
                i = self.table(lines, i, out)
                continue

            if self.LIST.match(line):
                i = self.list_block(lines, i, out)
                continue

            i = self.paragraph(lines, i, out)
        return out

    def fenced(self, lines, i, fence, out):
        marker = fence.group(2)
        lang = fence.group(3)
        body: list[str] = []
        i += 1
        while i < len(lines):
            if re.match(r"^ {0,3}" + re.escape(marker[0]) + "{" + str(len(marker)) + r",}\s*$", lines[i]):
                i += 1
                break
            body.append(lines[i])
            i += 1
        code = html.escape("\n".join(body), quote=False)
        attr = f' data-lang="{html.escape(lang)}"' if lang else ""
        out.append(f"<pre{attr}><code>{code}</code></pre>")
        return i

    def render_heading(self, match) -> str:
        level = len(match.group(2))
        text = match.group(3)
        plain = strip_md(text)
        hid = self.heading_id(text)
        if level >= 2:
            self.headings.append((level, plain, hid))
        label = self.inline(text)
        badge_html = ""
        if self.severity:
            key = plain.lower().strip()
            if key in BADGES[self.severity]:
                label, cls = BADGES[self.severity][key]
                badge_html = " " + badge(label, cls)
        return f'<h{level} id="{html.escape(hid)}">{label}{badge_html}</h{level}>'

    def blockquote(self, lines, i, out):
        body: list[str] = []
        while i < len(lines):
            stripped = lines[i].lstrip()
            if stripped.startswith(">"):
                body.append(re.sub(r"^> ?", "", stripped, count=1))
                i += 1
                continue
            if not stripped:
                break
            body.append(lines[i])
            i += 1
        inner = "\n".join(self.blocks(body))
        out.append(f"<blockquote>{inner}</blockquote>")
        return i

    def paragraph(self, lines, i, out):
        parts: list[str] = []
        while i < len(lines) and not self.starts_block(lines, i):
            parts.append(lines[i].strip())
            i += 1
        text = " ".join(parts)
        out.append(f"<p>{self.inline(text)}</p>")
        return i

    def list_block(self, lines, i, out):
        first = self.LIST.match(lines[i])
        base = len(first.group(1))
        ordered = first.group(2)[0].isdigit()
        start_no = int(re.match(r"\d+", first.group(2)).group(0)) if ordered else 1
        item_lines: list[str] | None = None
        items: list[list[str]] = []
        content_indent = base + len(first.group(2)) + 1

        while i < len(lines):
            line = lines[i]
            if not line.strip():
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and indent_of(lines[j]) > base and item_lines is not None:
                    item_lines.append("")
                    i += 1
                    continue
                break

            match = self.LIST.match(line)
            if match and len(match.group(1)) == base and match.group(2)[0].isdigit() == ordered:
                if item_lines is not None:
                    items.append(item_lines)
                content_indent = line.index(match.group(3))
                item_lines = [match.group(3)]
                i += 1
                continue

            if item_lines is not None and indent_of(line) > base:
                cut = min(content_indent, indent_of(line))
                item_lines.append(line[cut:])
                i += 1
                continue

            break

        if item_lines is not None:
            items.append(item_lines)

        tag = "ol" if ordered else "ul"
        attr = f' start="{start_no}"' if ordered and start_no != 1 else ""
        rendered: list[str] = []
        for raw in items:
            body = raw
            task = ""
            if body and re.match(r"^\[[ xX]\]\s+", body[0]):
                checked = body[0][1].lower() == "x"
                task = f'<input type="checkbox" disabled{" checked" if checked else ""}> '
                body = [re.sub(r"^\[[ xX]\]\s+", "", body[0])] + body[1:]
            content = "\n".join(self.blocks(body))
            if task:
                content = task + content
            rendered.append(f"<li>{content}</li>")
        out.append(f"<{tag}{attr}>\n" + "\n".join(rendered) + f"\n</{tag}>")
        return i

    def table(self, lines, i, out):
        header = split_row(lines[i])
        delim = split_row(lines[i + 1])
        aligns = []
        for cell in delim:
            cell = cell.strip()
            if cell.startswith(":") and cell.endswith(":"):
                aligns.append("center")
            elif cell.endswith(":"):
                aligns.append("right")
            else:
                aligns.append("left")
        i += 2
        rows: list[list[str]] = []
        while i < len(lines) and lines[i].strip() and "|" in lines[i]:
            if self.HEADING.match(lines[i]) or self.FENCE.match(lines[i]):
                break
            rows.append(split_row(lines[i]))
            i += 1

        width = len(header)
        head_cells = "".join(
            f'<th style="text-align:{aligns[idx] if idx < len(aligns) else "left"}">{self.cell(cell)}</th>'
            for idx, cell in enumerate(header[:width])
        )
        body_rows = []
        for row in rows:
            cells = (row + [""] * width)[:width]
            body_rows.append(
                "<tr>" + "".join(
                    f'<td style="text-align:{aligns[idx] if idx < len(aligns) else "left"}">{self.cell(cell)}</td>'
                    for idx, cell in enumerate(cells)
                ) + "</tr>"
            )
        out.append(
            '<div class="table-wrap"><table><thead><tr>' + head_cells + "</tr></thead><tbody>"
            + "".join(body_rows) + "</tbody></table></div>"
        )
        return i

    def cell(self, raw: str) -> str:
        raw = raw.strip()
        if self.severity:
            key = strip_md(raw).strip().lower()
            if key in BADGES[self.severity]:
                label, cls = BADGES[self.severity][key]
                return badge(label, cls)
        return self.inline(raw)

    # -- inline level ------------------------------------------------------
    def inline(self, text: str) -> str:
        codes: list[str] = []

        def stash(match):
            codes.append(match.group(2))
            return f"\x00C{len(codes) - 1}\x00"

        text = re.sub(r"(`+)(.+?)\1", stash, text, flags=re.S)
        out = html.escape(text, quote=False)
        out = self.images(out)
        out = self.links(out)
        out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out, flags=re.S)
        out = re.sub(r"(?<![\w*])\*([^\s*][^*\n]*?)\*(?!\*)", r"<em>\1</em>", out)
        out = re.sub(r"~~(.+?)~~", r"<del>\1</del>", out)
        for idx, code in enumerate(codes):
            out = out.replace(f"\x00C{idx}\x00", f"<code>{html.escape(code, quote=False)}</code>")
        return out

    def images(self, text: str) -> str:
        result: list[str] = []
        i = 0
        while i < len(text):
            if text.startswith("![", i):
                label, dest, end = parse_bracket_link(text, i + 1)
                if label is not None:
                    src = self.resolve(dest)
                    alt = strip_md(label)
                    result.append(f'<img src="{src}" alt="{html.escape(alt)}" loading="lazy">')
                    i = end
                    continue
            result.append(text[i])
            i += 1
        return "".join(result)

    def links(self, text: str) -> str:
        result: list[str] = []
        i = 0
        while i < len(text):
            if text[i] == "[":
                label, dest, end = parse_bracket_link(text, i)
                if label is not None:
                    href = self.resolve(dest)
                    result.append(f'<a href="{href}">{self.inline(label)}</a>')
                    i = end
                    continue
            result.append(text[i])
            i += 1
        return "".join(result)

    def resolve(self, dest: str) -> str:
        dest = dest.strip()
        if not dest:
            return "#"
        if re.match(r"^(https?:|mailto:|data:)", dest):
            return dest.replace('"', "%22")
        if dest.startswith("file://"):
            raw, _, frag = dest[len("file://"):].partition("#")
            local = urllib.parse.unquote(raw)
            # file:///C:/... 在 Windows 上是「带前导斜杠的盘符」，Path 会当成无盘符的根路径
            if re.match(r"^/[A-Za-z]:", local):
                local = local[1:]
            target_file = Path(local)
            if not target_file.exists():
                self.warnings.append(f"broken link in {self.doc_path}: {dest}")
            return self.local_link(target_file, frag, dest)
        path, _, frag = dest.partition("#")
        if path == "":
            return f"#doc-{self.slug}"

        target = posixpath.normpath(posixpath.join(self.doc_dir, urllib.parse.unquote(path)))
        if target.lower().endswith(".md"):
            if target in self.docs_by_path:
                return f"#doc-{self.docs_by_path[target]}"
            abs_md = self.repo_root / target
            if not abs_md.exists():
                self.warnings.append(f"broken md link in {self.doc_path}: {dest}")
            return self.local_link(abs_md, frag, dest)

        abs_path = self.repo_root / target
        if not abs_path.exists():
            self.warnings.append(f"broken link in {self.doc_path}: {dest}")
        return self.local_link(abs_path, frag, dest)

    def local_link(self, path: Path, frag: str, original: str) -> str:
        """仓内文件一律输出相对链接，生成物不能带本机绝对路径。"""
        href = repo_relative_url(path, self.repo_root, self.base_dir)
        if href is None:
            self.warnings.append(f"link outside repo in {self.doc_path}: {original}")
            return "#"
        return href + (f"#{frag}" if frag else "")


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def split_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|") and not text.endswith("\\|"):
        text = text[:-1]
    cells: list[str] = []
    buf = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text) and text[i + 1] == "|":
            buf += "|"
            i += 2
            continue
        if ch == "|":
            cells.append(buf.strip())
            buf = ""
            i += 1
            continue
        buf += ch
        i += 1
    cells.append(buf.strip())
    return cells


def parse_bracket_link(text: str, start: int):
    """Parse `[label](dest "title")` starting at `[`. Returns (label, dest, end)."""
    if start >= len(text) or text[start] != "[":
        return None, "", start
    depth = 0
    close = -1
    for idx in range(start, len(text)):
        if text[idx] == "[":
            depth += 1
        elif text[idx] == "]":
            depth -= 1
            if depth == 0:
                close = idx
                break
    if close < 0 or close + 1 >= len(text) or text[close + 1] != "(":
        return None, "", start
    depth = 1
    end = -1
    for idx in range(close + 2, len(text)):
        if text[idx] == "(":
            depth += 1
        elif text[idx] == ")":
            depth -= 1
            if depth == 0:
                end = idx
                break
    if end < 0:
        return None, "", start
    dest = text[close + 2:end].strip()
    if dest.startswith("<") and dest.endswith(">"):
        dest = dest[1:-1]
    title = re.search(r'\s+"[^"]*"$', dest)
    if title:
        dest = dest[:title.start()].strip()
    return text[start + 1:close], dest, end + 1


def repo_relative_url(path: Path, repo_root: Path, base_dir: Path) -> str | None:
    """把仓内路径转成相对站点入口的文件链接；仓外路径返回 None。"""
    try:
        target = path.resolve()
    except OSError:
        return None
    root = repo_root.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    try:
        rel = os.path.relpath(target, base_dir.resolve())
    except ValueError:  # Windows 跨盘符无法求相对路径
        return None
    return urllib.parse.quote(Path(rel).as_posix(), safe="/:@")


def build(repo_root: Path, out_path: Path) -> int:
    docs: list[dict] = []
    docs_by_path: dict[str, str] = {}
    for group in GROUPS:
        for entry in group["docs"]:
            docs_by_path[entry["path"]] = entry["slug"]

    warnings: list[str] = []
    articles: list[str] = []
    nav_groups: list[str] = []

    for group in GROUPS:
        items = []
        for entry in group["docs"]:
            src = repo_root / entry["path"]
            if not src.exists():
                print(f"[build] ERROR missing file: {entry['path']}", file=sys.stderr)
                return 1
            raw = src.read_text(encoding="utf-8")
            lines = raw.splitlines()
            title = entry["label"]
            body_start = 0
            for idx, line in enumerate(lines):
                if line.startswith("# "):
                    title = strip_md(line[2:])
                    body_start = idx + 1
                    break

            renderer = Markdown(
                repo_root,
                entry["path"],
                entry["slug"],
                docs_by_path,
                entry.get("severity"),
                out_path.resolve().parent,
            )
            body_html = renderer.render(lines[body_start:])
            warnings.extend(renderer.warnings)

            toc = "".join(
                f'<li class="toc-{lvl}"><a href="#{hid}">{html.escape(text)}</a></li>'
                for lvl, text, hid in renderer.headings if lvl <= 3
            )
            legend = ""
            if entry.get("severity"):
                chips = "".join(badge(label, cls) for label, cls in BADGES[entry["severity"]].values())
                legend = (
                    '<div class="legend"><span class="legend-label">'
                    + html.escape(LEGEND_LABELS[entry["severity"]])
                    + "</span>" + chips + "</div>"
                )

            archived = '<span class="nav-tag">存档</span>' if entry.get("archived") else ""
            meta_bits = [html.escape(group["label"]), f"源文件 <code>{html.escape(entry['path'])}</code>"]
            articles.append(
                f'<article class="doc" id="doc-{entry["slug"]}" data-doc="{entry["slug"]}" '
                f'data-group="{group["id"]}" hidden>\n'
                f'<header class="doc-head"><div class="kicker">{" · ".join(meta_bits)}'
                + (' · <span class="nav-tag">存档：已归档、未采纳</span>' if entry.get("archived") else "")
                + f'</div><h1>{html.escape(title)}</h1>'
                f'<div class="doc-facts">{len(lines)} 行 Markdown · {len(renderer.headings)} 个小节</div>'
                f"</header>\n"
                + (f'<details class="toc" open><summary>本页目录</summary><ul>{toc}</ul></details>' if toc else "")
                + legend
                + f'<div class="doc-body">\n{body_html}\n</div>\n'
                + f'<footer class="doc-footer">本页由 <code>docs/site/build.py</code> 从 '
                f'<code>{html.escape(entry["path"])}</code> 生成；要改内容请编辑 Markdown 后重建。</footer>\n'
                f"</article>"
            )

            items.append(
                f'<a class="nav-item" href="#doc-{entry["slug"]}" data-doc="{entry["slug"]}">'
                f'<span class="nav-title">{html.escape(entry["label"])}</span>'
                + ('<span class="nav-tag">存档</span>' if entry.get("archived") else "")
                + '<span class="hits" hidden></span></a>'
            )
            docs.append({
                "slug": entry["slug"],
                "label": entry["label"],
                "title": title,
                "group": group["id"],
                "path": entry["path"],
                "archived": bool(entry.get("archived")),
            })

        nav_groups.append(
            f'<section class="nav-group" data-group="{group["id"]}">'
            f'<h2>{html.escape(group["label"])}<span class="nav-count">{len(group["docs"])}</span></h2>'
            + "".join(items) + "</section>"
        )

    manifest_paths = {entry["path"] for group in GROUPS for entry in group["docs"]}
    for md in sorted(repo_root.rglob("*.md")):
        rel = md.relative_to(repo_root).as_posix()
        if rel.startswith(("apps/", "providers/", "node_modules/", ".runtime/", "docs/site/", "docs/ux-shots/")):
            continue
        if rel not in manifest_paths:
            warnings.append(f"markdown not in site manifest: {rel}")

    meta = {
        "docs": docs,
        "groups": [{"id": g["id"], "label": g["label"], "count": len(g["docs"])} for g in GROUPS],
        "built": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

    page = (
        HTML_TEMPLATE.replace("__DOC_META__", json.dumps(meta, ensure_ascii=False))
        .replace("__NAV__", "\n".join(nav_groups))
        .replace("__ARTICLES__", "\n".join(articles))
        .replace("__COUNT__", str(len(docs)))
        .replace("__GROUPS__", str(len(GROUPS)))
        .replace("__BUILT__", meta["built"])
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(page, encoding="utf-8")

    print(f"[build] {len(docs)} documents / {len(GROUPS)} groups")
    try:
        shown = out_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        shown = str(out_path)
    print(f"[build] wrote {shown} ({out_path.stat().st_size / 1024:.1f} KB)")
    if warnings:
        seen = []
        for item in warnings:
            if item not in seen:
                seen.append(item)
        print(f"[build] {len(seen)} warning(s):", file=sys.stderr)
        for item in seen:
            print(f"  - {item}", file=sys.stderr)
    return 0


def main(argv: list[str]) -> int:
    # 直接双击/在 cp936 控制台运行时，中文路径与警告不能崩
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Build the offline documentation site.")
    parser.add_argument("--repo-root", default=str(SITE_DIR.parent.parent))
    parser.add_argument("--out", default=str(SITE_DIR / "index.html"))
    args = parser.parse_args(argv)
    return build(Path(args.repo_root).resolve(), Path(args.out))


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>unified-2api 文档</title>
<style>
:root{
  --paper:#f7f7f5;--surface:#fff;--ink:#22252a;--muted:#5f646b;
  --line:#e2e2dd;--line-strong:#cfcfc9;--accent:#1f6f5c;--accent-soft:#eef4f1;
  --sidebar-w:296px;--tb:57px;
  --sev-critical-fg:#8c2f2f;--sev-critical-bg:#f7e9e8;--sev-critical-bd:#c9908f;
  --sev-high-fg:#8a4a12;--sev-high-bg:#f8ecdc;--sev-high-bd:#d0a878;
  --sev-medium-fg:#66570a;--sev-medium-bg:#f5f1d8;--sev-medium-bd:#c9bc72;
  --sev-low-fg:#4a5560;--sev-low-bg:#eef0f2;--sev-low-bd:#b9c1c9;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
  font:15px/1.75 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Hiragino Sans GB","Microsoft YaHei","Noto Sans CJK SC",sans-serif;
  -webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline;text-underline-offset:2px}
code,pre{font-family:"Cascadia Mono","SFMono-Regular",Consolas,"Liberation Mono",monospace}
.skip{position:absolute;left:-9999px}
.skip:focus{left:12px;top:8px;z-index:99;background:#fff;border:1px solid var(--accent);padding:6px 10px}

/* top bar */
.topbar{position:sticky;top:0;z-index:40;display:flex;align-items:center;gap:18px;
  padding:9px 20px;min-height:57px;background:rgba(247,247,245,.97);
  border-bottom:1px solid var(--line-strong);flex-wrap:wrap}
.brand{color:var(--ink);font-weight:700;font-size:15px;letter-spacing:-.01em;white-space:nowrap;display:flex;align-items:baseline;gap:7px}
.brand em{font-style:normal;font-family:"Cascadia Mono",Consolas,monospace;font-size:10.5px;letter-spacing:.14em;color:var(--muted);text-transform:uppercase}
.brand:hover{text-decoration:none}
.tabs{display:flex;gap:2px;flex:1 1 auto;min-width:0;overflow-x:auto;scrollbar-width:thin}
.tab{flex:0 0 auto;border:0;background:none;color:var(--muted);font:inherit;font-size:13px;
  padding:6px 10px 5px;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap}
.tab:hover{color:var(--ink)}
.tab[aria-selected="true"]{color:var(--ink);border-bottom-color:var(--accent);font-weight:600}
.searchbox{display:flex;align-items:center;gap:2px;border:1px solid var(--line-strong);background:#fff;
  padding:0 6px;flex:0 1 250px;min-width:150px}
.searchbox:focus-within{border-color:var(--accent)}
.searchbox input{border:0;outline:0;background:transparent;font:inherit;font-size:13px;
  padding:7px 4px;width:100%;min-width:0}
.search-clear{border:0;background:none;color:var(--muted);font-size:16px;line-height:1;cursor:pointer;padding:2px 4px}
.menu-btn{display:none;align-items:center;justify-content:center;min-width:34px;height:30px;padding:0 8px;white-space:nowrap;
  border:1px solid var(--line-strong);background:#fff;color:var(--ink);font:600 12px/1 inherit;cursor:pointer}

/* layout */
.layout{display:flex;align-items:flex-start}
.sidebar{position:sticky;top:var(--tb);height:calc(100vh - var(--tb));flex:0 0 var(--sidebar-w);width:var(--sidebar-w);
  overflow-y:auto;border-right:1px solid var(--line);padding:22px 16px 64px;scrollbar-width:thin;background:var(--paper)}
.nav-group{margin:0 0 22px}
.nav-group h2{display:flex;align-items:center;gap:8px;margin:0 0 6px;padding:0 0 0 10px;
  font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);font-weight:600}
.nav-count{font-size:10.5px;letter-spacing:0;color:#8a8f95;border:1px solid var(--line);padding:0 4px;background:#fff}
.nav-item{display:flex;align-items:center;gap:6px;padding:5px 8px 5px 10px;border-left:2px solid transparent;
  color:#3a3f45;font-size:13.5px;line-height:1.45}
.nav-item:hover{background:#efefe9;text-decoration:none}
.nav-item[aria-current="page"]{border-left-color:var(--accent);background:var(--accent-soft);color:#173f36;font-weight:600}
.nav-title{min-width:0}
.nav-tag{flex:0 0 auto;font-size:10px;line-height:1.5;letter-spacing:.05em;color:#6b6f75;
  border:1px solid var(--line-strong);background:#fff;padding:0 4px}
.hits{margin-left:auto;font-size:11px;color:#5b6167;background:#e7e7e1;padding:0 5px;min-width:20px;text-align:center}
.nav-item.is-hidden,.nav-group.is-hidden{display:none}

/* content */
.main{flex:1 1 auto;min-width:0;padding:34px 48px 140px}
.doc{max-width:900px}
.doc-head{border-bottom:1px solid var(--line);padding-bottom:16px;margin-bottom:24px}
.kicker{font-size:11.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.kicker code{text-transform:none;letter-spacing:0;font-size:11.5px}
.doc-head h1{font-size:30px;line-height:1.32;margin:9px 0 8px;letter-spacing:-.01em}
.doc-facts{font-size:12px;color:var(--muted)}
.toc{border:1px solid var(--line);background:#fbfbf8;padding:9px 14px;margin:0 0 24px}
.toc summary{cursor:pointer;font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);font-weight:600}
.toc ul{list-style:none;margin:8px 0 2px;padding-left:0;columns:2;column-gap:34px;font-size:13px}
.toc li{margin:2px 0;break-inside:avoid}
.toc .toc-3{margin-left:10px;font-size:12.5px;color:#4d5257}
.legend{display:flex;flex-wrap:wrap;align-items:center;gap:7px;border-left:3px solid var(--line-strong);
  background:#f2f2ed;padding:9px 12px;margin:0 0 24px;font-size:12px;color:#4d5257}
.legend-label{flex:1 1 100%;margin-bottom:1px;letter-spacing:.02em}

.doc-body{overflow-wrap:anywhere}
.doc-body h2{font-size:21px;line-height:1.4;margin:42px 0 12px;padding-top:18px;border-top:1px solid var(--line);scroll-margin-top:calc(var(--tb) + 16px)}
.doc-body h3{font-size:16.5px;line-height:1.45;margin:28px 0 8px;scroll-margin-top:calc(var(--tb) + 16px)}
.doc-body h4{font-size:14.5px;margin:20px 0 6px;color:#33383d}
.doc-body h2:first-child,.doc-body h3:first-child{margin-top:0;padding-top:0;border-top:0}
.doc-body p{margin:0 0 13px}
.doc-body ul,.doc-body ol{margin:0 0 15px;padding-left:22px}
.doc-body li{margin:3px 0}
.doc-body li>ul,.doc-body li>ol{margin:3px 0 3px}
.doc-body hr{border:0;border-top:1px solid var(--line);margin:28px 0}
.doc-body blockquote{margin:0 0 16px;padding:2px 0 2px 14px;border-left:2px solid var(--accent);
  color:#4a4f55;background:#f2f5f2}
.doc-body blockquote p{margin:6px 0}
.doc-body :not(pre)>code{background:#efefe9;border:1px solid #e4e4de;padding:1px 4px;font-size:.88em;overflow-wrap:anywhere}
.doc-body pre{margin:0 0 16px;padding:13px 15px;background:#f2f2ed;border-left:3px solid var(--line-strong);
  overflow-x:auto;font-size:12.5px;line-height:1.62;tab-size:4}
.doc-body pre code{background:none;border:0;padding:0;font-size:inherit;white-space:pre}
.doc-body img{max-width:100%;height:auto;border:1px solid var(--line)}
.table-wrap{margin:0 0 18px;overflow-x:auto;border:1px solid var(--line);background:var(--surface)}
.doc-body table{border-collapse:collapse;width:100%;font-size:13px;line-height:1.6}
.doc-body th{background:#f1f1ec;text-align:left;font-weight:600;padding:8px 10px;border-bottom:1px solid var(--line-strong);vertical-align:bottom}
.doc-body td{padding:8px 10px;border-top:1px solid var(--line);vertical-align:top}
.doc-body tr:first-child td{border-top:0}
.doc-body input[type="checkbox"]{margin-right:5px}
.doc-footer{margin-top:40px;padding-top:14px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
.doc-footer code{font-size:11.5px}

.badge{display:inline-block;border:1px solid;padding:0 6px;margin:0 4px 2px 0;
  font-size:10.5px;line-height:1.75;letter-spacing:.02em;font-weight:600;white-space:nowrap}
.sev-critical{color:var(--sev-critical-fg);background:var(--sev-critical-bg);border-color:var(--sev-critical-bd)}
.sev-high{color:var(--sev-high-fg);background:var(--sev-high-bg);border-color:var(--sev-high-bd)}
.sev-medium{color:var(--sev-medium-fg);background:var(--sev-medium-bg);border-color:var(--sev-medium-bd)}
.sev-low{color:var(--sev-low-fg);background:var(--sev-low-bg);border-color:var(--sev-low-bd)}
mark.search-hit{background:#ffe9a8;color:inherit;padding:0}
.is-hidden{display:none !important}
.search-note{margin:0 0 18px;padding:8px 12px;border-left:3px solid var(--accent);background:var(--accent-soft);
  font-size:13px;color:#2b4a41}
.search-note[hidden]{display:none}

.site-foot{max-width:900px;margin:60px 0 0;padding-top:16px;border-top:1px solid var(--line);font-size:12px;color:var(--muted)}
.overlay{display:none}

@media (max-width:1120px){
  :root{--sidebar-w:260px}
  .main{padding:30px 32px 120px}
  .doc-head h1{font-size:27px}
}
@media (max-width:900px){
  .layout{display:block}
  .menu-btn{display:inline-flex}
  .sidebar{position:fixed;left:0;top:var(--tb);bottom:0;height:auto;width:300px;max-width:86vw;z-index:35;
    border-right:1px solid var(--line-strong);transform:translateX(-100%);transition:transform .18s ease;
    background:var(--paper);box-shadow:1px 0 0 rgba(0,0,0,.04)}
  body.drawer-open .sidebar{transform:none}
  body.drawer-open .overlay{display:block;position:fixed;inset:var(--tb) 0 0;background:rgba(34,37,42,.18);z-index:30}
  .main{padding:22px 20px 90px}
  .doc-head h1{font-size:23px}
  .toc ol{columns:1}
  .searchbox{flex:1 1 180px;order:2}
  .tabs{order:3;flex:1 1 100%;border-top:1px solid var(--line);margin-top:2px}
  .topbar{gap:10px;padding:8px 14px}
  .doc-body h2{font-size:19px}
  .doc-body h3{font-size:15.5px}
  .doc-body table{font-size:12.5px}
}
@media (max-width:520px){
  .brand em{display:none}
  .searchbox{flex:1 1 100%;order:3}
  .tabs{order:4}
}
</style>
</head>
<body>
<a class="skip" href="#content">跳到正文</a>
<header class="topbar" id="topbar">
  <button class="menu-btn" id="menuBtn" aria-expanded="false" aria-controls="sidebar">目录</button>
  <a class="brand" href="#doc-overview">unified-2api <em>docs</em></a>
  <nav class="tabs" id="tabs" aria-label="文档分组"></nav>
  <div class="searchbox">
    <input id="search" type="search" placeholder="搜索目录与正文…" aria-label="站内搜索" autocomplete="off" spellcheck="false">
    <button class="search-clear" id="searchClear" aria-label="清除搜索" hidden>×</button>
  </div>
</header>
<div class="overlay" id="overlay"></div>
<div class="layout">
  <aside class="sidebar" id="sidebar" aria-label="文档目录">
    __NAV__
  </aside>
  <main class="main" id="content">
    <div class="search-note" id="searchNote" role="status" hidden></div>
    __ARTICLES__
    <div class="site-foot">
      离线文档站 · 共 __COUNT__ 篇 / __GROUPS__ 组 · 生成于 __BUILT__ ·
      由 <code>docs/site/build.py</code> 从仓库 Markdown 生成，不联网、不依赖 CDN。
    </div>
  </main>
</div>
<script>
const META = __DOC_META__;
(function () {
  "use strict";
  const articles = Array.from(document.querySelectorAll("article.doc"));
  const navItems = Array.from(document.querySelectorAll(".nav-item"));
  const navMap = new Map(navItems.map(a => [a.dataset.doc, a]));
  const tabBar = document.getElementById("tabs");
  const searchInput = document.getElementById("search");
  const searchClear = document.getElementById("searchClear");
  const note = document.getElementById("searchNote");
  const topbar = document.getElementById("topbar");
  const sidebar = document.getElementById("sidebar");
  const menuBtn = document.getElementById("menuBtn");
  const overlay = document.getElementById("overlay");
  const groups = META.groups;
  let activeDoc = null;
  let currentGroup = "all";
  let query = "";
  let index = null;
  let allMarks = [];

  function setTopbarHeight() {
    document.documentElement.style.setProperty("--tb", topbar.offsetHeight + "px");
  }
  setTopbarHeight();
  window.addEventListener("resize", setTopbarHeight);

  // ---- group tabs -------------------------------------------------------
  const allBtn = makeTab("all", "全部");
  tabBar.appendChild(allBtn);
  groups.forEach(g => tabBar.appendChild(makeTab(g.id, g.label)));
  function makeTab(id, label) {
    const b = document.createElement("button");
    b.className = "tab";
    b.dataset.group = id;
    b.textContent = label;
    b.setAttribute("aria-selected", id === "all" ? "true" : "false");
    b.addEventListener("click", () => {
      currentGroup = id;
      Array.from(tabBar.children).forEach(t => t.setAttribute("aria-selected", t === b ? "true" : "false"));
      if (query) { ensureIndex(); }
      applyNavFilter();
      if (id !== "all") {
        const first = META.docs.find(d => d.group === id);
        const active = META.docs.find(d => d.slug === activeDoc);
        if (first && (!active || active.group !== id)) showDoc(first.slug, null, true);
        else scrollSidebarTo(id);
      }
    });
    return b;
  }

  function scrollSidebarTo(groupId) {
    const el = sidebar.querySelector('.nav-group[data-group="' + groupId + '"]');
    if (el) el.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  // ---- hash routing -----------------------------------------------------
  function parseHash() {
    const raw = decodeURIComponent(location.hash.replace(/^#/, ""));
    if (!raw) return null;
    let best = null;
    META.docs.forEach(d => {
      const id = "doc-" + d.slug;
      if ((raw === id || raw.indexOf(id + "-") === 0) && (!best || id.length > best.id.length)) {
        best = { slug: d.slug, anchor: raw, id: id };
      }
    });
    return best;
  }

  function revealNavItem(item) {
    const sr = sidebar.getBoundingClientRect();
    const r = item.getBoundingClientRect();
    if (r.top < sr.top + 8) sidebar.scrollTop -= (sr.top + 8 - r.top);
    else if (r.bottom > sr.bottom - 8) sidebar.scrollTop += (r.bottom - sr.bottom + 8);
  }

  function showDoc(slug, anchor, pushHash) {
    const article = document.getElementById("doc-" + slug);
    if (!article) return;
    articles.forEach(a => { a.hidden = a !== article; });
    navItems.forEach(a => {
      if (a.dataset.doc === slug) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    activeDoc = slug;
    const activeNav = navMap.get(slug);
    if (activeNav && window.innerWidth > 900) revealNavItem(activeNav);
    document.title = (article.querySelector("h1") || {}).textContent
      ? article.querySelector("h1").textContent + " · unified-2api 文档" : "unified-2api 文档";
    if (query) {
      const hits = filterArticle(article, query);
      updateStatus(hits, countMatchingDocs());
    }
    const target = (anchor && anchor !== "doc-" + slug) ? document.getElementById(anchor) : null;
    const applyScroll = () => {
      if (target) target.scrollIntoView({ block: "start" });
      else window.scrollTo(0, 0);
    };
    applyScroll();
    // 浏览器在 hashchange 之后还会按锚点自行滚动一次，这里再校正一帧
    requestAnimationFrame(applyScroll);
    if (pushHash) {
      history.replaceState(null, "", "#doc-" + slug);
    }
    closeDrawer();
  }

  function handleHash() {
    const hit = parseHash();
    if (hit) showDoc(hit.slug, hit.anchor, false);
    else showDoc(META.docs[0].slug, null, false);
  }
  window.addEventListener("hashchange", handleHash);

  navItems.forEach(a => {
    a.addEventListener("click", e => {
      const slug = a.dataset.doc;
      if (slug === activeDoc && !location.hash.startsWith("#doc-" + slug)) {
        e.preventDefault();
        location.hash = "#doc-" + slug;
      } else if (slug === activeDoc) {
        const hit = parseHash();
        if (hit && hit.anchor === "doc-" + slug) window.scrollTo(0, 0);
      }
    });
  });

  // ---- search -----------------------------------------------------------
  function ensureIndex() {
    if (index) return;
    index = {};
    META.docs.forEach(d => {
      const el = document.getElementById("doc-" + d.slug);
      index[d.slug] = (el.textContent || "").toLowerCase();
    });
  }

  function countMatchingDocs() {
    if (!index) return 0;
    const needle = query.toLowerCase();
    let n = 0;
    META.docs.forEach(d => { if (index[d.slug] && index[d.slug].includes(needle)) n += 1; });
    return n;
  }

  function countOcc(hay, needle) {
    if (!needle) return 0;
    let n = 0, at = 0;
    while (true) {
      const pos = hay.indexOf(needle, at);
      if (pos < 0) break;
      n += 1;
      at = pos + needle.length;
    }
    return n;
  }

  function markMatches(root, needle) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        if (!node.nodeValue || !node.nodeValue.toLowerCase().includes(needle)) return NodeFilter.FILTER_REJECT;
        if (node.parentElement && node.parentElement.closest("mark.search-hit")) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    const targets = [];
    while (walker.nextNode()) targets.push(walker.currentNode);
    targets.forEach(node => {
      const text = node.nodeValue;
      const lower = text.toLowerCase();
      let from = 0;
      const frag = document.createDocumentFragment();
      while (true) {
        const at = lower.indexOf(needle, from);
        if (at < 0) break;
        if (at > from) frag.appendChild(document.createTextNode(text.slice(from, at)));
        const mk = document.createElement("mark");
        mk.className = "search-hit";
        mk.textContent = text.slice(at, at + needle.length);
        frag.appendChild(mk);
        allMarks.push(mk);
        from = at + needle.length;
      }
      if (from < text.length) frag.appendChild(document.createTextNode(text.slice(from)));
      node.parentNode.replaceChild(frag, node);
    });
  }

  function clearMarks() {
    allMarks.forEach(m => {
      if (m.parentNode) m.parentNode.replaceChild(document.createTextNode(m.textContent), m);
    });
    allMarks = [];
    articles.forEach(a => a.normalize());
  }

  function filterArticle(article, needle) {
    clearMarks();
    const blocks = Array.from(article.querySelectorAll(".doc-body > *"));
    let total = 0;
    blocks.forEach(el => {
      el.classList.remove("is-hidden");
      if (el.tagName === "UL" || el.tagName === "OL") {
        let hits = 0;
        Array.from(el.children).forEach(li => {
          const n = countOcc(li.textContent.toLowerCase(), needle);
          li.classList.toggle("is-hidden", n === 0);
          if (n) { hits += n; markMatches(li, needle); }
        });
        el.classList.toggle("is-hidden", hits === 0);
        total += hits;
      } else if (el.classList.contains("table-wrap")) {
        let hits = 0;
        el.querySelectorAll("tbody tr").forEach(tr => {
          const n = countOcc(tr.textContent.toLowerCase(), needle);
          tr.classList.toggle("is-hidden", n === 0);
          if (n) hits += n;
        });
        el.querySelectorAll("thead tr, tbody tr:not(.is-hidden)").forEach(tr => markMatches(tr, needle));
        el.classList.toggle("is-hidden", hits === 0);
        total += hits;
      } else {
        const n = countOcc(el.textContent.toLowerCase(), needle);
        el.classList.toggle("is-hidden", n === 0);
        if (n) { total += n; markMatches(el, needle); }
      }
    });
    return total;
  }

  function applyNavFilter() {
    const needle = query.toLowerCase();
    let docHits = 0;
    groups.forEach(g => {
      const section = sidebar.querySelector('.nav-group[data-group="' + g.id + '"]');
      let shown = 0;
      META.docs.filter(d => d.group === g.id).forEach(d => {
        const item = navMap.get(d.slug);
        const hits = needle ? countOcc(index[d.slug], needle) : 0;
        const groupOk = currentGroup === "all" || currentGroup === g.id;
        const searchOk = !needle || hits > 0;
        const visible = groupOk && searchOk;
        item.classList.toggle("is-hidden", !visible);
        const counter = item.querySelector(".hits");
        counter.hidden = !hits;
        counter.textContent = hits ? String(hits) : "";
        if (visible) shown += 1;
        if (hits) docHits += 1;
      });
      if (section) {
        section.classList.toggle("is-hidden", shown === 0);
        const cnt = section.querySelector(".nav-count");
        if (cnt) cnt.textContent = String(shown);
      }
    });
    return docHits;
  }

  function updateStatus(currentHits, docHits) {
    if (!query) { note.hidden = true; return; }
    note.hidden = false;
    if (currentHits > 0) {
      note.textContent = "搜索「" + query + "」：本篇 " + currentHits + " 处匹配，全站 " + docHits + " 篇文档命中。";
    } else {
      note.textContent = "搜索「" + query + "」：本篇无匹配，全站 " + docHits + " 篇文档命中；可在左侧目录中选择命中项。";
    }
  }

  function runSearch() {
    query = searchInput.value.trim();
    searchClear.hidden = !query;
    document.body.classList.toggle("searching", !!query);
    if (!query) {
      clearMarks();
      applyNavFilter();
      updateStatus(0, 0);
      articles.forEach(a => a.querySelectorAll(".is-hidden").forEach(el => {
        if (el.closest(".doc-body") || el.classList.contains("table-wrap")) el.classList.remove("is-hidden");
      }));
      return;
    }
    ensureIndex();
    const docHits = applyNavFilter();
    const article = document.getElementById("doc-" + activeDoc);
    const currentHits = article ? filterArticle(article, query.toLowerCase()) : 0;
    updateStatus(currentHits, docHits);
  }

  let searchTimer = null;
  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(runSearch, 120);
  });
  searchInput.addEventListener("keydown", e => {
    if (e.key === "Enter") {
      e.preventDefault();
      if (!query) return;
      ensureIndex();
      const article = document.getElementById("doc-" + activeDoc);
      const mine = article ? countOcc(article.textContent.toLowerCase(), query.toLowerCase()) : 0;
      if (mine > 0) return;
      const hit = META.docs.find(d => index[d.slug].includes(query.toLowerCase()));
      if (hit) location.hash = "#doc-" + hit.slug;
    }
    if (e.key === "Escape") {
      searchInput.value = "";
      runSearch();
      searchInput.blur();
    }
  });
  searchClear.addEventListener("click", () => {
    searchInput.value = "";
    runSearch();
    searchInput.focus();
  });

  // ---- drawer -----------------------------------------------------------
  function closeDrawer() {
    document.body.classList.remove("drawer-open");
    menuBtn.setAttribute("aria-expanded", "false");
  }
  menuBtn.addEventListener("click", () => {
    const open = document.body.classList.toggle("drawer-open");
    menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  overlay.addEventListener("click", closeDrawer);
  window.addEventListener("keydown", e => {
    if (e.key === "Escape") closeDrawer();
  });
  window.addEventListener("resize", () => {
    if (window.innerWidth > 900) closeDrawer();
  });

  // ---- boot -------------------------------------------------------------
  const start = parseHash();
  if (start) showDoc(start.slug, start.anchor, false);
  else showDoc(META.docs[0].slug, null, false);
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
