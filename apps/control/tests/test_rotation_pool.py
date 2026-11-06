"""账号池调度单测：规则命中、冷却分档、熔断、全冷却降级与轮转组持久化。

这些用例锁的是「为什么用了某个号」的可解释规则，不覆盖网络行为；
网络路径的换号在 providers / smoke 用例里另行覆盖。
"""

from __future__ import annotations

import json
import random
import sqlite3
from pathlib import Path

import pytest

from app.services.rotation import (
    BREAKER_COOLDOWN,
    CredentialPool,
    RotationStore,
    fingerprint,
    pick_in_group,
    sticky_key_from_payload,
)

pytestmark = pytest.mark.providers


def _clock(start: float = 1000.0):
    now = [start]

    def tick(seconds: float = 0.0) -> float:
        now[0] += seconds
        return now[0]

    return now, tick


async def test_round_robin_is_default_and_reproducible():
    pool = CredentialPool()
    keys = ["tok-a", "tok-b"]
    picks = [await pool.acquire(keys) for _ in range(4)]
    assert [pick.key for pick in picks] == ["tok-a", "tok-b", "tok-a", "tok-b"]
    assert {pick.rule for pick in picks} == {"round_robin"}


async def test_weighted_pick_follows_configured_weights():
    pool = CredentialPool(rng=random.Random(11))
    keys = ["light", "heavy"]
    pool.set_weights(keys, [1, 4])
    counts = {"light": 0, "heavy": 0}
    for _ in range(300):
        pick = await pool.acquire(keys)
        assert pick.rule == "weighted"
        counts[pick.key] += 1
    assert counts["heavy"] > counts["light"] * 2


async def test_sticky_key_hits_bound_credential():
    pool = CredentialPool()
    keys = ["tok-a", "tok-b"]
    await pool.bind_sticky("sess-1", "tok-a")
    pick = await pool.acquire(keys, sticky_key="sess-1")
    assert (pick.key, pick.rule) == ("tok-a", "sticky")
    # 另一个会话没有绑定，回到轮转，不能跟着 sess-1 一起粘
    other = await pool.acquire(keys, sticky_key="sess-2")
    assert other.rule == "round_robin"


async def test_sticky_unbinds_when_bound_credential_fails():
    pool = CredentialPool()
    keys = ["tok-a", "tok-b"]
    await pool.bind_sticky("sess-1", "tok-a")
    await pool.report_failure("tok-a", 401)
    pick = await pool.acquire(keys, sticky_key="sess-1")
    assert pick.key == "tok-b", "绑定的号进冷却后必须重新选号，不能继续撞坏号"


async def test_cooldown_tiers_escalate_and_breaker_opens():
    now, tick = _clock()
    pool = CredentialPool(clock=tick)
    first = await pool.report_failure("tok-a", 401)
    assert (first.cooldown_sec, first.consecutive, first.breaker_open) == (600.0, 1, False)
    now[0] += 601
    second = await pool.report_failure("tok-a", 401)
    assert second.cooldown_sec == 1200.0, "同账号连续失败要按倍数升级，而不是固定 600s"
    now[0] += 1201
    third = await pool.report_failure("tok-a", 401)
    assert third.breaker_open is True
    assert third.cooldown_sec == BREAKER_COOLDOWN


async def test_cooling_credential_is_skipped():
    now, tick = _clock()
    pool = CredentialPool(clock=tick)
    keys = ["tok-a", "tok-b"]
    await pool.report_failure("tok-a", 429)
    assert await pool.available(keys) == ["tok-b"]
    assert await pool.remaining("tok-a") == pytest.approx(120.0)
    pick = await pool.acquire(keys)
    assert pick.key == "tok-b"


