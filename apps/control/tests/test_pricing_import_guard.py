"""P0 回归：空/畸形 YAML 必须给出明确报错，不能抛 TypeError 打崩前端整页。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.billing import import_price_books

SCHEMA = """
CREATE TABLE price_books (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    token_class TEXT NOT NULL,
    price_micro_per_1m INTEGER NOT NULL,
    currency TEXT NOT NULL,
    effective_from INTEGER NOT NULL,
    created_by TEXT,
    note TEXT
)
"""


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    try:
        yield c
    finally:
        c.close()


def seed(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "prices.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "text,expect",
    [
        ("", "内容为空"),
        ("   \n\t\n", "内容为空"),
        ("prices: [oops", "解析失败"),
        ("models: [muse-spark]", "缺少 prices 段"),
        ("prices: []", "prices 段为空"),
        ("prices:\n  - provider: '*'\n    token_class: input\n    price_micro_per_1m: 1\n", "缺 model"),
    ],
)
def test_rejects_broken_seed(tmp_path: Path, conn: sqlite3.Connection, text: str, expect: str):
    with pytest.raises(ValueError, match=expect):
        import_price_books(conn, seed(tmp_path, text))


def test_valid_seed_dry_run_then_insert(tmp_path: Path, conn: sqlite3.Connection):
    path = seed(
        tmp_path,
        """
prices:
  - provider: "*"
    model: demo-model
    token_class: input
    price_micro_per_1m: 3
""",
    )
    dry = import_price_books(conn, path, dry_run=True)
    assert dry["inserted"] == 1 and dry["total"] == 1
    assert conn.execute("SELECT COUNT(*) FROM price_books").fetchone()[0] == 0

    real = import_price_books(conn, path)
    assert real["inserted"] == 1
    assert conn.execute("SELECT COUNT(*) FROM price_books").fetchone()[0] == 1

    again = import_price_books(conn, path)
    assert again["inserted"] == 0 and again["skipped"] == 1
