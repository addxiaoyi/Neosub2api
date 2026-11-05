#!/usr/bin/env python3
"""check_i18n 扫描器 strip_comments 的回归测试（可独立执行：python scripts/test_check_i18n.py）。

修复前能复现的假误判（实际让 CI 报过「真缺 5」，五条全是 logs.csv）：
    const name = /filename="?([^";]+)"?/;
    // 注释里的 "logs.csv" 不是翻译键
旧实现只认字符串/模板，正则里的 `"` 让它误入 double 态，状态机一路错到下一行的
注释；注释的 // 因此不再被剥离，注释里的 "logs.csv" 落进扫描文本，被 QUOTED 判成
键引用。test_quoted_regex_does_not_leak_comment_literals 就是这条的最小化复现。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_i18n


def _literal_keys(code: str) -> list[str]:
    """按扫描器同一口径取字面量键：先剥注释/正则，再用 QUOTED + 属性名过滤。"""
    stripped = check_i18n.strip_comments(code)
    keys: list[str] = []
    for found in check_i18n.QUOTED.finditer(stripped):
        if check_i18n.is_property_name(stripped, found.start(), found.end()):
            continue
        keys.append(found.group(1))
    return keys


def test_quoted_regex_does_not_leak_comment_literals() -> None:
    # 修复前这里会拿到 ["logs.csv"]：正则里的引号把注释的 // 吃掉了
    code = (
        'const name = /filename="?([^";]+)"?/;\n'
        '// 注释里的 "logs.csv" 不是翻译键\n'
    )
    assert _literal_keys(code) == [], "正则和注释都不是键来源"


def test_real_literal_keys_still_found() -> None:
    code = (
        "const a = t('logs.export');\n"
        'const b = t("logs.exporting");\n'
        "const obj = {'logs.lookalike': 1};\n"
    )
    assert _literal_keys(code) == ["logs.export", "logs.exporting"], "真键必须照抓，属性名不抓"


def test_slash_inside_character_class_keeps_parsing() -> None:
    # 字符类里的 / 不是结束符；提前收尾会把同一行后面的真键当成正则体吞掉
    code = 'const sep = /[/]+/g; const label = t("logs.export");\n'
    assert _literal_keys(code) == ["logs.export"]


def test_division_is_not_treated_as_regex() -> None:
    # a / b 与 (a + b) / c 都是除法；被当正则会吞掉同一行后面的真键
    code = (
        "const r1 = a / b / c; const label = t('logs.export');\n"
        'const r2 = (a + b) / c; const other = t("logs.exporting");\n'
    )
    assert _literal_keys(code) == ["logs.export", "logs.exporting"]


def test_regex_preceded_by_equals_then_real_key() -> None:
    # 等号后的正则是真正则：引号、点号都在字面量里，不能污染后面的键
    code = 'const re = /"logs.csv"/; const label = t("logs.export");\n'
    assert _literal_keys(code) == ["logs.export"]


def test_jsx_text_slash_and_arrow_regex() -> None:
    # 回归：JSX 文本里的 "/" 前面是 >，曾被当成正则起点，吞掉同一行后面的真键
    # （apps/web/app/(main)/quota/page.tsx 的 t('quota.colActive') 就是这么被漏掉的）
    jsx = '// <span> / {t(\'quota.colActive\')}</span>\n<sp>x / {t("logs.export")}</sp>\n'
    assert _literal_keys(jsx) == ["logs.export"], "JSX 里的 / 是文本，不是正则"

    arrow = 'const f = () => /"logs.csv"/.test(s); const g = t("logs.exporting");\n'
    assert _literal_keys(arrow) == ["logs.exporting"], "=> 后面才是正则"


CASES = (
    test_quoted_regex_does_not_leak_comment_literals,
    test_real_literal_keys_still_found,
    test_slash_inside_character_class_keeps_parsing,
    test_division_is_not_treated_as_regex,
    test_regex_preceded_by_equals_then_real_key,
    test_jsx_text_slash_and_arrow_regex,
)


def main() -> int:
    failed = 0
    for case in CASES:
        try:
            case()
        except AssertionError as err:
            failed += 1
            print(f"FAIL {case.__name__}: {err}")
        else:
            print(f"PASS {case.__name__}")
    print(f"\n{len(CASES) - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