async def test_all_cooled_strict_then_degraded():
    now, tick = _clock()
    pool = CredentialPool(clock=tick)
    keys = ["tok-a", "tok-b"]
    await pool.report_failure("tok-a", 429)
    await pool.report_failure("tok-b", 429)
    assert await pool.acquire(keys) is None, "全冷却时 strict 模式必须显式返回 None"
    pick = await pool.acquire(keys, allow_degraded=True)
    assert pick.rule == "degraded"
    assert pick.note and "冷却" in pick.note


async def test_snapshot_exposes_cooldown_remaining_and_last_used():
    now, tick = _clock()
    pool = CredentialPool(clock=tick)
    keys = ["tok-a", "tok-b"]
    await pool.acquire(keys)
    await pool.report_failure("tok-a", 403)
    snapshot = await pool.snapshot(keys)
    assert snapshot["cooling"] == 1
    assert snapshot["available"] == 1
    entries = {entry["id"]: entry for entry in snapshot["credentials"]}
    assert entries[fingerprint("tok-a")]["state"] == "cooling"
    assert entries[fingerprint("tok-a")]["cooldown_remaining"] > 0
    assert entries[fingerprint("tok-a")]["last_used_sec_ago"] is not None
    assert entries[fingerprint("tok-b")]["state"] == "ready"
    assert entries[fingerprint("tok-b")]["last_used_sec_ago"] is None
    raw = json.dumps(snapshot)
    assert "tok-a" not in raw and "tok-b" not in raw, "快照只能暴露指纹，不能泄露明文凭据"


async def test_group_selection_sticks_across_restart(tmp_path: Path):
    db = tmp_path / "rotation.db"
    keys = ["tok-a", "tok-b"]
    pool = CredentialPool()
    store = RotationStore(db)
    first = await pick_in_group(pool, store, provider="opencode", group="sota", keys=keys)
    assert first.rule == "group_init"
    second = await pick_in_group(pool, store, provider="opencode", group="sota", keys=keys)
    assert (second.key, second.rule) == (first.key, "group_sticky")

    # 模拟进程重启：新池 + 新 store 读同一个库，仍命中同组同号
    pool2 = CredentialPool()
    store2 = RotationStore(db)
    restarted = await pick_in_group(pool2, store2, provider="opencode", group="sota", keys=keys)
    assert restarted.key == first.key
    assert restarted.rule == "group_sticky"

    with sqlite3.connect(db) as conn:
        payload = conn.execute(
            "SELECT state_json FROM rotation_state WHERE provider = 'opencode' AND group_name = 'sota'"
        ).fetchone()[0]
    assert payload.count("cred-") == 1
    assert "tok-a" not in payload and "tok-b" not in payload, "落库不能复制明文凭据"


async def test_group_rotates_to_next_member_after_failure(tmp_path: Path):
    db = tmp_path / "rotation.db"
    keys = ["tok-a", "tok-b"]
    pool = CredentialPool()
    store = RotationStore(db)
    first = await pick_in_group(pool, store, provider="opencode", group="sota", keys=keys)
    await pool.report_failure(first.key, 401)
    second = await pick_in_group(pool, store, provider="opencode", group="sota", keys=keys)
    assert second.key != first.key
    assert second.rule == "group_rotate"


async def test_group_with_all_members_cooled_returns_none(tmp_path: Path):
    keys = ["tok-a", "tok-b"]
    pool = CredentialPool()
    store = RotationStore(tmp_path / "rotation.db")
    for key in keys:
        await pool.report_failure(key, 429)
    assert await pick_in_group(pool, store, provider="opencode", group="sota", keys=keys) is None


def test_sticky_key_from_payload_sources():
    assert sticky_key_from_payload({"session_id": " s1 "}) == "s1"
    assert sticky_key_from_payload({"conversation_id": "c1"}) == "c1"
    assert sticky_key_from_payload({"metadata": {"user_id": "u1"}}) == "u1"
    assert sticky_key_from_payload({"user": "client-7"}) == "client-7"
    assert sticky_key_from_payload({"model": "m", "messages": []}) == ""
    assert sticky_key_from_payload(None) == ""
