"""2-B5：opencode 模型级配额只有测试改动。

验证 quota_policies 的 model 收窄确实生效：同一把 key 下，只有命中模型的流水计入，
别的模型、别的 provider 都不串账。
"""

from __future__ import annotations

from pathlib import Path

from app.billing import check_quota, settle, window_usage
from app.db import Database


def _conn(tmp_path):
    return Database(Path(tmp_path) / "unified.db").connect()


def _policy(conn, *, key_id: int, provider: str, model: str, limit_micro: int) -> int:
    cur = conn.execute(
        "INSERT INTO quota_policies(key_id, provider, model, window, limit_micro, "
        "created_at, updated_at) VALUES(?,?,?,?,?,0,0)",
        (key_id, provider, model, "month", limit_micro),
    )
    return int(cur.lastrowid)


def _spend(conn, *, model: str, micro: int, ts: int, provider: str = "opencode") -> dict:
    return settle(
        conn,
        ts=ts,
        key_id=1,
        key_name="k",
        provider=provider,
        model=model,
        status=200,
        usage={"prompt_tokens": micro},
        pricing={
            "at": 0,
            "lines": {
                "input": {
                    "price_id": 1,
                    "provider": provider,
                    "model": model,
                    "price_micro_per_1m": 1000000,
                }
            }
        },
    )


TS = 1793600000


def test_model_scoped_policy_counts_only_matching_model(tmp_path):
    conn = _conn(tmp_path)
    _policy(conn, key_id=1, provider="opencode", model="oc-model-a", limit_micro=1000)
    policy = conn.execute("SELECT * FROM quota_policies").fetchone()

    _spend(conn, model="oc-model-a", micro=600, ts=TS)
    _spend(conn, model="oc-model-b", micro=600, ts=TS)
    _spend(conn, model="oc-model-a", micro=600, ts=TS, provider="zcode")

    usage = window_usage(conn, policy, TS)
    assert usage["used_micro"] == 600, "只算 opencode + oc-model-a 这一笔"
    assert usage["used_requests"] == 1

    assert check_quota(conn, key_id=1, provider="opencode", model="oc-model-a", at=TS) is None
    assert check_quota(conn, key_id=1, provider="opencode", model="oc-model-b", at=TS) is None
    assert check_quota(conn, key_id=1, provider="zcode", model="oc-model-a", at=TS) is None


def test_model_scoped_policy_blocks_after_crossing_limit(tmp_path):
    conn = _conn(tmp_path)
    _policy(conn, key_id=1, provider="opencode", model="oc-model-a", limit_micro=1000)
    _spend(conn, model="oc-model-a", micro=600, ts=TS)
    _spend(conn, model="oc-model-a", micro=600, ts=TS)

    denied = check_quota(conn, key_id=1, provider="opencode", model="oc-model-a", at=TS)
    assert denied is not None
    assert denied["type"] == "quota_exceeded"
    assert denied["policy_id"] == conn.execute("SELECT id FROM quota_policies").fetchone()["id"]

    # 同 key 的另一个模型不受影响
    assert check_quota(conn, key_id=1, provider="opencode", model="oc-model-b", at=TS) is None


def test_key_wide_policy_still_counts_all_models(tmp_path):
    conn = _conn(tmp_path)
    _policy(conn, key_id=1, provider="opencode", model="", limit_micro=1000)
    policy = conn.execute("SELECT * FROM quota_policies").fetchone()
    _spend(conn, model="oc-model-a", micro=600, ts=TS)
    _spend(conn, model="oc-model-b", micro=600, ts=TS)
    assert window_usage(conn, policy, TS)["used_micro"] == 1200
