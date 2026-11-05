#!/usr/bin/env python3
"""零 skip 门禁：pytest 日志只放行显式声明 [env] 的环境性跳过，且条数须精确等于本平台声明数。

用法：
    python scripts/check_no_bad_skip.py pytest.log        # 门禁本体
    python scripts/check_no_bad_skip.py --selftest         # 门禁自测（喂假日志）

为什么不再用 `grep -q 'SKIPPED|N skipped'` 一刀切：仓库里有一条真实的环境性跳过
（test_hardening.py::test_db_file_is_owner_only，Windows 没有 POSIX 权限位），
Linux CI 上它照跑、Windows 上才跳。一刀切会把正确行为判红；放宽成“跳过数 <= N”
又会把 conftest 那种整会话 pytest.skip 的假绿放回来。所以：原因必须带 [env] 标记，
条数按平台写死，多一条少一条都失败。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# 按平台写死允许的环境性跳过总数：改动此表必须同步改动 skip 声明本身
EXPECTED_ENV_SKIPS = {
    "win32": 1,  # test_hardening.py::test_db_file_is_owner_only（无 POSIX 权限位）
    "linux": 0,
    "darwin": 0,
}
EXPECTED_DEFAULT = 0

ENV_MARK = "[env]"

_SKIP_LINE = re.compile(r"SKIPPED\s+\[(\d+)\]\s+(\S+\:\d+)\:\s+(.*)")
_SUMMARY_SKIPPED = re.compile(r"(\d+) skipped")
_FINISHED = re.compile(r"(\d+) passed|(\d+) failed|(\d+) error")


def expected_for(platform: str) -> int:
    """本平台允许的环境性跳过条数。"""
    return EXPECTED_ENV_SKIPS.get(platform, EXPECTED_DEFAULT)


def audit(log: str, platform: str = sys.platform) -> list[str]:
    """返回问题列表；空列表表示门禁通过。"""
    problems: list[str] = []
    skipped: list[tuple[str, str, int]] = []
    # 摘要行里的 skipped 数：没写 skipped 就是 0，None 表示整条摘要都没找到
    summary: int | None = None

    for line in log.splitlines():
        hit = _SKIP_LINE.match(line.strip())
        if hit:
            skipped.append((hit.group(2), hit.group(3).strip(), int(hit.group(1))))
            continue
        if _FINISHED.search(line):
            found = _SUMMARY_SKIPPED.search(line)
            summary = int(found.group(1)) if found else 0

    for where, reason, _ in skipped:
        if not reason.startswith(ENV_MARK):
            problems.append(f"跳过原因缺 {ENV_MARK} 标记：{where} —— {reason}")

    total = sum(count for _, _, count in skipped)
    want = expected_for(platform)
    if total != want:
        problems.append(f"跳过条数与平台声明不符：实际 {total}，{platform} 声明 {want}")

    if summary is None:
        problems.append("日志里找不到 pytest 摘要行（未跑测试或日志为空？）")
    elif summary != total:
        problems.append(f"摘要行 {summary} skipped 与 SKIPPED 明细 {total} 条对不上")
        problems.append("  （预览会截断明细，需完整日志；明细不全无法审计原因）")

    return problems


def run(log_path: Path, platform: str = sys.platform) -> int:
    if not log_path.is_file():
        print(f"::error::零 skip 门失败：找不到日志 {log_path}")
        return 1
    problems = audit(log_path.read_text(encoding="utf-8", errors="replace"), platform)
    if problems:
        for item in problems:
            print(f"::error::{item}")
        print(f"零 skip 门失败：{len(problems)} 项问题（平台 {platform}）")
        return 1
    print(f"零 skip 门通过：{platform} 下跳过条数 = {expected_for(platform)}，且全部带 {ENV_MARK} 标记")
    return 0


_WIN_LOG = """\
tests/test_hardening.py::test_db_file_is_owner_only SKIPPED (Windows ...)
--- live log ---
=========================== short test summary info ===========================
SKIPPED [1] tests/test_hardening.py:98: [env] Windows 无 POSIX 权限位
902 passed, 1 skipped, 3 warnings in 300.00s
"""

_LINUX_LOG = """\
SKIPPED [1] tests/test_demo_mode.py:138: [env] 端口被占用（仅演示模式容器缺依赖）
902 passed, 1 skipped in 300.00s
"""

_CLEAN_LOG = "902 passed, 12 warnings in 300.00s\n"

_BARE_SKIP_LOG = """\
SKIPPED [1] tests/conftest.py:0: 控制面启动失败：该环境不具备条件
901 passed, 1 skipped in 300.00s
"""

_TWO_ENV_WIN = """\
SKIPPED [1] tests/test_hardening.py:98: [env] Windows 无 POSIX 权限位
SKIPPED [1] tests/test_other.py:12: [env] 另一个环境性跳过
901 passed, 2 skipped in 300.00s
"""

_COUNT_LIE_LOG = """\
SKIPPED [1] tests/test_hardening.py:98: [env] Windows 无 POSIX 权限位
901 passed, 2 skipped in 300.00s
"""

_EMPTY_LOG = ""


def selftest() -> int:
    """喂假 pytest 尾行，断言放行/拦截口径。"""
    cases = [
        ("Windows 单条 [env] 跳过放行", _WIN_LOG, "win32", 0),
        ("Linux 零跳过放行", _CLEAN_LOG, "linux", 0),
        ("Linux 出现跳过即失败（声明 0 条）", _LINUX_LOG, "linux", 1),
        ("无条件 skip（无 [env]）失败", _BARE_SKIP_LOG, "linux", 1),
        ("Windows 两条 [env] 超过声明数失败", _TWO_ENV_WIN, "win32", 1),
        ("摘要 skipped 数与明细不符失败", _COUNT_LIE_LOG, "win32", 1),
        ("空日志失败", _EMPTY_LOG, "linux", 1),
    ]
    failed = 0
    for name, log, platform, want_code in cases:
        problems = audit(log, platform)
        code = 1 if problems else 0
        ok = code == want_code
        print(f"{'PASS' if ok else 'FAIL'} {name}（平台 {platform}，期望 exit={want_code}，实际 {code}）")
        if not ok:
            failed += 1
            for item in problems:
                print(f"      {item}")
    print(f"{len(cases) - failed}/{len(cases)} 用例通过")
    print(f"TEST_EXIT={failed}")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    target = Path(argv[1]) if len(argv) > 1 else Path("pytest.log")
    return run(target)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
