#!/usr/bin/env python3
"""校验 apps/web 五种语言的键集与源码引用是否对齐。

基准语言：
  - zh-CN：config.ts 的 DEFAULT_LOCALE，缺键时的回退语言，必须拥有全部界面键。
  - en：复数形式（*.one / *.other）的定义语言。

两类「设计性回退」不计为真缺：
  - phrases.*：后端运行时报文表，键是中文源文，zh-CN 本就没有该表，
    translatePhrase 取不到时原样返回源文（lib/i18n/index.ts:124-128）。
  - 复数：en 用 *.one/*.other，其余语言用基础字符串键；比较时归一到基础键，
    取值时 translate 会把对象交给 pluralOf（lib/i18n/index.ts:102-107）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "apps" / "web"
LOCALE_DIR = WEB_ROOT / "lib" / "i18n" / "locales"
CONFIG_FILE = WEB_ROOT / "lib" / "i18n" / "config.ts"

BASELINES = ("zh-CN", "en")
PLURAL_CATEGORIES = frozenset({"zero", "one", "two", "few", "many", "other"})
SKIP_DIRS = frozenset({"node_modules", ".next", "out", "locales"})
KEY_SHAPE = re.compile(r"^[A-Za-z][\w-]*(?:\.[\w-]+)+$")
QUOTED = re.compile(r"""['"]([A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)+)['"]""")
TEMPLATE = re.compile(r"`([^`]*)`")
PLACEHOLDER = re.compile(r"\$\{[^}]*\}")


class CheckError(Exception):
    """脚本自身无法继续（文件缺失、格式异常）。"""


def load_locales() -> list[str]:
    text = CONFIG_FILE.read_text(encoding="utf-8")
    found = re.search(r"LOCALES\s*=\s*\[(.*?)\]", text, re.S)
    if not found:
        raise CheckError(f"无法从 {CONFIG_FILE} 解析 LOCALES 列表")
    locales = re.findall(r"""['"]([^'"]+)['"]""", found.group(1))
    if not locales:
        raise CheckError(f"{CONFIG_FILE} 的 LOCALES 为空")
    return locales


