"""鉴权旁路 + usage 记录：匿名/伪造 key → 401；合法 key 非流式/流式 → DB usage_records 有落库。"""

from __future__ import annotations

import json
import sqlite3
import time

import httpx
import pytest

pytestmark = [pytest.mark.auth, pytest.mark.usage]


@pytest.fixture(scope="module")
def bearer_key(admin: httpx.Client) -> str:
    r = admin.post("/api/keys", json={"name": "auth-usage-suite"})
    assert r.status_code == 200
    return r.json()["key"]


def test_no_auth_rejected(anon: httpx.Client) -> None:
    r = anon.post(
        "/v1/chat/completions",
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    # UNIFIED_ALLOW_ANONYMOUS_GATEWAY=false，且 DB settings 没配 gateway 开关 → 回落到 env=false → 401
    assert r.status_code == 401, f"没 Authorization 应 401：{r.status_code} {r.text}"


def test_wrong_key_rejected(anon: httpx.Client) -> None:
    r = anon.post(
        "/v1/chat/completions",
        headers={"authorization": "Bearer u2a_fakefakefakefakefakefakefake"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 401, f"伪造 key 应 401：{r.status_code} {r.text}"


def test_usage_recorded_non_stream(
    admin: httpx.Client, anon: httpx.Client, bearer_key: str, db_path: str
) -> None:
    # 清一下 usage_records 让断言更干净
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM usage_records")
    conn.commit()
    conn.close()

    r = anon.post(
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {bearer_key}"},
        json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200, f"{r.status_code} {r.text}"

    time.sleep(0.2)  # 控制面进程和 pytest 进程并发，稍微等一下让 _record commit

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM usage_records ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()

    assert row is not None, "usage_records 应有记录"
    assert row["stream"] == 0, f"非流式应为 0，拿到 {row['stream']}"
    assert row["usage_json"], "usage_json 不能为空"
    usage = json.loads(row["usage_json"])
    assert usage.get("prompt_tokens") == 11, f"假上游固定 11，拿到 {usage}"
    assert usage.get("completion_tokens") == 7
    assert row["provider"], "provider 字段应有值"


def test_usage_recorded_stream(
    admin: httpx.Client, anon: httpx.Client, bearer_key: str, db_path: str
) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM usage_records")
    conn.commit()
    conn.close()

    with anon.stream(
        "POST",
        "/v1/chat/completions",
        headers={"authorization": f"Bearer {bearer_key}"},
        json={
            "model": "m",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    ) as resp:
        assert resp.status_code == 200
        for _ in resp.iter_bytes():
            pass  # 完整消费流，让 finally 里的 _record commit

    time.sleep(0.2)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM usage_records ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()

    assert row is not None, "usage_records 应有记录"
    assert row["stream"] == 1, f"流式应为 1，拿到 {row['stream']}"
    # 流式 usage 从 SSE 末 chunk 里捞，假上游的末 chunk 带 usage 对象
    assert row["usage_json"], "usage_json 不应为空"
