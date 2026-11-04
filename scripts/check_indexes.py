#!/usr/bin/env python3
"""索引存在性检查（轻量）：按真实启动路径建库，确认 perf_indexes 的索引真的落地。

不跑压测，也不碰仓库 data/ 目录。只验证：
  1. Database 建库（db.py 已接线 ensure_indexes）能完整跑完 schema 与轻量迁移；
  2. sqlite_master 里每条期望索引都存在，且挂在 DDL 声明的表上。
索引缺失说明启动接线没生效或 DDL 与 schema 不匹配，直接失败。
"""

from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONTROL = REPO / "apps" / "control"

_ON_TABLE = re.compile(r"\bON\s+([A-Za-z_][\w$]*)", re.I)


def _load():
    control = str(CONTROL)
    if control not in sys.path:
        sys.path.insert(0, control)
    from app.db import Database
    from app.perf_indexes import INDEXES

    return Database, INDEXES


def _lookup(conn: sqlite3.Connection, name: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index' AND name = ?",
        (name,),
    ).fetchone()


def verify(db_path: Path, database_cls, expected: tuple[tuple[str, str], ...]) -> list[str]:
    """返回问题列表；空列表表示通过。"""
    database_cls(db_path)  # 与启动一致：建 schema、补列，并在最后接线 ensure_indexes

    wanted = {
        name: (match.group(1).lower() if (match := _ON_TABLE.search(ddl)) else "")
        for name, ddl in expected
    }

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        problems: list[str] = []
        for name, _ddl in expected:
            row = _lookup(conn, name)
            if row is None:
                problems.append(f"缺少索引 {name}（启动建库路径未创建）")
            elif row["tbl_name"].lower() != wanted[name]:
                problems.append(f"索引 {name} 挂在 {row['tbl_name']}，期望 {wanted[name]}")
        return problems
    finally:
        conn.close()


def main() -> int:
    database_cls, expected = _load()
    try:
        with tempfile.TemporaryDirectory(prefix="unified-index-") as tmp:
            problems = verify(Path(tmp) / "unified.db", database_cls, expected)
    except (ImportError, sqlite3.Error) as err:
        print(f"索引检查无法执行：{err}", file=sys.stderr)
        return 2

    total = len(expected)
    print(f"索引检查：{total} 条期望索引")
    print(f"  建库后存在：{total - len(problems)} / {total}")
    for item in problems:
        print(f"  - {item}")
    print("结果：" + ("通过" if not problems else "失败"))
    return 0 if not problems else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