def read_dict(locale: str) -> dict:
    path = LOCALE_DIR / f"{locale}.json"
    if not path.is_file():
        raise CheckError(f"缺少语言文件: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as err:
        raise CheckError(f"{path} 不是合法 JSON: {err}") from err
    if not isinstance(data, dict):
        raise CheckError(f"{path} 顶层必须是对象")
    return data


def is_plural(node: dict) -> bool:
    """只有复数分类构成的节点才算复数组，普通命名空间照常递归。"""
    return bool(node) and set(node) <= PLURAL_CATEGORIES and "other" in node


def flatten(tree: dict) -> dict:
    """展开为「点分路径 -> 叶子」；复数组保留为一个基础键，不做 .one/.other 拆分。"""
    flat: dict = {}

    def walk(node: dict, prefix: str) -> None:
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                if is_plural(value):
                    flat[path] = value
                else:
                    walk(value, path)
            else:
                flat[path] = value

    walk(tree, "")
    return flat


def leaf_count(tree: dict) -> int:
    return sum(
        leaf_count(value) if isinstance(value, dict) else 1
        for value in tree.values()
    )


def strip_comments(code: str) -> str:
    """去掉 // 与 /* */ 注释，保留换行以维持行号；字符串/模板里的内容原样保留。"""
    out: list[str] = []
    index = 0
    length = len(code)
    state = "code"
    while index < length:
        char = code[index]
        pair = code[index:index + 2]
        if state == "code":
            if pair == "//":
                state = "line"
                index += 2
                continue
            if pair == "/*":
                state = "block"
                index += 2
                continue
            out.append(char)
            if char == "'":
                state = "single"
            elif char == '"':
                state = "double"
            elif char == "`":
                state = "template"
            index += 1
        elif state == "line":
            if char == "\n":
                state = "code"
                out.append(char)
            index += 1
        elif state == "block":
            if pair == "*/":
                state = "code"
                index += 2
                continue
            if char == "\n":
                out.append(char)
            index += 1
        else:
            quote = {"single": "'", "double": '"', "template": "`"}[state]
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(code[index + 1])
                index += 2
                continue
            if char == quote:
                state = "code"
            index += 1
    return "".join(out)


def is_property_name(code: str, start: int, end: int) -> bool:
    """形如 {'a.b': ...} 的对象属性名不是翻译键；三元/映射值里的字面量要保留。"""
    before = code[:start].rstrip()[-1:]
    after = code[end:].lstrip()[:1]
    return (before in "{," and after == ":") or (before == "[" and after == "]")


def scan_sources(top_segments: set[str]) -> tuple[int, dict[str, list[str]], list[dict]]:
    """扫描 t("x.y") / translate("x.y") 等字面量键，以及模板化的动态键前缀。"""
    literals: dict[str, list[str]] = {}
    dynamics: list[dict] = []
    files = sorted(
        path
        for path in WEB_ROOT.rglob("*")
        if path.suffix in (".ts", ".tsx")
        and not any(part in SKIP_DIRS for part in path.relative_to(WEB_ROOT).parts)
    )

    def add_literal(key: str, rel: str, line: int) -> None:
        if key.split(".")[0] not in top_segments or key.startswith("phrases."):
            return
        literals.setdefault(key, []).append(f"{rel}:{line}")

    for path in files:
        rel = path.relative_to(REPO_ROOT).as_posix()
        code = strip_comments(path.read_text(encoding="utf-8"))
        for match in QUOTED.finditer(code):
            if is_property_name(code, match.start(), match.end()):
                continue
            add_literal(match.group(1), rel, code.count("\n", 0, match.start()) + 1)

        for match in TEMPLATE.finditer(code):
            raw = match.group(1)
            line = code.count("\n", 0, match.start()) + 1
            if not raw:
                continue
            if "${" not in raw:
                if KEY_SHAPE.match(raw) and not is_property_name(code, match.start(), match.end()):
                    add_literal(raw, rel, line)
                continue
            if raw.split(".")[0] not in top_segments or raw.startswith("phrases."):
                continue
            parts = PLACEHOLDER.split(raw)
            if not parts or not parts[0]:
                continue
            regex = "^" + r"[\w.-]+".join(re.escape(part) for part in parts) + "$"
            dynamics.append({"template": raw, "regex": regex, "location": f"{rel}:{line}"})

    return len(files), literals, dynamics


def plural_fallback_keys(locale_flat: dict, en_flat: dict) -> list[str]:
    """en 是复数组、本语言用基础字符串的键 —— 设计性回退，不算缺失。"""
    return sorted(
        key
        for key, value in en_flat.items()
        if isinstance(value, dict) and key in locale_flat and isinstance(locale_flat[key], str)
    )


def build_report() -> dict:
    locales = load_locales()
    absent = [locale for locale in BASELINES if locale not in locales]
    if absent:
        raise CheckError(f"基准语言不在 LOCALES 中: {', '.join(absent)}")
    trees = {locale: read_dict(locale) for locale in locales}
    flats = {locale: flatten(tree) for locale, tree in trees.items()}
    ui_keys = {
        locale: {key for key in flat if not key.startswith("phrases.")}
        for locale, flat in flats.items()
    }
    phrases = {
        locale: trees[locale].get("phrases", {})
        for locale in locales
    }
    for locale, table in phrases.items():
        if not isinstance(table, dict):
            raise CheckError(f"{locale}.json 的 phrases 必须是对象")

    top_segments = set().union(*(set(tree) for tree in trees.values()))
    file_count, literals, dynamics = scan_sources(top_segments)
    dynamic_regexes = [re.compile(item["regex"]) for item in dynamics]

    baseline_keys = set().union(*(ui_keys[locale] for locale in BASELINES if locale in ui_keys))
    phrase_baseline = set(phrases.get("en", {}))

    referenced = set(literals)
    for keys in ui_keys.values():
        referenced.update(key for key in keys if any(rx.match(key) for rx in dynamic_regexes))

    locale_reports: dict[str, dict] = {}
    for locale in locales:
        keys = ui_keys[locale]
        missing = sorted(baseline_keys - keys)
        extra = sorted(keys - baseline_keys)
        missing_phrases = [] if locale == "zh-CN" else sorted(phrase_baseline - set(phrases[locale]))
        locale_reports[locale] = {
            "raw_leaves": leaf_count(trees[locale]),
            "keys": len(keys),
            "phrases": len(phrases[locale]),
            "plural_groups": sum(1 for value in flats[locale].values() if isinstance(value, dict)),
            "plural_string_fallback": plural_fallback_keys(flats[locale], flats.get("en", {})),
            "missing": missing,
            "extra": extra,
            "unreferenced": sorted(keys - referenced),
            "missing_phrases": missing_phrases,
            "extra_phrases": sorted(set(phrases[locale]) - phrase_baseline) if locale != "zh-CN" else [],
        }

    literal_missing = {
        locale: sorted(key for key in literals if key not in ui_keys[locale])
        for locale in locales
    }
    dynamic_unmatched = {}
    for locale in locales:
        dynamic_unmatched[locale] = [
            {"template": item["template"], "location": item["location"]}
            for item, rx in zip(dynamics, dynamic_regexes)
            if not any(rx.match(key) for key in ui_keys[locale])
        ]

    hard_failures = sum(
        len(data["missing"]) + len(literal_missing[locale])
        for locale, data in locale_reports.items()
    )

    return {
        "locales": locales,
        "baselines": list(BASELINES),
        "source": {
            "files": file_count,
            "literal_keys": len(literals),
            "literal_locations": literals,
            "dynamic_patterns": dynamics,
            "literal_missing": literal_missing,
            "dynamic_unmatched": dynamic_unmatched,
        },
        "locale_reports": locale_reports,
        "hard_failures": hard_failures,
        "ok": hard_failures == 0,
    }


def print_table(report: dict) -> None:
    print("i18n 校验：基准 zh-CN + en")
    header = f"{'语言':<8}{'叶子':>7}{'界面键':>9}{'短语':>7}{'复数组':>8}{'真缺':>7}{'多余':>7}{'源码缺':>9}"
    print(header)
    print("-" * len(header))
    for locale in report["locales"]:
        data = report["locale_reports"][locale]
        print(
            f"{locale:<8}{data['raw_leaves']:>7}{data['keys']:>9}{data['phrases']:>7}"
            f"{data['plural_groups']:>8}{len(data['missing']):>7}{len(data['extra']):>7}"
            f"{len(report['source']['literal_missing'][locale]):>9}"
        )

    fallback = {}
    for locale in report["locales"]:
        count = len(report["locale_reports"][locale]["plural_string_fallback"])
        if count:
            fallback[locale] = count
    if fallback:
        print("\n复数基础键回退（en 用 .one/.other，其余语言用基础字符串）：")
        print("  " + "，".join(f"{locale} {count} 键" for locale, count in fallback.items()))

    for locale in report["locales"]:
        data = report["locale_reports"][locale]
        if data["missing"]:
            print(f"\n[{locale}] 真缺键 ({len(data['missing'])}):")
            for key in data["missing"][:50]:
                print(f"  - {key}")
            if len(data["missing"]) > 50:
                print(f"  … 还有 {len(data['missing']) - 50} 个")

    print("\n源码字面量引用缺键：")
    any_missing = False
    for locale in report["locales"]:
        keys = report["source"]["literal_missing"][locale]
        if keys:
            any_missing = True
            print(f"  [{locale}] {len(keys)} 个")
            for key in keys[:20]:
                print(f"    - {key}")
    if not any_missing:
        print("  0（五种语言均覆盖源码字面量键）")

    unmatched = report["source"]["dynamic_unmatched"]
    for locale in report["locales"]:
        patterns = unmatched[locale]
        if patterns:
            print(f"\n[{locale}] 动态模板在字典里零匹配（仅告警）：")
            for item in patterns:
                print(f"  - {item['template']} @ {item['location']}")

    extras = sum(len(report["locale_reports"][locale]["extra"]) for locale in report["locales"])
    unref = sum(len(report["locale_reports"][locale]["unreferenced"]) for locale in report["locales"])
    soft_phrases = sum(len(report["locale_reports"][locale]["missing_phrases"]) for locale in report["locales"])
    print(f"\n合计：真缺 {report['hard_failures']}，多余 {extras}，源码零引用 {unref}，短语软缺 {soft_phrases}")
    print("结果：" + ("通过" if report["ok"] else "失败"))


def main() -> int:
    parser = argparse.ArgumentParser(description="校验前端五种语言的 i18n 键集")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--strict", action="store_true", help="存在真缺键时以非 0 退出")
    args = parser.parse_args()

    try:
        report = build_report()
    except CheckError as err:
        print(f"check_i18n: {err}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_table(report)

    if args.strict and not report["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
